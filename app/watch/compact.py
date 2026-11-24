"""Bounded, reference-only handoff from a project-local Codex PreCompact hook."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .codex_scope import in_workspace, local_soul, manual_grant, regular_file

QUEUE = "codex-compact.sqlite3"
_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_ROLLOUT = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)\.jsonl")
_HEAD_LIMIT = 64 * 1024
_TAIL_LIMIT = 512 * 1024
_MAX_PENDING = 2048
_FIELDS = "workspace, session_id, turn_id, rollout, start_byte, end_byte, fingerprint"
_FINALIZED = """CREATE TABLE IF NOT EXISTS finalized (
    workspace TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    PRIMARY KEY(workspace,session_id,turn_id))"""
_RETRY = """CREATE TABLE IF NOT EXISTS retry (
    workspace TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    not_before REAL NOT NULL, PRIMARY KEY(workspace,session_id,turn_id))"""


@dataclass(frozen=True)
class Intent:
    workspace: str
    session_id: str
    turn_id: str
    rollout: str
    start_byte: int
    end_byte: int
    fingerprint: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.workspace, self.session_id, self.turn_id


def _rollout_path(codex_home: Path, path: Path, session: str) -> bool:
    match = _ROLLOUT.fullmatch(path.name)
    return bool(match and match[1] == session and path.is_absolute()
                and path.is_relative_to(codex_home / "sessions")
                and not (os.name == "nt" and path.drive.startswith("\\\\"))
                and regular_file(path))


def _object(line: bytes) -> dict:
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {}
    except (ValueError, RecursionError):
        return {}


def _id(value) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _ordinary_rollout(head: bytes, session: str, workspace: Path, in_workspace) -> bool:
    record = _object(head)
    meta = record.get("payload")
    return (record.get("type") == "session_meta" and isinstance(meta, dict)
            and meta.get("id") == session and isinstance(meta.get("source"), str)
            and in_workspace(workspace, meta.get("cwd")))


def _reference(tail: bytes, offset: int, workspace: Path, session: str, active: str, in_workspace):
    base = offset
    current = None
    candidate = None
    for line in tail.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        if not line.endswith(b"\n"):
            break
        row = _object(line)
        kind, p = row.get("type"), row.get("payload")
        if not isinstance(p, dict):
            continue
        event = p.get("type") if kind == "event_msg" else None
        turn = p.get("turn_id")
        if event == "task_started":
            current = [turn, start, False, False] if _id(turn) and turn != active else None
        elif current and turn == current[0]:
            if kind == "turn_context":
                current[2] = in_workspace(workspace, p.get("cwd"))
            elif event == "user_message":
                current[3] |= isinstance(p.get("message"), str) and bool(p["message"].strip())
            elif event == "item_completed" and p.get("thread_id") == session:
                item = p.get("item")
                current[3] |= isinstance(item, dict) and item.get("type") == "UserMessage"
            elif event in ("task_complete", "turn_complete"):
                if current[2] and current[3]:
                    span = tail[current[1] - base:offset - base]
                    candidate = (turn, current[1], offset, hashlib.sha256(span).hexdigest())
                current = None
            elif event == "turn_aborted":
                current = None
    return candidate


def stage(soul: Path, workspace: Path, codex_home: Path, event: dict, in_workspace) -> bool:
    """Stage at most one completed main-thread turn; caller checks the manual grant."""
    session, active = event.get("session_id"), event.get("turn_id")
    if (event.get("hook_event_name") != "PreCompact" or event.get("trigger") not in ("auto", "manual")
            or not _id(session) or not _id(active) or event.get("agent_id") or event.get("agent_type")
            or not in_workspace(workspace, event.get("cwd"))):
        return False
    value = event.get("transcript_path")
    if not isinstance(value, str):
        return False
    path = Path(value)
    sessions = codex_home / "sessions"
    if not _rollout_path(codex_home, path, session):
        return False
    with path.open("rb") as stream:
        head = stream.readline(_HEAD_LIMIT)
        if not head.endswith(b"\n") or not _ordinary_rollout(head, session, workspace, in_workspace):
            return False
        offset = max(stream.seek(0, os.SEEK_END) - _TAIL_LIMIT, 0)
        if offset:
            stream.seek(offset - 1)
            if stream.read(1) != b"\n":
                stream.readline(_TAIL_LIMIT)
        else:
            stream.seek(0)
        offset = stream.tell()
        ref = _reference(stream.read(_TAIL_LIMIT), offset, workspace, session, active, in_workspace)
    if not ref:
        return False
    destination = soul / QUEUE
    if (destination.exists() or destination.is_symlink()) and not regular_file(destination):
        return False
    key = (os.path.normcase(str(workspace)), session, ref[0])
    with closing(sqlite3.connect(destination, timeout=0.1)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS intents (
            workspace TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            rollout TEXT NOT NULL, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, PRIMARY KEY(workspace, session_id, turn_id))""")
        db.execute(_FINALIZED)
        db.execute(_RETRY)
        inserted = db.execute(
            """INSERT OR IGNORE INTO intents SELECT ?,?,?,?,?,?,?
                 WHERE (SELECT COUNT(*) FROM intents) < ?
                    AND NOT EXISTS (SELECT 1 FROM finalized WHERE workspace=? AND session_id=? AND turn_id=?)""",
            (*key, path.relative_to(sessions).as_posix(), *ref[1:], _MAX_PENDING, *key),
        ).rowcount
    return inserted == 1


def pending(soul: Path, *, session_id: str | None = None, limit: int = 16, ready_only: bool = False) -> list[Intent]:
    destination = soul / QUEUE
    if not regular_file(destination):
        return []
    where = []
    args: tuple = ()
    if session_id is not None:
        where.append("session_id=?")
        args = (session_id,)
    with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
        if ready_only and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retry'").fetchone():
            where.append("NOT EXISTS (SELECT 1 FROM retry r WHERE r.workspace=intents.workspace"
                         " AND r.session_id=intents.session_id AND r.turn_id=intents.turn_id AND r.not_before>?)")
            args += (time.time(),)
        query = f"SELECT {_FIELDS} FROM intents" + (" WHERE " + " AND ".join(where) if where else "")
        return [Intent(*row) for row in db.execute(query + " ORDER BY rowid LIMIT ?", (*args, limit))]


def forget(soul: Path, item: Intent) -> None:
    destination = soul / QUEUE
    if regular_file(destination):
        with closing(sqlite3.connect(destination, timeout=0.1)) as db, db:
            db.execute(_FINALIZED)
            deleted = db.execute("DELETE FROM intents WHERE workspace=? AND session_id=? AND turn_id=? AND fingerprint=?",
                                 (*item.key, item.fingerprint)).rowcount
            if deleted:
                db.execute("INSERT OR IGNORE INTO finalized VALUES (?,?,?)", item.key)
                db.execute(_RETRY)
                db.execute("DELETE FROM retry WHERE workspace=? AND session_id=? AND turn_id=?", item.key)


def defer(soul: Path, item: Intent) -> None:
    destination = soul / QUEUE
    if regular_file(destination):
        with closing(sqlite3.connect(destination, timeout=0.1)) as db, db:
            db.execute(_RETRY)
            db.execute("INSERT OR REPLACE INTO retry SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM intents "
                       "WHERE workspace=? AND session_id=? AND turn_id=? AND fingerprint=?)",
                       (*item.key, time.time() + 300, *item.key, item.fingerprint))


def authorized(soul: Path, item: Intent) -> bool:
    workspace = Path(item.workspace)
    return local_soul(soul) and in_workspace(workspace, workspace) and manual_grant(soul, workspace)


def verified_turn(soul: Path, codex_home: Path, item: Intent):
    """Read back just the staged turn; stale, replaced and out-of-scope references are not usable."""
    from .adapters import parse_codex

    if (not _id(item.session_id) or not _id(item.turn_id)
            or not isinstance(item.start_byte, int) or not isinstance(item.end_byte, int)
            or not 0 <= item.start_byte < item.end_byte <= item.start_byte + _TAIL_LIMIT
            or not authorized(soul, item)):
        return None
    path = codex_home / "sessions" / item.rollout
    if not _rollout_path(codex_home, path, item.session_id):
        return None
    workspace = Path(item.workspace)
    with path.open("rb") as stream:
        head = stream.readline(_HEAD_LIMIT)
        if not head.endswith(b"\n") or not _ordinary_rollout(head, item.session_id, workspace, in_workspace):
            return None
        stream.seek(item.start_byte)
        span = stream.read(item.end_byte - item.start_byte)
    if hashlib.sha256(span).hexdigest() != item.fingerprint:
        return None
    expected = (item.turn_id, item.start_byte, item.end_byte, item.fingerprint)
    if _reference(span, item.start_byte, workspace, item.session_id, "", in_workspace) != expected:
        return None
    turns, _ = parse_codex("codex", span, item.start_byte, {"session_id": item.session_id}, path)
    if (len(turns) == 1 and turns[0].native_turn_id == item.turn_id and turns[0].end == item.end_byte
            and not turns[0].delegated and in_workspace(workspace, turns[0].cwd)):
        return path, turns[0]
    return None
