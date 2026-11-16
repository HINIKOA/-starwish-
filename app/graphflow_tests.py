"""Operator-approved execution of trusted, configured project test profiles."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

from . import config, graphflow, graphflow_legacy_observations
from .graphflow import GraphFlowError
from .graphflow_edit_queue import state_path

MAX_LOG_BYTES = 128 * 1024
MAX_PAGE_CHARS = 4000
MAX_LOG_LINES = 120
_RETENTION_SECONDS = 86400
_RETENTION_RUNS = 30
_LOG_HANDLE_PREFIX = "swtest:"
_OBSERVATION_PREFIX = "gfo:"
PROFILES_ENV = "STARWISH_GRAPHFLOW_TEST_PROFILES"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}")


def enabled() -> bool:
    return bool(os.environ.get(PROFILES_ENV))


def agent_logs_enabled() -> bool:
    return os.environ.get("STARWISH_GRAPHFLOW_TEST_AGENT_LOGS") == "1"


def _profiles() -> dict:
    raw = os.environ.get(PROFILES_ENV, "")
    if not raw:
        return {}
    try:
        profiles = json.loads(raw)
        if not isinstance(profiles, dict) or len(profiles) > 20:
            raise ValueError
        result = {}
        for name, profile in profiles.items():
            if not _ID.fullmatch(name) or not isinstance(profile, dict) or set(profile) - {"argv", "paths", "timeout"}:
                raise ValueError
            argv, paths, timeout = profile.get("argv"), profile.get("paths", []), profile.get("timeout", 30)
            if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                    or any(not isinstance(x, str) or len(x) > 500 or "\0" in x for x in argv)
                    or not isinstance(paths, list) or len(paths) > 32
                    or any(not isinstance(x, str) or not 1 <= len(x) <= 200 for x in paths)
                    or type(timeout) is not int or not 1 <= timeout <= 120):
                raise ValueError
            exe = Path(argv[0])
            if (not exe.is_absolute() or exe.suffix.lower() in {".bat", ".cmd"}
                    or exe.stem.casefold() in {"cmd", "powershell", "pwsh", "sh", "bash", "zsh", "fish", "dash"}
                    or not exe.is_file()):
                raise ValueError
            result[name] = {"argv": argv, "paths": paths, "timeout": timeout}
        return result
    except (ValueError, OSError, TypeError) as exc:
        raise GraphFlowError(f"{PROFILES_ENV} 配置无效，请检查测试项和绝对程序路径。", 503) from exc


def _changed(workspace: Path) -> list[str]:
    git = shutil.which("git")
    if not git:
        raise GraphFlowError("匹配测试需要 Git。", 503)
    files = set()
    try:
        for flags in ([], ["--cached"]):
            output = subprocess.run(
                [git, "diff", *flags, "--name-only", "--no-ext-diff", "--no-renames", "-z", "--"],
                cwd=workspace, capture_output=True, timeout=10, check=True,
            ).stdout
            for raw in output.split(b"\0"):
                try:
                    path = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if graphflow._safe_source(path):
                    files.add(path)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GraphFlowError("无法读取 Git 改动清单。", 503) from exc
    return sorted(files)


def _approval_inputs(workspace: Path, changes: list[str]) -> list:
    if not changes:
        return []
    if len(changes) > graphflow.MAX_FILES:
        raise GraphFlowError("改动文件过多，无法生成测试审批。", 413)
    git = shutil.which("git")
    if not git:
        raise GraphFlowError("测试审批需要 Git。", 503)
    try:
        index = subprocess.run([git, "ls-files", "--stage", "-z"], cwd=workspace, capture_output=True,
                               check=True, timeout=10).stdout
        entries = {}
        changed_bytes = {path.encode("utf-8") for path in changes}
        for row in index.split(b"\0"):
            meta, _, raw = row.partition(b"\t")
            if raw in changed_bytes:
                if raw in entries or not re.fullmatch(rb"100(?:644|755) [0-9a-f]+ 0", meta):
                    raise GraphFlowError("测试审批不支持冲突或非常规的改动文件。", 409)
                entries[raw] = meta.decode("ascii")
        inputs = []
        total = 0
        soul = config.SOUL_DIR.resolve()
        for path in changes:
            stage = entries.get(path.encode("utf-8"))
            if stage is None:
                inputs.append([path, None, None])
                continue
            source = workspace.joinpath(*path.split("/"))
            try:
                resolved = source.resolve(strict=False)
                if resolved != source or resolved.is_relative_to(soul):
                    raise GraphFlowError("测试审批无法读取链接或灵魂包中的改动文件。", 409)
                metadata = source.stat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                    raise GraphFlowError("测试审批不支持非常规文件或硬链接。", 409)
                allowance = min(graphflow.MAX_FILE_BYTES, graphflow.MAX_BYTES - total)
                if metadata.st_size > allowance:
                    raise GraphFlowError("改动超出测试审批的内容校验预算。", 413)
                with source.open("rb") as handle:
                    data = handle.read(allowance + 1)
                if len(data) > allowance:
                    raise GraphFlowError("改动超出测试审批的内容校验预算。", 413)
                total += len(data)
                digest = hashlib.sha256(data).hexdigest()
            except FileNotFoundError:
                digest = None
            inputs.append([path, stage, digest])
        return inputs
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise GraphFlowError("无法校验测试审批的 Git 改动。", 503) from exc


def plan(workspace: Path, *, operator: bool = False) -> dict:
    profiles = _profiles()
    if not profiles:
        return {"profiles": [], "changed_count": 0}
    changes = _changed(workspace)
    direct = {name: _match(changes, spec["paths"]) for name, spec in profiles.items()}
    impact = {"tests": [], "partial": False}
    impact_status = "not_needed"
    if changes and any(spec["paths"] and not direct[name] for name, spec in profiles.items()):
        try:
            impact = graphflow.affected_tests(workspace, changes)
            impact_status = "partial" if impact["partial"] else "ready"
        except GraphFlowError as exc:
            impact_status = f"unavailable_{exc.status_code}"
    items = []
    approval_inputs = None
    for name, spec in sorted(profiles.items()):
        via_impact = _match(impact["tests"], spec["paths"]) if not direct[name] else []
        matching = direct[name] or via_impact
        if spec["paths"] and not matching:
            continue
        if approval_inputs is None:
            approval_inputs = _approval_inputs(workspace, changes)
        approval = hashlib.sha256(json.dumps([str(workspace), name, spec, approval_inputs, via_impact],
                                               ensure_ascii=True).encode()).hexdigest()
        item = {"profile": name, "matched": matching[:8], "matched_count": len(matching),
                "via_impact": bool(via_impact), "timeout": spec["timeout"], "approval_sha256": approval}
        if operator:
            item["argv"] = spec["argv"]
        items.append(item)
    return {"profiles": items, "changed_count": len(changes), "impact_status": impact_status}


def _match(paths: list[str], patterns: list[str]) -> list[str]:
    return [path for path in paths if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)]


@contextmanager
def _db(workspace: Path):
    try:
        with closing(sqlite3.connect(state_path(workspace, "-tests"), timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, profile TEXT NOT NULL, state TEXT NOT NULL,
                started REAL NOT NULL, deadline REAL NOT NULL, duration_ms INTEGER,
                exit_code INTEGER, output TEXT, truncated INTEGER NOT NULL DEFAULT 0,
                approval_sha256 TEXT NOT NULL
            )""")
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("UPDATE runs SET state='interrupted' WHERE state='running' AND deadline<?", (now,))
            db.execute("""DELETE FROM runs WHERE state!='running' AND (started<?
                       OR id IN (SELECT id FROM runs ORDER BY started DESC,id LIMIT -1 OFFSET ?))""",
                       (now - _RETENTION_SECONDS, _RETENTION_RUNS))
            yield db
    except (OSError, sqlite3.Error) as exc:
        raise GraphFlowError("本机测试记录不可用。", 503) from exc


def _stop(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # The PID belongs to the process we just launched, not to a request parameter.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        if process.poll() is None:
            try:
                subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=4, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def _execute(workspace: Path, argv: list[str], timeout: int) -> tuple[str, int | None, str, bool]:
    kept = bytearray()
    clipped = False
    state = "failed"
    safe_env = {key: val for key, val in os.environ.items()
                if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "LANG"}}
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with tempfile.TemporaryDirectory(prefix="starwish-test-") as scratch:
        safe_env.update(TEMP=scratch, TMP=scratch, TMPDIR=scratch, PYTHONUTF8="1", PYTHONNOUSERSITE="1")
        try:
            process = subprocess.Popen(argv, cwd=workspace, env=safe_env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
        except OSError:
            return "launch_failed", None, "", False

        def consume():
            nonlocal clipped
            with process.stdout as pipe:
                while chunk := pipe.read1(8192):
                    room = MAX_LOG_BYTES - len(kept)
                    kept.extend(chunk[:room])
                    clipped = clipped or len(chunk) > room

        drain = threading.Thread(target=consume, daemon=True)
        drain.start()
        try:
            code = process.wait(timeout=timeout)
            state = "passed" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            state = "timed_out"
            _stop(process)
            code = process.wait(timeout=5)
        drain.join(timeout=1)
        if drain.is_alive():
            _stop(process)
        return state, code, bytes(kept).decode("utf-8", errors="replace"), clipped or drain.is_alive()


def run(workspace: Path, profile: str, approval_sha256: str) -> dict:
    chosen = next((item for item in plan(workspace, operator=True)["profiles"] if item["profile"] == profile), None)
    if chosen is None or chosen["approval_sha256"] != approval_sha256:
        raise GraphFlowError("测试计划已变化，请刷新后重新审批。", 409)
    run_id = secrets.token_hex(16)
    now = time.time()
    with _db(workspace) as db:
        if db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone():
            raise GraphFlowError("此工作区已有测试正在运行。", 429)
        db.execute("""INSERT INTO runs(id,profile,state,started,deadline,approval_sha256)
                   VALUES(?,?,?,?,?,?)""", (run_id, profile, "running", now, now + chosen["timeout"] + 20, approval_sha256))
    start = time.monotonic()
    try:
        state, code, output, clipped = _execute(workspace, chosen["argv"], chosen["timeout"])
    except (OSError, subprocess.SubprocessError):
        state, code, output, clipped = "interrupted", None, "", False
    elapsed = int((time.monotonic() - start) * 1000)
    with _db(workspace) as db:
        db.execute("UPDATE runs SET state=?,duration_ms=?,exit_code=?,output=?,truncated=? WHERE id=?",
                   (state, elapsed, code, output, int(clipped), run_id))
    return {"run_id": run_id, "log_handle": _LOG_HANDLE_PREFIX + run_id,
            "observation_handle": _OBSERVATION_PREFIX + run_id, "profile": profile, "state": state, "exit_code": code,
            "duration_ms": elapsed, "truncated": clipped}


def _read_runs(workspace: Path, columns: str, predicate: str = "", args: tuple = (), limit: int = 1) -> list:
    # Column lists and predicates are internal SQL constants; values are bound below.
    now = time.time()
    try:
        path = state_path(workspace, "-tests", create=False)
        if not path.exists() and not path.is_symlink():
            return []
        if path.resolve() != path or not path.is_file():
            raise OSError("Unexpected test record path")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
            db.row_factory = sqlite3.Row
            return db.execute(f"""WITH ranked AS (
                SELECT id, started,
                    CASE WHEN state='running' AND deadline<? THEN 'interrupted' ELSE state END AS visible_state,
                    ROW_NUMBER() OVER (ORDER BY started DESC,id) AS position FROM runs
            ), visible AS (
                SELECT r.id,r.profile,v.visible_state AS state,r.exit_code,r.duration_ms,r.output,r.truncated,r.started
                FROM ranked v JOIN runs r ON r.id=v.id
                WHERE v.visible_state='running' OR (v.started>=? AND v.position<=?)
            )
            SELECT {columns} FROM visible {predicate} ORDER BY started DESC,id LIMIT ?""",
                (now, now - _RETENTION_SECONDS, _RETENTION_RUNS, *args, limit)).fetchall()
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        raise GraphFlowError("本机测试记录不可用。", 503) from exc


def records(workspace: Path) -> list[dict]:
    if not enabled():
        return []
    rows = _read_runs(workspace, "id AS run_id,profile,state,exit_code,duration_ms,truncated", limit=10)
    return [dict(row, log_handle=_LOG_HANDLE_PREFIX + row["run_id"],
                 observation_handle=_OBSERVATION_PREFIX + row["run_id"]) for row in rows]


def latest_finished(workspace: Path) -> sqlite3.Row | None:
    rows = _read_runs(workspace, "profile,state,exit_code", "WHERE state!='running'") if enabled() else []
    return rows[0] if rows else None


def evidence_record(workspace: Path, run_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise GraphFlowError("测试记录编号无效。")
    rows = _read_runs(workspace, "profile,state,exit_code", "WHERE id=? AND state!='running'", (run_id,))
    if not rows:
        raise GraphFlowError("未找到已结束的测试记录。", 404)
    return dict(rows[0])


def logs(workspace: Path, run_id: str, offset: int = 0, *, page_size: int = MAX_PAGE_CHARS) -> dict:
    if type(offset) is not int or not 0 <= offset <= MAX_LOG_BYTES:
        raise GraphFlowError("日志游标无效。")
    row = _log_row(workspace, run_id)
    output = row["output"] or ""
    end = min(len(output), offset + page_size)
    return {"text": output[offset:end], "next_offset": end if end < len(output) else None,
            "state": row["state"], "truncated": bool(row["truncated"])}


def _log_row(workspace: Path, run_id: str) -> sqlite3.Row:
    if not enabled():
        raise GraphFlowError("未配置项目测试。", 503)
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise GraphFlowError("测试记录编号无效。")
    rows = _read_runs(workspace, "state,output,truncated", "WHERE id=?", (run_id,))
    if not rows:
        raise GraphFlowError("找不到测试记录。", 404)
    return rows[0]


def exec_readonly(cmd: str = "", run: bool = False, changedFiles: list[str] | None = None,
                  handle: str = "", range: list[int] | None = None, timeoutMs: int | None = None,
                  rootDir: str = "") -> str:
    if type(run) is not bool or not isinstance(cmd, str) or not isinstance(handle, str):
        raise GraphFlowError("gf_exec 参数格式无效。")
    cmd = cmd.strip()
    if run or timeoutMs is not None or cmd not in {"", "affected-tests", "status", "doctor",
                                                    "graphflow status", "graphflow doctor"}:
        raise GraphFlowError("星愿 gf_exec 只提供测试定位、状态和日志；执行需在本机面板审批。", 403)
    if handle:
        legacy = graphflow_legacy_observations.HANDLE.fullmatch(handle)
        if cmd or changedFiles is not None or (not legacy and not re.fullmatch(r"(?:swtest|gfo):[0-9a-f]{32}", handle)):
            raise GraphFlowError("日志句柄须为完整的星愿或 GraphFlow 日志 ID。")
        if legacy:
            start, end = _log_bounds(range, [1, 101])
            lines = graphflow_legacy_observations.read(graphflow.checked_workspace(rootDir), legacy[1], MAX_LOG_BYTES)
            body = "\n".join(f"{n:>4}: {line}" for n, line in enumerate(lines[start - 1:end], start))
            header = f"# Observation {handle} (lines {start}-{min(len(lines), end)} of {len(lines)})"
            return header + "\n" + body[:MAX_PAGE_CHARS] + ("\n（日志已截断。）" if len(body) > MAX_PAGE_CHARS else "")
        if not agent_logs_enabled():
            raise GraphFlowError("测试日志未授权给 Agent。", 403)
        return _log_lines(graphflow.checked_workspace(rootDir), handle, range)
    if range is not None:
        raise GraphFlowError("range 只用于测试日志句柄。")
    workspace = graphflow.checked_workspace(rootDir)
    if cmd not in {"", "affected-tests"}:
        if changedFiles is not None:
            raise GraphFlowError("状态查询不接受改动文件。")
        from . import graphflow_diagnostics

        return graphflow_diagnostics.exec_status(workspace, doctor=cmd.endswith("doctor"))
    explicit = changedFiles is not None
    if explicit:
        if not isinstance(changedFiles, list) or len(changedFiles) > graphflow.MAX_AFFECTED_INPUTS:
            raise GraphFlowError(f"最多提供 {graphflow.MAX_AFFECTED_INPUTS} 个项目改动文件。")
        if any(not isinstance(path, str) for path in changedFiles):
            raise GraphFlowError("改动文件须为项目相对路径。")
        changes = sorted({path.replace("\\", "/").removeprefix("./") for path in changedFiles})
        if any(not graphflow._safe_source(path) for path in changes):
            raise GraphFlowError("改动文件须为项目相对路径。")
    else:
        changes = _changed(workspace)
    result = graphflow.affected_tests(workspace, changes, explicit=explicit)
    tests = result["tests"]
    lines = [f"# Affected Tests ({len(tests)} test file(s) from {len(changes)} changed file(s)):",
             *(f"- {path}" for path in tests[:40])]
    if result["partial"] or len(tests) > 40:
        lines.append("（结果不完整，请缩小改动范围。）")
    lines.append("仅建议；请在本机面板审批预设测试方案。")
    text = "\n".join(lines)
    return text if len(text) <= MAX_PAGE_CHARS else text[:MAX_PAGE_CHARS] + "\n（输出已截断。）"


def _log_bounds(bounds: list[int] | None, default: list[int]) -> tuple[int, int]:
    bounds = default if bounds is None else bounds
    if (not isinstance(bounds, list) or len(bounds) != 2 or any(type(n) is not int for n in bounds)
            or not 1 <= bounds[0] <= bounds[1] <= MAX_LOG_BYTES or bounds[1] - bounds[0] >= MAX_LOG_LINES):
        raise GraphFlowError(f"日志范围为 1 起算的起止行，最多 {MAX_LOG_LINES} 行。")
    return bounds[0], bounds[1]


def _log_lines(workspace: Path, handle: str, bounds: list[int] | None) -> str:
    start, end = _log_bounds(bounds, [1, 100])
    row = _log_row(workspace, handle.partition(":")[2])
    if row["state"] == "running":
        raise GraphFlowError("测试仍在运行。", 409)
    lines = (row["output"] or "").splitlines()
    body = "\n".join(f"{n:>5} | {line}" for n, line in enumerate(lines[start - 1:end], start))
    header = f"# Observation {handle} (requested lines {start}-{end}; {len(lines)} stored)"
    return header + "\n" + body[:MAX_PAGE_CHARS] + ("\n（日志已截断。）" if row["truncated"] or len(body) > MAX_PAGE_CHARS else "")
