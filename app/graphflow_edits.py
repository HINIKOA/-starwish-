"""Read-only edit proposals for the explicitly configured code workspace."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, graphflow
from .graphflow import GraphFlowError

MAX_BLOCK = 12_000
MAX_DIFF = 24_000
TTL = 600
MAX_PENDING = 32
MAX_COMPAT_EDITS = 8
MAX_EDIT_FILES = 4


@dataclass(frozen=True)
class _Receipt:
    workspace: Path
    path: str
    deadline: float
    sources: tuple[tuple[str, str], ...]


_receipts: dict[str, _Receipt] = {}
_lock = threading.Lock()


def _read_tracked(workspace: Path, path: str) -> bytes:
    relative = graphflow._safe_source(path)
    if relative is None:
        raise GraphFlowError("目标必须是工作区内受版本控制的源码相对路径。")
    git = shutil.which("git")
    if not git:
        raise GraphFlowError("编辑预览需要 Git。", 503)
    try:
        entries = subprocess.run(
            [git, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", path],
            cwd=workspace, capture_output=True, timeout=10, check=False,
        )
        if entries.returncode:
            raise GraphFlowError("无法读取 Git 文件清单。", 503)
        rows = entries.stdout.rstrip(b"\0").split(b"\0")
        meta, _, name = rows[0].partition(b"\t")
        if len(rows) != 1 or os.fsdecode(name) != path or not re.fullmatch(rb"100(?:644|755) [0-9a-f]+ 0", meta):
            raise GraphFlowError("目标必须是已跟踪、无冲突的常规文件。", 404)
        source = workspace.joinpath(*relative.parts)
        resolved = source.resolve(strict=True)
        if resolved != source or resolved.is_relative_to(config.SOUL_DIR.resolve()):
            raise GraphFlowError("无法读取目标：路径指向工作区外或灵魂包。", 404)
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise GraphFlowError("不支持非常规文件或硬链接。", 404)
        if info.st_size > graphflow.MAX_FILE_BYTES:
            raise GraphFlowError("目标文件超出预览大小限制。", 413)
        with source.open("rb") as handle:
            data = handle.read(graphflow.MAX_FILE_BYTES + 1)
        if len(data) > graphflow.MAX_FILE_BYTES:
            raise GraphFlowError("目标文件超出预览大小限制。", 413)
        return data
    except FileNotFoundError as exc:
        raise GraphFlowError("目标文件已不存在。", 404) from exc
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("读取 Git 文件清单超时。", 504) from exc
    except (OSError, RuntimeError) as exc:
        raise GraphFlowError("无法读取目标文件。", 503) from exc


def _block(text: str) -> list[str]:
    if not isinstance(text, str) or len(text) > MAX_BLOCK or "\0" in text:
        raise GraphFlowError(f"原始文本和替换文本各最多 {MAX_BLOCK} 字，不支持 NUL。")
    normal = text.replace("\r\n", "\n")
    if "\r" in normal:
        raise GraphFlowError("不支持单独的 CR 换行。")
    return normal.removesuffix("\n").split("\n") if normal else []


def _file_text(data: bytes) -> tuple[str, str, bool]:
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8-sig" if bom else "utf-8")
    except UnicodeDecodeError as exc:
        raise GraphFlowError("仅支持 UTF-8 文件。") from exc
    lf = text.replace("\r\n", "\n")
    if "\0" in lf or "\r" in lf or ("\r\n" in text and text.count("\n") != text.count("\r\n")):
        raise GraphFlowError("不支持二进制文件或混合换行。")
    return lf, "\r\n" if "\r\n" in text else "\n", bom


def _diff(before: str, after: str, path: str) -> str:
    # difflib omits the conventional marker for a last line without a newline.
    def lf_lines(text: str) -> list[str]:
        items = text.split("\n")
        return [line + "\n" for line in items[:-1]] + ([items[-1]] if items[-1] else [])

    lines = difflib.unified_diff(lf_lines(before), lf_lines(after),
                                fromfile=f"a/{path}", tofile=f"b/{path}")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def preview(path: str, start_line: int, end_line: int, expected: str, replacement: str) -> dict:
    if not isinstance(path, str) or type(start_line) is not int or type(end_line) is not int:
        raise GraphFlowError("文件路径与行号格式不正确。")
    if not 1 <= start_line <= end_line or end_line - start_line >= 200:
        raise GraphFlowError("起止行必须递增，单次最多替换 200 行。")
    want, change = _block(expected) or [""], _block(replacement)
    workspace = graphflow._workspace()
    path = path.strip().replace("\\", "/")
    original = _read_tracked(workspace, path)
    before, newline, bom = _file_text(original)
    lines = before.removesuffix("\n").split("\n") if before else []
    if end_line > len(lines) or lines[start_line - 1:end_line] != want:
        raise GraphFlowError("原始文本与当前文件不一致。请重新读取目标行。", 409)
    updated = lines[:start_line - 1] + change + lines[end_line:]
    after = "\n".join(updated) + ("\n" if updated and before.endswith("\n") else "")
    return _issue(workspace, path, original, before, after, newline, bom)


def _compat_path(file: str) -> str:
    if not isinstance(file, str):
        raise GraphFlowError("file 须为源码相对路径。")
    path = file.strip().replace("\\", "/").removeprefix("./")
    if not graphflow._safe_source(path):
        raise GraphFlowError("file 须为源码相对路径。")
    return path


def _unique_span(before: str, want: list[str]) -> tuple[int, int]:
    if not before or not any(want):
        raise GraphFlowError("按原文定位时，请提供非空的完整行。")
    padded = "\n" + before.removesuffix("\n") + "\n"
    needle = "\n" + "\n".join(want) + "\n"
    at = padded.find(needle)
    if at < 0 or padded.find(needle, at + 1) >= 0:
        raise GraphFlowError("原文未找到或不唯一，请重新读取并指定行范围。", 409)
    start = padded.count("\n", 0, at) + 1
    return start, start + len(want) - 1


def compat_preview(file: str = "", symbol: str = "", startLine: int | None = None, endLine: int | None = None,
                   oldCode: str | None = None, newCode: str | None = None, renameTo: str = "",
                   edits: list[dict] | None = None, rootDir: str = "", *, preview_only: bool = False) -> dict:
    if type(preview_only) is not bool:
        raise GraphFlowError("preview_only 必须是布尔值。")
    if not isinstance(renameTo, str):
        raise GraphFlowError("renameTo 须为新符号名。")
    if renameTo:
        if any(value is not None for value in (startLine, endLine, oldCode, newCode, edits)):
            raise GraphFlowError("重命名不能与替换参数混用。")
        return _rename_preview(file, symbol, renameTo, rootDir, preview_only)
    if edits is None:
        edits = [{"file": file, "symbol": symbol, "startLine": startLine, "endLine": endLine,
                  "oldCode": oldCode, "newCode": newCode}]
    elif (not isinstance(edits, list) or not 1 <= len(edits) <= MAX_COMPAT_EDITS
          or symbol != "" or any(value is not None for value in (startLine, endLine, oldCode, newCode))):
        raise GraphFlowError(f"批量预览最多 {MAX_COMPAT_EDITS} 处，不能混用顶层编辑参数。")
    workspace = graphflow.checked_workspace(rootDir)
    return _submit(workspace, _prepare_parts(workspace, edits, file), preview_only=preview_only)


def _prepare_parts(workspace: Path, edits: list[dict], file: str = "") -> list[dict]:
    default = _compat_path(file) if file != "" else ""
    grouped = {}
    spellings = {}
    budget_old = budget_new = 0
    for entry in edits:
        if not isinstance(entry, dict) or set(entry) - {"file", "symbol", "startLine", "endLine", "oldCode", "newCode"}:
            raise GraphFlowError("编辑项格式无效。")
        path = _compat_path(entry.get("file", default))
        if spellings.setdefault(path.casefold(), path) != path:
            raise GraphFlowError("同一文件不能使用不同大小写的路径。")
        name, start, end = entry.get("symbol", ""), entry.get("startLine"), entry.get("endLine")
        if not isinstance(name, str):
            raise GraphFlowError("符号名称格式无效。")
        name = name.strip().replace("::", ".")
        if name:
            if start is not None or end is not None or not re.fullmatch(r"[\w$<>-]+(?:\.[\w$<>-]+)?", name):
                raise GraphFlowError("只能选择符号或行范围，不支持混用。")
            graphflow.validate_read(target=f"{path}#{name}")
        elif start is not None or end is not None:
            if type(start) is not int or type(end) is not int or not 1 <= start <= end < start + 200:
                raise GraphFlowError("起止行必须递增，单次最多替换 200 行。")
        old, new = entry.get("oldCode"), entry.get("newCode")
        want, change = _block(old) or [""], _block(new)
        budget_old += len(old)
        budget_new += len(new)
        if max(budget_old, budget_new) > MAX_BLOCK or len(want) > 200:
            raise GraphFlowError("合并编辑文本过大，请缩小范围。", 413)
        grouped.setdefault(path, []).append((name, start, end, want, change))
    if len(grouped) > MAX_EDIT_FILES or sum(len(hunk[3]) for hunks in grouped.values() for hunk in hunks) > 200:
        raise GraphFlowError(f"批量预览最多 {MAX_EDIT_FILES} 个文件、共 200 个原始行。", 413)
    return [_prepare_compat(workspace, path, hunks) for path, hunks in sorted(grouped.items())]


def _rename_preview(file: str, symbol: str, rename_to: str, root_dir: str, preview_only: bool) -> dict:
    path = _compat_path(file)
    ident = r"(?:[^\W\d]|\$)[\w$]*"
    if not isinstance(symbol, str) or not re.fullmatch(ident + r"(?:(?:\.|::)" + ident + ")?", symbol.strip()):
        raise GraphFlowError("重命名须提供符号名，可带一个类名限定。")
    symbol = symbol.strip().replace("::", ".")
    if (not re.fullmatch(ident, rename_to) or len(rename_to) > 160 or symbol.rsplit(".", 1)[-1] == rename_to):
        raise GraphFlowError("重命名须提供不同且有效的新标识符。")
    graphflow.validate_read(target=f"{path}#{symbol}")
    workspace = graphflow.checked_workspace(root_dir)
    _read_tracked(workspace, path)
    candidates = graphflow.rename_candidates(workspace, path, symbol, rename_to, MAX_EDIT_FILES)
    parts = []
    for relative, (expected, candidate) in sorted(candidates.items()):
        original = _read_tracked(workspace, relative)
        if hashlib.sha256(original).digest() != expected:
            raise GraphFlowError("文件在重命名预览期间已变化，请重新生成。", 409)
        before, newline, bom = _file_text(original)
        after, _, _ = _file_text(candidate)
        parts.append(_prepare(relative, original, before, after, newline, bom))
    return {**_submit(workspace, parts, preview_only=preview_only), "mode": "rename", "symbol": symbol, "rename_to": rename_to,
            "analysis": "static_snapshot"}


def _prepare_compat(workspace: Path, path: str, hunks: list) -> dict:
    original = _read_tracked(workspace, path)
    before, newline, bom = _file_text(original)
    lines = before.removesuffix("\n").split("\n") if before else []
    symbols = list(dict.fromkeys(hunk[0] for hunk in hunks if hunk[0]))
    resolved = graphflow.locate_edit_spans(workspace, path, symbols, original) if symbols else {}
    patches = []
    for name, start, end, want, change in hunks:
        if name:
            start, end = resolved[name]
        elif start is None:
            start, end = _unique_span(before, want)
        if end > len(lines) or lines[start - 1:end] != want:
            raise GraphFlowError("原始文本与目标范围不一致，请重新读取。", 409)
        patches.append((start, end, change))
    patches.sort(key=lambda patch: patch[0])
    if sum(end - start + 1 for start, end, _ in patches) > 200:
        raise GraphFlowError("合并预览最多替换 200 行。", 413)
    if any(left[1] >= right[0] for left, right in zip(patches, patches[1:])):
        raise GraphFlowError("批量编辑范围重叠，请合并为一项。", 409)
    for start, end, change in reversed(patches):
        lines[start - 1:end] = change
    after = "\n".join(lines) + ("\n" if lines and before.endswith("\n") else "")
    return {**_prepare(path, original, before, after, newline, bom),
            "spans": [{"start_line": start, "end_line": end} for start, end, _ in patches]}


def _issue(workspace: Path, path: str, original: bytes, before: str, after: str, newline: str, bom: bool) -> dict:
    return _submit(workspace, [_prepare(path, original, before, after, newline, bom)])


def _prepare(path: str, original: bytes, before: str, after: str, newline: str, bom: bool) -> dict:
    if before == after:
        raise GraphFlowError("替换内容与原文相同，没有可预览的变更。")
    diff = _diff(before, after, path)
    try:
        proposed = after.replace("\n", newline).encode("utf-8-sig" if bom else "utf-8")
    except UnicodeEncodeError as exc:
        raise GraphFlowError("替换文本必须是有效的 UTF-8。") from exc
    if len(diff) > MAX_DIFF or len(proposed) > graphflow.MAX_FILE_BYTES:
        raise GraphFlowError("变更过大，请缩小范围。", 413)
    return {"path": path, "before": original, "after": proposed, "diff": diff,
            "base": hashlib.sha256(original).hexdigest(), "result": hashlib.sha256(proposed).hexdigest()}


def manifest_digest(parts: list, key: str) -> str:
    pairs = sorted((part["path"], part[key]) for part in parts)
    if len(pairs) == 1:
        return pairs[0][1]
    return hashlib.sha256(json.dumps(pairs, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _describe(parts: list[dict]) -> dict:
    diff = "".join(part["diff"] for part in parts)
    if len(diff) > MAX_DIFF:
        raise GraphFlowError("合并差异过大，请缩小范围。", 413)
    path = parts[0]["path"]
    base, result = (manifest_digest(parts, key) for key in ("base", "result"))
    report = {"path": path, "diff": diff, "base_sha256": base, "result_sha256": result, "written": False}
    if len(parts) > 1:
        report["files"] = [{key: part[key] for key in ("path", "spans") if key in part} for part in parts]
    elif "spans" in parts[0]:
        report["spans"] = parts[0]["spans"]
    return report


def _submit(workspace: Path, parts: list[dict], *, preview_only: bool = False) -> dict:
    report = _describe(parts)
    if preview_only:
        return {**report, "state": "preview_only", "queued": False}
    receipt = secrets.token_hex(16)
    report.update(preview_id=receipt, expires_in_seconds=TTL)
    from . import graphflow_edit_queue
    if graphflow_edit_queue.enabled():
        graphflow_edit_queue.enqueue_parts(workspace, report, parts)
        return {**report, "state": "pending", "queued": True}
    now = time.monotonic()
    with _lock:
        for key in [key for key, value in _receipts.items() if value.deadline <= now]:
            del _receipts[key]
        if len(_receipts) >= MAX_PENDING:
            del _receipts[next(iter(_receipts))]
        _receipts[receipt] = _Receipt(workspace, report["path"], now + TTL,
                                      tuple((part["path"], part["base"]) for part in parts))
    return report


def check(preview_id: str) -> dict:
    if not isinstance(preview_id, str) or not re.fullmatch(r"[0-9a-f]{32}", preview_id):
        raise GraphFlowError("预览编号格式不正确。")
    workspace = graphflow._workspace()
    from . import graphflow_edit_queue
    if graphflow_edit_queue.enabled():
        return graphflow_edit_queue.check(workspace, preview_id)
    with _lock:
        receipt = _receipts.get(preview_id)
        if receipt is None or receipt.workspace != workspace:
            raise GraphFlowError("当前工作区或进程中没有该预览。", 404)
        if receipt.deadline <= time.monotonic():
            del _receipts[preview_id]
            raise GraphFlowError("预览已过期，请重新生成。", 410)
    try:
        stale = any(hashlib.sha256(_read_tracked(workspace, path)).hexdigest() != digest
                    for path, digest in receipt.sources)
    except GraphFlowError as exc:
        if exc.status_code not in {404, 413}:
            raise
        stale = True
    with _lock:
        if _receipts.get(preview_id) is not receipt:
            raise GraphFlowError("预览已失效，请重新生成。", 409)
        if stale or receipt.deadline <= time.monotonic():
            del _receipts[preview_id]
            raise GraphFlowError("原文件已改变，请重新生成预览。" if stale else "预览已过期，请重新生成。",
                                 409 if stale else 410)
    result = {"preview_id": preview_id, "path": receipt.path, "current": True, "written": False}
    if len(receipt.sources) > 1:
        result["paths"] = [path for path, _ in receipt.sources]
    return result
