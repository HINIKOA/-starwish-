"""Opt-in, read-only GraphFlow adapter shared by the panel and MCP."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from itertools import chain
from pathlib import Path, PurePosixPath

from . import config

VERSION = "3.9.2"
PROVIDER = f"GraphFlow {VERSION}"
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 2500
MAX_READ_TARGETS = 12
MAX_AFFECTED_INPUTS = 128
GRAPH_NODE_LIMIT = 100
GRAPH_EDGE_LIMIT = 160
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
SOURCE_SUFFIXES = frozenset(
    ".py .pyi .js .jsx .mjs .cjs .ts .tsx .mts .cts .go .rs .c .h .cpp .hpp .cc .cxx .hxx .cs .css .html .md".split()
)
EXCLUDED = {"soul", "node_modules", "graphflow-out", "secrets", "credentials", "vendor", "dist"}
_gate = threading.BoundedSemaphore(1)
_active_snapshot: _Snapshot | None = None
_READ_DEDUP_TTL = 900
_READ_DEDUP_LIMIT = 256
_read_dedup: OrderedDict[bytes, tuple[float, bytes]] = OrderedDict()
_read_dedup_lock = threading.Lock()


class GraphFlowError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _workspace() -> Path:
    value = os.environ.get("STARWISH_GRAPHFLOW_WORKSPACE", "")
    path = Path(value)
    if not value or not path.is_absolute():
        raise GraphFlowError("请先配置 STARWISH_GRAPHFLOW_WORKSPACE 为可信 Git 项目根目录。", 503)
    path = path.resolve()
    if (path == Path(path.anchor) or path == Path.home().resolve()
            or path.is_relative_to(config.SOUL_DIR) or config.SOUL_DIR == path
            or not (path / ".git").exists()):
        raise GraphFlowError("工作区必须是独立的可信 Git 项目根目录，不能是灵魂包。", 503)
    return path


def checked_workspace(root_dir: str = "") -> Path:
    workspace = _workspace()
    if not isinstance(root_dir, str):
        raise GraphFlowError("工作区格式无效。")
    if root_dir:
        try:
            root = Path(root_dir)
            if not root.is_absolute() or root.resolve() != workspace:
                raise ValueError
        except (OSError, RuntimeError, ValueError) as exc:
            raise GraphFlowError("仅可使用已配置的工作区。", 403) from exc
    return workspace


def engine_prefix() -> Path:
    configured = os.environ.get("STARWISH_GRAPHFLOW_PREFIX")
    if configured:
        return Path(configured)
    bundled = config.APP_DIR / "engines" / f"graphflow-{VERSION}"
    if bundled.exists():
        return bundled
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
    return base / "StarWish" / f"graphflow-{VERSION}"


def _engine(prefix: Path | None = None) -> tuple[str, Path, str]:
    prefix = prefix if prefix is not None else engine_prefix()
    package = prefix / "node_modules" / "@roarpeng" / "graphflow"
    node, git = shutil.which("node"), shutil.which("git")
    entry = package / "dist" / "slicer.js"
    try:
        version = json.loads((package / "package.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError):
        version = None
    if not node or not git or version != VERSION or not entry.is_file():
        raise GraphFlowError(f"需要 Git、Node.js 和固定版本 {PROVIDER}；请查看接入文档。", 503)
    return node, entry.resolve(), git


def status() -> dict:
    workspace = None
    try:
        workspace = _workspace().name
        _engine()
    except (GraphFlowError, OSError, RuntimeError) as exc:
        reason = str(exc) if isinstance(exc, GraphFlowError) else "无法访问工作区或引擎。"
        return {"available": False, "workspace": workspace, "provider": PROVIDER, "reason": reason}
    return {"available": True, "workspace": workspace, "provider": PROVIDER, "reason": ""}


def _safe_source(value: str) -> PurePosixPath | None:
    if not value or len(value) > 500 or re.search(r'[:\\\x00-\x1f]', value):
        return None
    parts = value.split("/")
    if any(not part or part.startswith(".") or part.rstrip(" .") != part
           or part.casefold() in EXCLUDED for part in parts):
        return None
    path = PurePosixPath(value)
    return path if path.suffix.casefold() in SOURCE_SUFFIXES else None


def _regular_source(info: os.stat_result) -> bool:
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not (getattr(info, "st_file_attributes", 0) & _REPARSE))


def _source_metadata(source: Path, soul: Path) -> os.stat_result:
    resolved = source.resolve()
    if resolved != source or resolved.is_relative_to(soul):
        raise GraphFlowError("源码须位于授权项目内，不允许链接或灵魂包路径。", 404)
    info = source.lstat()
    if not _regular_source(info):
        raise GraphFlowError("源码须为普通文件，不允许硬链接或重解析点。", 404)
    return info


@contextmanager
def _open_source(source: Path, soul: Path, checked: os.stat_result):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(source, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        # Verify the opened file even on hosts without O_NOFOLLOW.
        if (not _regular_source(opened) or not os.path.samestat(checked, opened)
                or not os.path.samestat(opened, _source_metadata(source, soul))):
            raise GraphFlowError("打开源码时文件身份已变化。", 409)
        yield stream


def _refresh_snapshot(workspace: Path, dest: Path, git: str, previous: dict[str, bytes],
                      preferred: tuple[str, ...] = ()) -> tuple[bool, dict[str, bytes], dict]:
    process = subprocess.run(
        [git, "ls-files", "--cached", "-z"], cwd=workspace, capture_output=True,
        timeout=10, check=False,
    )
    if process.returncode:
        raise GraphFlowError("无法读取 Git 的受版本控制文件清单。", 503)
    files: dict[str, bytes] = {}
    changes: dict[str, bytes] = {}
    used = 0
    partial = False
    soul = config.SOUL_DIR.resolve()
    tracked = process.stdout.split(b"\0")
    requested = dict.fromkeys(os.fsencode(path) for path in preferred)
    if requested:
        present = {raw for raw in tracked if raw in requested}
        # Explicit targets get the first slots, never an exemption from Git or size checks.
        ordered = chain((raw for raw in requested if raw in present),
                        (raw for raw in tracked if raw not in requested))
    else:
        ordered = iter(tracked)
    for raw in ordered:
        value = os.fsdecode(raw)
        relative = _safe_source(value)
        if relative is None or value in files:
            continue
        source = workspace.joinpath(*relative.parts)
        try:
            metadata = _source_metadata(source, soul)
            if metadata.st_size > MAX_FILE_BYTES:
                partial = True
                continue
            if len(files) >= MAX_FILES or used + metadata.st_size > MAX_BYTES:
                partial = True
                break
            with _open_source(source, soul, metadata) as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or used + len(data) > MAX_BYTES:
                partial = True
                continue
            data.decode("utf-8")
        except (GraphFlowError, OSError, UnicodeError, RuntimeError):
            continue
        digest = hashlib.sha256(data).digest()
        files[value] = digest
        if previous.get(value) != digest:
            changes[value] = data
        used += len(data)
    removed = previous.keys() - files.keys()
    for value in removed:
        path = dest.joinpath(*value.split("/"))
        path.unlink(missing_ok=True)
        # Upstream 3.9.2 stores one AST shard per relative path.
        shard = hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()
        (dest / ".graphflow" / "ast-cache" / f"{shard}.json").unlink(missing_ok=True)
        # A removed file's parent may now be a tracked file itself.
        parent = path.parent
        while parent != dest:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    for value, data in changes.items():
        target = dest.joinpath(*value.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    added = len(files.keys() - previous.keys())
    return partial, files, {"files": len(files), "added": added, "updated": len(changes) - added,
                            "removed": len(removed), "partial": partial}


class _Snapshot:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.directory = tempfile.TemporaryDirectory(prefix="starwish-code-")
        self.root = Path(self.directory.name)
        self.files: dict[str, bytes] = {}
        self.warmed: dict[str, bytes] = {}

    def refresh(self, git: str, preferred: tuple[str, ...] = ()) -> tuple[bool, dict]:
        partial, self.files, stats = _refresh_snapshot(self.workspace, self.root, git, self.files, preferred)
        self.warmed = {path: digest for path, digest in self.warmed.items() if self.files.get(path) == digest}
        return partial, stats

    def close(self) -> None:
        self.directory.cleanup()


def _clear_snapshot() -> None:
    global _active_snapshot
    snapshot, _active_snapshot = _active_snapshot, None
    if snapshot is not None:
        snapshot.close()


def _workspace_snapshot(workspace: Path) -> _Snapshot:
    global _active_snapshot
    if _active_snapshot is None or _active_snapshot.workspace != workspace:
        _clear_snapshot()
        _active_snapshot = _Snapshot(workspace)
    return _active_snapshot


_BRIDGE = """
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const path = require('node:path');
const slicer = require(process.argv[1]);
if (input.rename) {
    const { executeSurgicalEdit } = require(path.join(path.dirname(process.argv[1]), 'surgical-edit.js'));
    const result = executeSurgicalEdit({ rootDir: input.rootDir, file: input.rename.file,
        symbol: input.rename.symbol, renameTo: input.rename.renameTo });
    const reason = result.text.match(/\\[([a-z-]+)\\]/);
    process.stdout.write(JSON.stringify({ ok: result.ok, touched: result.touchedFiles, reason: reason?.[1] }));
} else if (input.changedFiles) {
    const { resolveAffectedTests } = require(path.join(path.dirname(process.argv[1]), 'dehydrator.js'));
    process.stdout.write(JSON.stringify(resolveAffectedTests(input.rootDir, input.changedFiles).affectedTests));
} else if (input.warmFiles) {
    const index = slicer.getWorkspaceAstIndex(input.rootDir);
    const warmed = input.warmFiles.filter((file) => index.getFileAnalysis(file) != null);
    slicer.flushWorkspaceAstCaches();
    process.stdout.write(JSON.stringify(warmed));
} else if (input.locate) {
    const { file, symbols } = input.locate;
    const source = slicer.getWorkspaceAstIndex(input.rootDir).getFileAnalysis(file);
    const ranges = symbols.map((symbol) => source ? slicer.findMatchingSymbolsInAst(source.ast, symbol)
        .slice(0, 2).map(({ line, endLine }) => [line, endLine]) : []);
    process.stdout.write(JSON.stringify(ranges));
} else if (input.graph) {
    const { buildKnowledgeGraphData } = require(path.join(path.dirname(process.argv[1]), 'doc-graph.js'));
    const index = slicer.getWorkspaceAstIndex(input.rootDir);
    const { nodes, edges } = buildKnowledgeGraphData(index);
    const { target, nodeLimit, edgeLimit } = input.graph;
    const known = new Set(input.graph.files);
    const byId = new Map();
    const docFiles = new Map();
    for (const { id, kind, detail } of nodes) {
        if (kind === 'doc') {
            // Resolve only an unambiguous input path; never emit the section identifier.
            const files = [];
            for (let split = id.indexOf('#', 4); split >= 0; split = id.indexOf('#', split + 1)) {
                const file = id.slice(4, split);
                if (known.has(file) && file.toLowerCase().endsWith('.md')) files.push(file);
            }
            if (files.length === 1) {
                const file = files[0];
                docFiles.set(id, 'doc-file:' + file);
                byId.set('doc-file:' + file, { kind: 'doc', file });
            }
            continue;
        }
        if (kind !== 'file' && kind !== 'symbol') continue;
        const separator = id.lastIndexOf('#');
        const file = kind === 'file' ? id.slice(5) : id.slice(4, separator);
        if (!known.has(file)) continue;
        const row = { kind, file };
        if (kind === 'symbol') {
            const symbol = id.slice(separator + 1);
            if (separator < 4 || !symbol || symbol.length > 160) continue;
            const range = detail.slice(file.length).match(/^:([0-9]+)-([0-9]+) [|]/);
            if (!range) continue;
            Object.assign(row, { symbol, line: Number(range[1]), end_line: Number(range[2]) });
        }
        byId.set(id, row);
    }
    const links = [];
    const uniqueDocLinks = new Set();
    for (const { kind, source, target } of edges) {
        if (['defines', 'imports', 'calls'].includes(kind) && byId.has(source) && byId.has(target)) {
            links.push({ kind, source, target });
        } else if (kind === 'doc-links-code' && docFiles.has(source) && byId.has(target)) {
            const doc = docFiles.get(source);
            const key = JSON.stringify([doc, target]);
            if (uniqueDocLinks.has(key)) continue;
            uniqueDocLinks.add(key);
            links.push({ kind, source: doc, target });
        }
    }
    for (const pair of input.graph.cochange) {
        const [source, target] = pair.files.map(file => 'file:' + file);
        if (byId.has(source) && byId.has(target)) links.push({
            kind: 'co-edit', source, target, co_commit_count: pair.co_commit_count, confidence: pair.confidence,
        });
    }
    const seeds = target ? new Set([...byId].filter(([, node]) => node.file === target).map(([id]) => id)) : null;
    const scoped = seeds ? links.filter(({ source, target }) => seeds.has(source) || seeds.has(target)) : links;
    const candidates = seeds ? new Set([...seeds, ...scoped.flatMap(({ source, target }) => [source, target])])
        : new Set(byId.keys());
    const ids = [...candidates].slice(0, nodeLimit);
    const ordinals = new Map(ids.map((id, i) => [id, i]));
    const retained = scoped.filter(({ source, target }) => ordinals.has(source) && ordinals.has(target));
    const result = {
        nodes: ids.map(id => byId.get(id)),
        edges: retained.slice(0, edgeLimit).map(({ source, target, ...relation }) =>
            ({ ...relation, source: ordinals.get(source), target: ordinals.get(target) })),
        truncated: candidates.size > ids.length || scoped.length > Math.min(retained.length, edgeLimit) ||
            edges.length >= 5000,
    };
    slicer.flushWorkspaceAstCaches();
    process.stdout.write(JSON.stringify(result));
} else if (input.impact) {
    const { file, symbol } = input.impact;
    const index = slicer.getWorkspaceAstIndex(input.rootDir);
    const source = index.getFileAnalysis(file);
    const matches = source ? slicer.findMatchingSymbolsInAst(source.ast, symbol) : [];
    const { computeBlastRadius } = require(path.join(path.dirname(process.argv[1]), 'blast-radius.js'));
    const { resolveRelativeImportPath } = require(path.join(path.dirname(process.argv[1]), 'ast-engine.js'));
    const known = index.getFileSet();
    const moduleNames = new Map();
    let parse;
    try { ({ parse } = require('node:module').createRequire(process.argv[1])('@babel/parser')); } catch {}
    const esm = /\\.(?:[cm]?[jt]s|[jt]sx)$/i;
    const identifier = node => node?.name ?? node?.value;
    function namedExports(module) {
        if (moduleNames.has(module)) return moduleNames.get(module);
        if (!parse || moduleNames.size >= 32 || !esm.test(module) || !known.has(module)) return null;
        const names = new Map();
        const named = new Set();
        const details = {names, named};
        moduleNames.set(module, details);
        const content = index.getFileAnalysis(module)?.content;
        if (!content) return details;
        let statements;
        try {
            const plugins = /\\.[cm]?tsx?$/i.test(module) ? ['typescript'] : [];
            if (/\\.(?:[cm]?js|[jt]sx)$/i.test(module)) plugins.push('jsx');
            statements = parse(content, {sourceType: 'module', plugins}).program.body;
        } catch { return details; }
        const imports = new Map();
        for (const stmt of statements) {
            if (stmt.type !== 'ImportDeclaration' || stmt.importKind === 'type') continue;
            for (const spec of stmt.specifiers) {
                if (spec.type === 'ImportSpecifier' && spec.importKind !== 'type') {
                    const binding = identifier(spec.imported);
                    imports.set(identifier(spec.local), {module: stmt.source.value, binding});
                    named.add(JSON.stringify([stmt.loc.start.line, stmt.source.value, binding]));
                }
            }
        }
        for (const stmt of statements) {
            if (stmt.type !== 'ExportNamedDeclaration' || stmt.exportKind === 'type') continue;
            const line = stmt.loc.start.line;
            const decl = stmt.declaration;
            if (decl) {
                for (const local of [decl.id, ...(decl.declarations ?? []).map(item => item.id)]) {
                    if (local?.type === 'Identifier') names.set(local.name, {binding: local.name, line});
                }
            }
            for (const spec of stmt.specifiers) {
                if (spec.type !== 'ExportSpecifier' || spec.exportKind === 'type') continue;
                const binding = identifier(spec.local);
                const origin = stmt.source ? {module: stmt.source.value, binding} : imports.get(binding) ?? {binding};
                names.set(identifier(spec.exported), {...origin, line});
                if (stmt.source) named.add(JSON.stringify([line, stmt.source.value, binding]));
            }
        }
        return details;
    }
    function forwardingChain(start, name) {
        const chain = [];
        const visited = new Set();
        let module = start;
        let binding = name;
        for (let step = 0; step <= 4; step++) {
            const key = JSON.stringify([module, binding]);
            if (visited.has(key)) return null;
            visited.add(key);
            const link = namedExports(module)?.names.get(binding);
            if (!link) return null;
            if (!link.module) return module === file && link.binding === name && chain.length ? chain : null;
            if (step === 4 || link.binding === 'default') return null;
            chain.push({file: module, line: link.line});
            const next = resolveRelativeImportPath(index.rootDir, module, link.module, known);
            if (!next) return null;
            module = next;
            binding = link.binding;
        }
        return null;
    }
    const impacts = matches.slice(0, 6).map((match) => {
        const blast = computeBlastRadius(index, file, match.name, 24, match.parentClass);
        const importers = blast.importers.slice(0, 24).map(({ file: importer, line, rawImport }) => {
            const declaration = index.getFileAnalysis(importer)?.ast.imports
                .find((item) => item.line === line && item.raw === rawImport);
            const resolved = declaration
                ? resolveRelativeImportPath(index.rootDir, importer, declaration.module, known) : undefined;
            const resolved_file = resolved && known.has(resolved) ? resolved : null;
            const chain = resolved_file && resolved_file !== file && esm.test(file) && !match.parentClass
                && namedExports(importer)?.named.has(JSON.stringify([line, declaration.module, match.name]))
                ? forwardingChain(resolved_file, match.name) : null;
            return { file: importer, line, resolved_file,
                resolution: resolved_file === file ? 'direct_module' :
                    chain ? 'reexport_module' : resolved_file ? 'other_module' : 'unresolved',
                ...(chain ? {reexport_chain: chain} : {}) };
        });
        return {
            symbol: match.parentClass ? match.parentClass + '.' + match.name : match.name,
            line: match.line,
            end_line: match.endLine,
            total_call_sites: blast.totalCallSites,
            call_sites: blast.callSites.slice(0, 8).map(({ file, line, caller }) => ({ file, line, caller })),
            total_importers: blast.importers.length,
            importers,
        };
    });
    process.stdout.write(JSON.stringify({ impacts, truncated: matches.length > 6 }));
} else {
    const result = slicer.executeSurgicalRead(input);
    let output = input.anchorsOnly ? result.matchedSymbols.slice(0, 12) :
        Array.from(result.text).slice(0, 24001).join('');
    if (input.captureTargets && !input.anchorsOnly) {
        const resolved = new Set(result.resolvedKeys);
        const index = slicer.getWorkspaceAstIndex(input.rootDir);
        const keys = input.targets.filter((target) => {
            if (!resolved.has(target)) return false;
            const range = target.match(/^(.+?):(\\d+)-(\\d+)$/);
            return !range || Number(range[3]) <= (index.getFileAnalysis(range[1])?.ast.totalLines ?? 0);
        });
        output = {text: output, keys};
    }
    process.stdout.write(JSON.stringify(output));
}
"""


def assistant_enabled() -> bool:
    return os.environ.get("STARWISH_GRAPHFLOW_ASSISTANT") == "1" and status()["available"]


def _sync_snapshot(workspace: Path, git: str, preferred: tuple[str, ...] = ()) -> tuple[_Snapshot, bool, dict]:
    snapshot = _workspace_snapshot(workspace)
    try:
        partial, stats = snapshot.refresh(git, preferred)
    except Exception:
        # Never reuse a partially refreshed snapshot.
        _clear_snapshot()
        raise
    return snapshot, partial, stats


def _call_bridge(snapshot: _Snapshot, node: str, entry: Path, **options):
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "HOME", "USERPROFILE", "LANG"}}
    env.update(GRAPHFLOW_AUTO_CAPTURE="0", GRAPHFLOW_AST_CACHE="1", GRAPHFLOW_SEMANTIC_CACHE="0",
               GRAPHFLOW_NAME_INDEX="0", GRAPHFLOW_MAX_FILES=str(MAX_FILES))
    process = subprocess.run(
        [node, "-e", _BRIDGE, str(entry)], input=json.dumps({"rootDir": str(snapshot.root), **options}),
        capture_output=True, encoding="utf-8", cwd=snapshot.root, env=env, timeout=25, check=False,
    )
    if process.returncode:
        raise GraphFlowError("GraphFlow 查询失败，请检查引擎与解析器。", 503)
    try:
        return json.loads(process.stdout)
    except ValueError as exc:
        raise GraphFlowError("GraphFlow 返回了无效的查询结果。", 503) from exc


def validate_read(query: str = "", target: str = "", skeleton: bool = False,
                  impact_only: bool = False) -> tuple[str, str, str, str]:
    if not isinstance(query, str) or not isinstance(target, str) or not isinstance(skeleton, bool):
        raise GraphFlowError("查询参数格式不正确。")
    if not isinstance(impact_only, bool):
        raise GraphFlowError("查询参数格式不正确。")
    query, target = query.strip(), target.strip().replace("\\", "/")
    if bool(query) == bool(target) or len(query) > 300 or len(target) > 500:
        raise GraphFlowError("关键词与相对路径只能选填一项，查询最长 300 字，目标最长 500 字。")
    file, separator, symbol = target.partition("#")
    line_range = None
    if target and not separator:
        line_range = re.fullmatch(r"(.+):([1-9]\d{0,6})-([1-9]\d{0,6})", target)
        if line_range:
            file, start, end = line_range.groups()
            if not 0 <= int(end) - int(start) < 200:
                raise GraphFlowError("起止行必须递增，单次最多读取 200 行。")
    if target and (not _safe_source(file) or (separator and not re.fullmatch(r"[\w.$<>-]{1,160}", symbol))):
        raise GraphFlowError("目标必须是项目内受版本控制的源码相对路径，可加 #符号名或 :起始行-结束行。")
    if impact_only and not separator:
        raise GraphFlowError("影响分析需要使用 相对路径#符号名。")
    return query, target, file, symbol


def read(query: str = "", target: str = "", skeleton: bool = False, impact_only: bool = False,
         *, anchors_only: bool = False) -> dict:
    query, target, file, symbol = validate_read(query, target, skeleton, impact_only)
    return _read(_workspace(), query, [target] if target else [], [file] if file else [], skeleton, 6,
                 impact={"file": file, "symbol": symbol} if impact_only else None, anchors_only=anchors_only)


def read_batch(targets: list[str] | None = None, q: str = "", topK: int = 6, skeleton: bool = False,
               dedup: bool = False, topicId: str = "", rootDir: str = "", sessionId: str = "",
               resumeFromTurnId: str = "") -> dict:
    from . import graphflow_legacy_journal, graphflow_tasks, graphflow_trajectories

    if (not isinstance(dedup, bool) or not isinstance(topicId, str)
            or not isinstance(resumeFromTurnId, str) or not isinstance(skeleton, bool)):
        raise GraphFlowError("dedup、skeleton 或话题参数格式不正确。")
    topic, alias = topicId.strip(), resumeFromTurnId.strip()
    if topic and alias and topic != alias:
        raise GraphFlowError("topicId 与 resumeFromTurnId 必须指向同一任务。")
    topic = topic or alias
    native_topic = bool(topic and graphflow_tasks.valid_task_id(topic))
    legacy_enabled = bool(topic and graphflow_legacy_journal.topic_resume_enabled())
    if topic and not native_topic and not (legacy_enabled and graphflow_legacy_journal.valid_topic(topic)):
        raise GraphFlowError("话题编号必须是星愿项目任务 ID 或已启用的旧话题编号。")
    if not isinstance(sessionId, str) or (dedup and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", sessionId)):
        raise GraphFlowError("启用去重时请提供独立的会话标识（16 到 128 位字母、数字、下划线或连字符）。")
    if type(topK) is not int or not 1 <= topK <= MAX_READ_TARGETS:
        raise GraphFlowError(f"topK 必须是 1 到 {MAX_READ_TARGETS} 之间的整数。")
    if targets is None:
        targets = []
    if not isinstance(targets, list) or len(targets) > MAX_READ_TARGETS or not isinstance(q, str):
        raise GraphFlowError(f"请提供合法关键词及最多 {MAX_READ_TARGETS} 个目标。")
    if q.strip():
        q, _, _, _ = validate_read(query=q, skeleton=skeleton)
    else:
        q = ""
    if not targets and not q and not topic:
        targets = ["@brief"]
    resolved = [("", "@brief", "", "") if isinstance(target, str) and target.strip() in ("@brief", "brief")
                else validate_read(target=target, skeleton=skeleton) for target in targets]
    workspace = checked_workspace(rootDir)
    legacy_topic = False
    if topic:
        if native_topic and (graphflow_tasks.enabled() or not legacy_enabled):
            try:
                saved_target, saved_query = graphflow_tasks.topic_read_inputs(workspace, topic)
            except GraphFlowError as exc:
                if not legacy_enabled or exc.status_code != 404:
                    raise
                legacy_topic = True
        else:
            legacy_topic = True
        if legacy_topic:
            graphflow_legacy_journal.require_topic(workspace, topic)
            if (not resolved and not q) or any(row[1] == "@brief" for row in resolved):
                raise GraphFlowError("旧话题只接续显式源码查询，请提供源码 targets 或 q。")
        elif not resolved and not q:
            if saved_target:
                resolved = [validate_read(target=saved_target, skeleton=skeleton)]
            elif saved_query:
                q, _, _, _ = validate_read(query=saved_query, skeleton=skeleton)
            else:
                raise GraphFlowError("该任务没有保存代码定位，请提供 targets 或 q。")
    normalized = list(dict.fromkeys(row[1] for row in resolved))
    capture = (graphflow_trajectories.capture_enabled() and not q and not topic
               and 1 <= len(normalized) <= graphflow_trajectories.MAX_TARGETS
               and all(row[2] for row in resolved) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", sessionId))
    result = _read(workspace, q, normalized, list(dict.fromkeys(row[2] for row in resolved if row[2])), skeleton, topK,
                   include_snapshot_digest=dedup, capture_targets=bool(capture))
    capture_keys = result.pop("_capture_keys", [])
    snapshot_digest = result.pop("_snapshot_digest", b"")
    if dedup and _duplicate_read([str(workspace), sessionId, topic, q, normalized, skeleton, topK],
                                 result["text"], snapshot_digest):
        result.update(text="本会话的读取结果未变化。需要正文时可关闭 dedup 后重读。", deduplicated=True)
    elif capture and capture_keys == normalized:
        graphflow_trajectories.record(workspace, sessionId, normalized)
    if topic:
        notice = "已匹配旧话题；下方仅为当前源码。" if legacy_topic else "星愿项目任务已定位。"
        result.update(text=f"// [Topic Resume: {topic}] {notice}\n\n{result['text']}",
                      topic_id=topic, topic_source="legacy_journal" if legacy_topic else "starwish_task")
    return result


def _duplicate_read(identity: list, body: str, snapshot_digest: bytes) -> bool:
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()).digest()
    fingerprint = hashlib.sha256(snapshot_digest + body.encode("utf-8")).digest()
    with _read_dedup_lock:
        now = time.monotonic()
        while _read_dedup and next(iter(_read_dedup.values()))[0] <= now:
            _read_dedup.popitem(last=False)
        previous = _read_dedup.pop(key, None)
        _read_dedup[key] = (now + _READ_DEDUP_TTL, fingerprint)
        if len(_read_dedup) > _READ_DEDUP_LIMIT:
            _read_dedup.popitem(last=False)
        return previous is not None and previous[1] == fingerprint


def _read(workspace: Path, query: str, targets: list[str], files: list[str], skeleton: bool, top_k: int,
          *, impact: dict | None = None, anchors_only: bool = False, include_snapshot_digest: bool = False,
          capture_targets: bool = False) -> dict:
    node, entry, git = _engine()
    if not _gate.acquire(blocking=False):
        raise GraphFlowError("已有代码查询运行中，请稍后再试。", 429)
    try:
        snapshot, partial, index_stats = _sync_snapshot(workspace, git, tuple(files))
        if any(file not in snapshot.files for file in files):
            reason = "目标不在当前可查询的受版本控制文件范围内。"
            if partial:
                reason += "索引仅部分覆盖，未找到不代表文件不存在。"
            raise GraphFlowError(reason, 404)
        if not snapshot.files:
            raise GraphFlowError("当前工作区没有可查询的受版本控制文件。", 404)
        options = {"query": query or None, "targets": targets,
                   "topK": top_k, "includeCallers": not anchors_only, "includeSkeleton": skeleton,
                   "anchorsOnly": anchors_only}
        if impact:
            options["impact"] = impact
        if capture_targets:
            options["captureTargets"] = True
        result = _call_bridge(snapshot, node, entry, **options)
        if anchors_only and isinstance(result, list):
            return {"matches": result, "index": index_stats}
        if impact and isinstance(result, dict) and isinstance(result.get("impacts"), list):
            impacts = result["impacts"]
            if not impacts:
                raise GraphFlowError("目标文件中没有找到指定符号。", 404)
            return {"file": impact["file"], "impacts": impacts, "truncated": result["truncated"],
                    "provider": PROVIDER, "index": index_stats, "analysis": "static_heuristic"}
        keys = []
        if capture_targets and isinstance(result, dict):
            keys, result = result.get("keys", []), result.get("text")
        if not isinstance(result, str):
            raise GraphFlowError("GraphFlow 返回了无效的查询结果。", 503)
        if "@brief" in targets:
            from . import graphflow_brief

            result = graphflow_brief.render(workspace) + "\n\n" + result
        if len(result) > 24000:
            result = result[:24000] + "\n…（输出已截断，请缩小范围）"
        if partial:
            result += "\n（部分文件超出大小或数量限制，结果可能不完整。）"
        response = {"text": result, "provider": PROVIDER, "index": index_stats}
        if capture_targets:
            response["_capture_keys"] = keys
        if include_snapshot_digest:
            digest = hashlib.sha256()
            for path, file_digest in sorted(snapshot.files.items()):
                digest.update(path.encode("utf-8") + b"\0" + file_digest)
            response["_snapshot_digest"] = digest.digest()
        return response
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("代码查询超时，请缩小范围后再试。", 504) from exc
    except (OSError, UnicodeError) as exc:
        raise GraphFlowError("无法读取工作区或启动 GraphFlow。", 503) from exc
    finally:
        _gate.release()


def impact(target: str) -> dict:
    return read(target=target, impact_only=True)


def dependency_graph(target: str = "", node_limit: int = 20, edge_limit: int = 32, root_dir: str = "") -> dict:
    from . import graphflow_history

    if type(node_limit) is not int or not 1 <= node_limit <= GRAPH_NODE_LIMIT:
        raise GraphFlowError(f"节点上限须在 1 到 {GRAPH_NODE_LIMIT} 之间。")
    if type(edge_limit) is not int or not 0 <= edge_limit <= GRAPH_EDGE_LIMIT:
        raise GraphFlowError(f"边上限须在 0 到 {GRAPH_EDGE_LIMIT} 之间。")
    if not isinstance(target, str):
        raise GraphFlowError("目标须为项目内的源码相对路径。")
    target = target.strip().replace("\\", "/")
    if target and not _safe_source(target):
        raise GraphFlowError("目标须为项目内的源码相对路径。")
    workspace = checked_workspace(root_dir)
    node, entry, git = _engine()
    if not _gate.acquire(blocking=False):
        raise GraphFlowError("已有代码查询或索引运行中，请稍后再试。", 429)
    try:
        snapshot, partial, stats = _sync_snapshot(workspace, git, (target,) if target else ())
        if target and target not in snapshot.files:
            suffix = "索引仅部分覆盖，未找到不代表文件不存在。" if partial else ""
            raise GraphFlowError("目标不在当前可查询的文件范围内。" + suffix, 404)
        history_status = "ready"
        try:
            history = graphflow_history.mine(workspace, target=target, allowed_files=set(snapshot.files))
        except GraphFlowError as exc:
            history_status = f"unavailable_{exc.status_code}"
            history = {"pairs": [], "more": False}
        result = _call_bridge(snapshot, node, entry, graph={
            "target": target, "nodeLimit": node_limit, "edgeLimit": edge_limit, "files": list(snapshot.files),
            "cochange": history["pairs"],
        })
        if not isinstance(result, dict) or not isinstance(result.get("nodes"), list) or not isinstance(result.get("edges"), list):
            raise GraphFlowError("GraphFlow 返回了无效的图谱。", 503)
        if target and not result["nodes"]:
            raise GraphFlowError("目标在快照中，但固定引擎未生成其代码节点。", 404)
        return {**result, "target": target, "index": stats, "provider": PROVIDER, "analysis": "static_heuristic",
                "history": {"status": history_status, "more": history["more"], "commit_limit": graphflow_history.MAX_COMMITS}}
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("图谱构建超时。", 504) from exc
    except (OSError, UnicodeError) as exc:
        raise GraphFlowError("无法读取工作区或启动 GraphFlow。", 503) from exc
    finally:
        _gate.release()


def locate_edit_spans(workspace: Path, file: str, symbols: list[str], original: bytes) -> dict[str, tuple[int, int]]:
    node, entry, git = _engine()
    if not _gate.acquire(blocking=False):
        raise GraphFlowError("已有代码查询运行中，请稍后再试。", 429)
    try:
        snapshot, _, _ = _sync_snapshot(workspace, git)
        if snapshot.files.get(file) != hashlib.sha256(original).digest():
            raise GraphFlowError("符号索引与原文不一致，请重新读取文件。", 409)
        matches = _call_bridge(snapshot, node, entry, locate={"file": file, "symbols": symbols})
        if not isinstance(matches, list) or len(matches) != len(symbols):
            raise GraphFlowError("GraphFlow 返回了无效的符号范围。", 503)
        spans = {}
        for symbol, candidates in zip(symbols, matches):
            if not isinstance(candidates, list):
                raise GraphFlowError("GraphFlow 返回了无效的符号范围。", 503)
            if not candidates:
                raise GraphFlowError("目标文件中没有找到指定符号。", 404)
            if len(candidates) != 1:
                raise GraphFlowError("符号不唯一；请指定类名或使用行范围。", 409)
            span = candidates[0]
            if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                    or not 1 <= span[0] <= span[1]):
                raise GraphFlowError("GraphFlow 返回了无效的符号范围。", 503)
            spans[symbol] = tuple(span)
        return spans
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("编辑符号定位超时。", 504) from exc
    except (OSError, UnicodeError) as exc:
        raise GraphFlowError("无法读取工作区或定位编辑符号。", 503) from exc
    finally:
        _gate.release()


def rename_candidates(workspace: Path, file: str, symbol: str, rename_to: str,
                      max_files: int) -> dict[str, tuple[bytes, bytes]]:
    node, entry, git = _engine()
    if not _gate.acquire(blocking=False):
        raise GraphFlowError("已有代码查询运行中，请稍后再试。", 429)
    scratch = None
    try:
        # The upstream rename writes files; never use the shared read snapshot.
        scratch = _Snapshot(workspace)
        partial, _ = scratch.refresh(git)
        if partial:
            raise GraphFlowError("代码快照达到容量上限，无法安全生成重命名预览。", 413)
        if file not in scratch.files:
            raise GraphFlowError("符号文件不在可查询的受版本控制范围内。", 404)
        result = _call_bridge(scratch, node, entry, rename={"file": file, "symbol": symbol, "renameTo": rename_to})
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise GraphFlowError("引擎返回了无效的重命名结果。", 503)
        if not result["ok"]:
            reason = result.get("reason")
            suffix = f"（{reason}）" if isinstance(reason, str) and re.fullmatch(r"[a-z-]{1,50}", reason) else ""
            raise GraphFlowError(f"引擎无法安全生成重命名候选{suffix}。请核对符号或使用显式编辑。", 409)
        touched = result.get("touched")
        if (not isinstance(touched, list) or any(not isinstance(path, str) or path not in scratch.files for path in touched)
                or len(touched) != len(set(touched)) or file not in touched):
            raise GraphFlowError("重命名候选包含无效文件。", 503)
        if len(touched) > max_files:
            raise GraphFlowError(f"重命名涉及 {len(touched)} 个文件，超过单次审批上限 {max_files}。", 413)
        root = scratch.root.resolve()
        changes = {}
        for path, digest in scratch.files.items():
            candidate = root / path
            if candidate.resolve(strict=True) != candidate or not stat.S_ISREG(candidate.stat().st_mode):
                raise GraphFlowError("重命名副本中的文件不再有效。", 503)
            with candidate.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise GraphFlowError("重命名候选文件超过大小限制。", 413)
            if hashlib.sha256(data).digest() != digest:
                changes[path] = (digest, data)
        if set(changes) != set(touched):
            raise GraphFlowError("重命名候选清单与实际差异不一致。", 503)
        return changes
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("重命名分析超时。", 504) from exc
    except (OSError, RuntimeError) as exc:
        raise GraphFlowError("无法读取或验证重命名副本。", 503) from exc
    finally:
        try:
            if scratch is not None:
                scratch.close()
        finally:
            _gate.release()


def affected_tests(workspace: Path, changes: list[str], *, explicit: bool = False) -> dict:
    if not changes:
        return {"tests": [], "partial": False}
    node, entry, git = _engine()
    if not _gate.acquire(blocking=False):
        raise GraphFlowError("已有代码查询运行中，请稍后再试。", 429)
    try:
        limited = changes[:MAX_AFFECTED_INPUTS]
        snapshot, partial, _ = _sync_snapshot(workspace, git, tuple(limited))
        missing = any(path not in snapshot.files for path in limited)
        if explicit and missing:
            reason = "显式改动必须是可查询的受版本控制文件。"
            if partial:
                reason += "索引仅部分覆盖，请缩小改动范围。"
            raise GraphFlowError(reason, 404)
        tests = _call_bridge(snapshot, node, entry, changedFiles=limited)
        if not isinstance(tests, list) or any(not isinstance(path, str) or path not in snapshot.files for path in tests):
            raise GraphFlowError("GraphFlow 返回了无效的测试定位结果。", 503)
        return {"tests": sorted(set(tests)), "partial": partial or missing or len(changes) > len(limited)}
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("测试定位超时。", 504) from exc
    except (OSError, UnicodeError) as exc:
        raise GraphFlowError("无法读取工作区或启动测试定位。", 503) from exc
    finally:
        _gate.release()


def reindex(mode: str = "incremental", file_path: str = "", root_dir: str = "") -> dict:
    if not isinstance(mode, str) or mode not in {"incremental", "full"} or not isinstance(file_path, str):
        raise GraphFlowError("索引模式只能是 incremental 或 full。")
    file = file_path.strip().replace("\\", "/")
    if file and (mode == "full" or not _safe_source(file)):
        raise GraphFlowError("单文件索引须使用工作区相对路径，不能同时全量重建。")
    workspace = checked_workspace(root_dir)
    node, entry, git = _engine()
    if not _gate.acquire(blocking=False):
        raise GraphFlowError("已有代码查询或索引运行中，请稍后再试。", 429)
    try:
        if mode == "full":
            _clear_snapshot()
        snapshot, _, stats = _sync_snapshot(workspace, git, (file,) if file else ())
        if file and file not in snapshot.files:
            raise GraphFlowError("目标不在可索引的受版本控制文件范围内。", 404)
        files = [file] if file else [name for name in snapshot.files if name not in snapshot.warmed]
        parsed = _call_bridge(snapshot, node, entry, warmFiles=files) if files else []
        if not isinstance(parsed, list) or any(not isinstance(name, str) or name not in snapshot.files for name in parsed):
            raise GraphFlowError("GraphFlow 返回了无效的索引结果。", 503)
        snapshot.warmed.update({name: snapshot.files[name] for name in parsed})
        return {"mode": "file" if file else mode, "index": stats, "parsed_or_reused": len(parsed),
                "unparsed": len(files) - len(parsed), "cache_scope": "process", "provider": PROVIDER}
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("索引超时，请缩小范围后重试。", 504) from exc
    except (OSError, UnicodeError) as exc:
        raise GraphFlowError("无法读取工作区或启动 GraphFlow。", 503) from exc
    finally:
        _gate.release()
