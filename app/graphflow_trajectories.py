"""Mine replayable read bundles from opt-in native or legacy events."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections import Counter
from contextlib import closing
from pathlib import Path

from . import config, graphflow, graphflow_history
from .graphflow import GraphFlowError

ENV = "STARWISH_GRAPHFLOW_LEGACY_TRAJECTORIES"
CAPTURE_ENV = "STARWISH_GRAPHFLOW_READ_TRAJECTORIES"
ID = re.compile(r"trace_[0-9a-f]{16}")
MAX_BYTES = 1024 * 1024
MAX_EVENTS = 500
MAX_BUNDLES = 12
MAX_TARGETS = 6
MAX_PINS = 24


def capture_enabled() -> bool:
    return os.environ.get(CAPTURE_ENV) == "1"


def _state_file(workspace: Path, *, create: bool = False, suffix: str = "-reads") -> Path:
    from .graphflow_edit_queue import state_path

    path = state_path(workspace, suffix, create=create)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve(strict=True) != path:
            raise OSError("Unsafe trajectory store")
    except FileNotFoundError:
        pass
    return path


def _identifier(targets: tuple[str, ...] | list[str]) -> str:
    digest = hashlib.sha256(json.dumps(targets, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return f"trace_{digest[:16]}"


def _pin_rows(workspace: Path) -> dict[str, list[str]]:
    try:
        path = _state_file(workspace, suffix="-macros")
        if not path.exists():
            return {}
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            rows = db.execute("SELECT name,targets FROM pins WHERE length(targets)<=20000 ORDER BY name LIMIT ?",
                              (MAX_PINS,)).fetchall()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise GraphFlowError("本机固定宏记录不可读。", 503) from exc
    pins = {}
    for name, encoded in rows:
        try:
            targets = json.loads(encoded)
            if (isinstance(targets, list) and 2 <= len(targets) <= MAX_TARGETS
                    and targets == sorted({graphflow.validate_read(target=item)[1] for item in targets})
                    and name == _identifier(targets)):
                pins[name] = targets
        except (TypeError, ValueError, RecursionError):
            continue
    return pins


def pin(workspace: Path, name: str, targets: list[str]) -> None:
    if name != _identifier(targets):
        raise GraphFlowError("轨迹代码包标识已变化。", 409)
    try:
        path = _state_file(workspace, create=True, suffix="-macros")
        with closing(sqlite3.connect(path, timeout=1)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS pins (name TEXT PRIMARY KEY, targets TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM pins WHERE name=?", (name,)).fetchone():
                return
            if db.execute("SELECT COUNT(*) FROM pins").fetchone()[0] >= MAX_PINS:
                raise GraphFlowError("已达到固定宏上限，请先取消一个固定。", 409)
            db.execute("INSERT INTO pins VALUES (?,?)", (name, json.dumps(targets, ensure_ascii=True)))
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise GraphFlowError("无法保存本机固定宏。", 503) from exc


def unpin(workspace: Path, name: str) -> None:
    try:
        path = _state_file(workspace, suffix="-macros")
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)) as db, db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("DELETE FROM pins WHERE name=?", (name,))
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise GraphFlowError("无法取消固定宏。", 503) from exc


def record(workspace: Path, session: str, targets: list[str]) -> None:
    if not capture_enabled():
        return
    try:
        session_key = hashlib.sha256(os.fsencode(workspace) + b"\0" + session.encode("ascii")).hexdigest()
        event = json.dumps({"kind": "read", "ok": True, "sessionId": session_key, "items": targets},
                           ensure_ascii=True, separators=(",", ":"))
        path = _state_file(workspace, create=True)
        with closing(sqlite3.connect(path, timeout=0.2)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("CREATE TABLE IF NOT EXISTS reads (id INTEGER PRIMARY KEY, event TEXT NOT NULL)")
            db.execute("INSERT INTO reads(event) VALUES (?)", (event,))
            db.execute("DELETE FROM reads WHERE id IN (SELECT id FROM reads ORDER BY id DESC LIMIT -1 OFFSET ?)",
                       (MAX_EVENTS,))
    except (GraphFlowError, OSError, RuntimeError, sqlite3.Error):
        pass  # Optional local learning must not turn a successful read into a failure.


def _read_native(workspace: Path) -> tuple[str, list[str]]:
    if not capture_enabled():
        return "disabled", []
    try:
        path = _state_file(workspace)
        if not path.exists():
            return "missing", []
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            rows = db.execute("SELECT CASE WHEN length(event)<=20000 THEN event ELSE '' END FROM reads "
                              "ORDER BY id DESC LIMIT ?", (MAX_EVENTS,)).fetchall()
        return "ready", [row[0] for row in reversed(rows)]
    except (GraphFlowError, OSError, RuntimeError, sqlite3.Error):
        return "unavailable", []


def _read(workspace: Path) -> tuple[str, list[bytes]]:
    if os.environ.get(ENV) != "1":
        return "disabled", []
    path = workspace / ".graphflow" / "trajectories.jsonl"
    try:
        if (path.resolve(strict=True) != path or path.is_relative_to(config.SOUL_DIR)
                or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_nlink != 1):
            return "unavailable", []
        with path.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return "missing", []
    except (OSError, RuntimeError):
        return "unavailable", []
    if len(data) > MAX_BYTES:
        return "too_large", []
    return "ready", data.removeprefix(b"\xef\xbb\xbf").splitlines()[-MAX_EVENTS:]


def _event(raw: bytes | str) -> tuple[str, tuple[str, ...], str] | None:
    try:
        row = json.loads(raw)
        if not isinstance(row, dict) or row.get("kind") != "read" or row.get("ok") is not True:
            return None
        items, session = row.get("items"), row.get("sessionId")
        if (not isinstance(items, list) or not 1 <= len(items) <= MAX_TARGETS
                or not isinstance(session, str) or not 1 <= len(session) <= 128):
            return None
        targets = tuple(sorted({graphflow.validate_read(target=item)[1] for item in items}))
        for target in targets:
            target.encode("utf-8")
        timestamp = row.get("ts")
        return session, targets, timestamp if isinstance(timestamp, str) and len(timestamp) <= 64 else ""
    except (TypeError, ValueError, RecursionError):
        return None


def _counts(events: list[bytes] | list[str]) -> Counter:
    counts = Counter()
    previous = None
    seen = set()
    for raw in events:
        event = _event(raw)
        if event is None or (event[2] and event in seen):
            previous = None
            continue
        if event[2]:
            seen.add(event)
        session, targets, _ = event
        if len(targets) > 1:
            counts[targets] += 1
            previous = None
        elif previous and previous[0] == session and previous[1] != targets:
            counts[tuple(sorted((*previous[1], *targets)))] += 1
            previous = None
        else:
            previous = (session, targets)
    return counts


def mine(workspace: Path, *, budget: int = 5000) -> dict:
    status, events = _read(workspace)
    native_status, native_events = _read_native(workspace)
    result = {"status": status if status != "disabled" else native_status,
              "events_scanned": len(events) + len(native_events), "candidates": [], "more": False, "persisted": False}
    counts = _counts(events)
    if native_status != "disabled":
        result["sources"] = {"legacy": status, "native": native_status}
        if native_status == "ready":
            result["status"] = "ready"
            counts.update(_counts(native_events))
    groups = [(targets, count) for targets, count in counts.items() if count >= 2]
    if not groups:
        return result
    git = shutil.which("git")
    if not git:
        raise GraphFlowError("轨迹分析需要 Git。", 503)
    tracked = graphflow_history._tracked_paths(workspace, git)
    groups.sort(key=lambda row: (-row[1], row[0]))
    for targets, count in groups:
        if any(os.fsencode(graphflow.validate_read(target=target)[2]) not in tracked for target in targets):
            continue
        if len(result["candidates"]) >= MAX_BUNDLES:
            result["more"] = True
            break
        result["candidates"].append({"id": _identifier(targets), "targets": list(targets), "occurrences": count})
        if len(json.dumps(result, ensure_ascii=False)) > budget:
            result["candidates"].pop()
            result["more"] = True
            break
    return result


def specs(workspace: Path) -> dict:
    pinned = _pin_rows(workspace)
    try:
        targets = {entry["id"]: entry["targets"] for entry in mine(workspace, budget=42000)["candidates"]}
    except GraphFlowError:
        targets = {}
    return {name: {"title": f"轨迹代码包 · {name}", "steps": [{"op": "read_batch", "targets": items}],
                   "persistence": "pinned" if name in pinned else "candidate"}
            for name, items in {**targets, **pinned}.items()}
