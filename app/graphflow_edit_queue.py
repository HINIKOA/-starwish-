"""Local, opt-in broker for operator-approved workspace edits."""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, graphflow, graphflow_edits
from .graphflow import GraphFlowError

ROLLBACK_TTL = 86400
AUDIT_TTL = 30 * 86400
MAX_HISTORY = 500
MAX_EVENTS = 2000


def enabled() -> bool:
    return os.environ.get("STARWISH_GRAPHFLOW_EDIT_QUEUE") == "1"


def _operator_key() -> str:
    key = os.environ.get("STARWISH_GRAPHFLOW_OPERATOR_KEY", "")
    return key if len(key) >= 24 else ""


def operator_ready() -> bool:
    return bool(_operator_key())


def require_operator(provided: str, *, feature_enabled: bool | None = None) -> None:
    key = _operator_key()
    if not (enabled() if feature_enabled is None else feature_enabled) or not key:
        raise GraphFlowError("未启用该本机审批功能，或没有配置审批密钥。", 503)
    if not hmac.compare_digest(key.encode("utf-8"), provided.encode("utf-8")):
        raise GraphFlowError("审批密钥无效。", 403)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def state_path(workspace: Path, suffix: str = "", *, create: bool = True) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    folder = (base / "StarWish" / "project-edits").resolve()
    if not base.is_absolute() or folder.is_relative_to(workspace) or folder.is_relative_to(config.SOUL_DIR.resolve()):
        raise GraphFlowError("审批记录必须保存在工作区和灵魂包之外。", 503)
    namespace = _hash(os.fsencode(str(workspace)) + b"\0" + os.fsencode(str(config.SOUL_DIR.resolve())))
    if create:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder / f"{namespace}{suffix}.sqlite3"


@contextmanager
def _db(workspace: Path):
    try:
        with sqlite3.connect(state_path(workspace), timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS edits(
                id TEXT PRIMARY KEY, path TEXT NOT NULL, base TEXT NOT NULL, result TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                before BLOB, after BLOB, diff TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS edit_events(
                seq INTEGER PRIMARY KEY, preview_id TEXT NOT NULL, path TEXT NOT NULL,
                state TEXT NOT NULL, at REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS edit_files(
                preview_id TEXT NOT NULL REFERENCES edits(id) ON DELETE CASCADE,
                path TEXT NOT NULL, base TEXT NOT NULL, result TEXT NOT NULL,
                before BLOB, after BLOB, PRIMARY KEY(preview_id,path)
            )""")
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            for state, expired in (
                ("expired", db.execute("SELECT id FROM edits WHERE state='pending' AND created<=?",
                                       (now - graphflow_edits.TTL,)).fetchall()),
                ("archived", db.execute("SELECT id FROM edits WHERE state='applied' AND updated<=?",
                                        (now - ROLLBACK_TTL,)).fetchall()),
            ):
                for row in expired:
                    _state(db, row["id"], state, purge=True)
            db.execute("""DELETE FROM edits WHERE state IN ('expired','stale','rolled_back','archived','failed')
                        AND (updated<=? OR id IN (SELECT id FROM edits ORDER BY updated DESC LIMIT -1 OFFSET ?))""",
                       (now - AUDIT_TTL, MAX_HISTORY))
            db.execute("""DELETE FROM edit_events WHERE at<=?
                       OR seq IN (SELECT seq FROM edit_events ORDER BY seq DESC LIMIT -1 OFFSET ?)""",
                       (now - AUDIT_TTL, MAX_EVENTS))
            yield db
    except (OSError, sqlite3.Error) as exc:
        raise GraphFlowError("本机编辑审批队列不可用，请稍后再试。", 503) from exc
    finally:
        if "db" in locals():
            db.close()


def enqueue_parts(workspace: Path, proposal: dict, parts: list[dict]) -> None:
    with _db(workspace) as db:
        count = db.execute("""SELECT count(*) FROM edits WHERE state IN
                           ('pending','applying','applied','rolling_back','recovery_needed')""").fetchone()[0]
        if count >= graphflow_edits.MAX_PENDING:
            raise GraphFlowError("编辑队列已满，请等待过期或处理现有变更。", 429)
        now = time.time()
        single = len(parts) == 1
        db.execute("INSERT INTO edits VALUES(?,?,?,?,?,?,?,?,?,?)", (
            proposal["preview_id"], proposal["path"], proposal["base_sha256"], proposal["result_sha256"],
            "pending", now, now, parts[0]["before"] if single else None, parts[0]["after"] if single else None,
            proposal["diff"],
        ))
        if not single:
            db.executemany("INSERT INTO edit_files VALUES(?,?,?,?,?,?)", (
                (proposal["preview_id"], part["path"], part["base"], part["result"], part["before"], part["after"])
                for part in parts))
        _audit(db, proposal["preview_id"], "pending", now)


def _find(db: sqlite3.Connection, preview_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM edits WHERE id=?", (preview_id,)).fetchone()
    if row is None:
        raise GraphFlowError("当前工作区没有该预览。", 404)
    return row


def _state(db: sqlite3.Connection, preview_id: str, state: str, purge: bool = False) -> None:
    remove_data = ", before=NULL, after=NULL, diff=NULL" if purge else ""
    now = time.time()
    db.execute(f"UPDATE edits SET state=?, updated=?{remove_data} WHERE id=?", (state, now, preview_id))
    if purge:
        db.execute("UPDATE edit_files SET before=NULL,after=NULL WHERE preview_id=?", (preview_id,))
    _audit(db, preview_id, state, now)


def _audit(db: sqlite3.Connection, preview_id: str, state: str, now: float) -> None:
    db.execute("""INSERT INTO edit_events(preview_id,path,state,at)
               SELECT ?,path,?,? FROM edit_files WHERE preview_id=?
               UNION SELECT id,path,?,? FROM edits WHERE id=?""", (preview_id, state, now, preview_id, state, now, preview_id))


def _members(db: sqlite3.Connection, row: sqlite3.Row) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM edit_files WHERE preview_id=? ORDER BY path", (row["id"],)).fetchall()


def _busy_conflict(db: sqlite3.Connection, preview_id: str, paths: list[str]) -> None:
    markers = ",".join("?" for _ in paths)
    if db.execute(f"""SELECT 1 FROM edits e WHERE e.id!=? AND e.state IN ('applying','rolling_back','recovery_needed')
                   AND (e.path IN ({markers}) OR EXISTS (SELECT 1 FROM edit_files f
                        WHERE f.preview_id=e.id AND f.path IN ({markers})))""", (preview_id, *paths, *paths)).fetchone():
        raise GraphFlowError("至少一个文件存在进行中或待恢复的写入。", 409)


def _current(workspace: Path, path: str) -> str | None:
    try:
        return _hash(graphflow_edits._read_tracked(workspace, path))
    except GraphFlowError as exc:
        if exc.status_code in {404, 413}:
            return None
        raise


def check(workspace: Path, preview_id: str) -> dict:
    with _db(workspace) as db:
        row = _find(db, preview_id)
        state = row["state"]
        members = _members(db, row)
        if state == "pending" and any(_current(workspace, item["path"]) != item["base"] for item in members or [row]):
            state = "stale"
            _state(db, preview_id, state, purge=True)
        result = {"preview_id": preview_id, "path": row["path"], "current": state == "pending",
                  "state": state, "written": state in {"applied", "archived"}}
        if members:
            result["paths"] = [part["path"] for part in members]
    if state == "expired":
        raise GraphFlowError("预览已过期，请重新生成。", 410)
    if state == "stale":
        raise GraphFlowError("原文件已改变，请重新生成预览。", 409)
    return result


def list_items(workspace: Path) -> list[dict]:
    with _db(workspace) as db:
        rows = db.execute("""SELECT id AS preview_id, path, base AS base_sha256, result AS result_sha256,
                            state, updated, diff FROM edits ORDER BY updated DESC LIMIT 50""")
        items = [dict(row) for row in rows]
        for item in items:
            paths = db.execute("SELECT path FROM edit_files WHERE preview_id=? ORDER BY path", (item["preview_id"],)).fetchall()
            if paths:
                item["paths"] = [part["path"] for part in paths]
        return items


def _replace(workspace: Path, relative: str, expect: str, data: bytes) -> None:
    path = workspace / relative
    if _current(workspace, relative) != expect:
        raise GraphFlowError("文件已变化，拒绝覆盖。", 409)
    tmp = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".starwish-edit-", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.chmod(mode)
        if _current(workspace, relative) != expect:
            raise GraphFlowError("文件在写入前变化，拒绝覆盖。", 409)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def mutate(workspace: Path, preview_id: str, confirmed: str, action: str) -> dict:
    if action not in {"apply", "rollback"}:
        raise ValueError("invalid edit action")
    apply = action == "apply"
    previous, busy, done = ("pending", "applying", "applied") if apply else ("applied", "rolling_back", "rolled_back")
    from_key, to_key, blob = ("base", "result", "after") if apply else ("result", "base", "before")
    with _db(workspace) as db:
        row = _find(db, preview_id)
        members = _members(db, row)
        restoring = bool(members) and not apply and row["state"] == "recovery_needed"
        if (row["state"] != previous and not restoring) or row[to_key] != confirmed:
            raise GraphFlowError("审批状态或确认哈希已变化，请刷新后重试。", 409)
        _busy_conflict(db, preview_id, [part["path"] for part in members or [row]])
        if members:
            _group_writes(workspace, row, members, apply, restoring)
        elif row[blob] is None or _hash(row[blob]) != row[to_key]:
            raise GraphFlowError("审批记录内容无效。", 409)
        if not members and not apply and _current(workspace, row["path"]) != row[from_key]:
            raise GraphFlowError("文件在应用后已变化，拒绝回滚覆盖。", 409)
        _state(db, preview_id, busy)
    if members:
        return _finish_group(workspace, preview_id, busy, done, apply, restoring)
    failure = None
    with _db(workspace) as db:
        row = _find(db, preview_id)
        if row["state"] != busy:
            raise GraphFlowError("操作状态已改变，请刷新审批队列。", 409)
        try:
            _replace(workspace, row["path"], row[from_key], row[blob])
        except GraphFlowError as exc:
            if exc.status_code != 409:
                raise
            _state(db, preview_id, "stale" if apply else "applied", purge=apply)
            failure = exc
        except OSError as exc:
            # Leave the durable in-progress marker for explicit hash-based recovery.
            raise GraphFlowError("未能确认写入结果；请使用恢复检查。", 503) from exc
        else:
            _state(db, preview_id, done, purge=not apply)
    if failure is not None:
        raise failure
    return {"preview_id": preview_id, "path": row["path"], "state": done}


def _group_writes(workspace: Path, row: sqlite3.Row, members: list[sqlite3.Row], apply: bool,
                  restoring: bool) -> list[tuple[str, str, bytes]]:
    if len(members) < 2 or any(row[key] != graphflow_edits.manifest_digest(members, key) for key in ("base", "result")):
        raise GraphFlowError("批量审批清单不完整。", 409)
    from_key, to_key, blob = ("base", "result", "after") if apply else ("result", "base", "before")
    writes = []
    for part in members:
        if any(part[field] is None or _hash(part[field]) != part[key]
               for field, key in (("before", "base"), ("after", "result"))):
            raise GraphFlowError("批量审批记录内容无效。", 409)
        current = _current(workspace, part["path"])
        if restoring and current == part[to_key]:
            continue
        if current != part[from_key]:
            raise GraphFlowError("批量审批中有文件已变化，拒绝覆盖。", 409)
        writes.append((part["path"], current, part[blob]))
    return writes


def _finish_group(workspace: Path, preview_id: str, busy: str, done: str, apply: bool, restoring: bool) -> dict:
    # The first transaction committed the busy marker before any files were changed.
    with _db(workspace) as db:
        row = _find(db, preview_id)
        if row["state"] != busy:
            raise GraphFlowError("操作状态已改变，请刷新审批队列。", 409)
        members = _members(db, row)
        try:
            for path, expected, data in _group_writes(workspace, row, members, apply, restoring):
                _replace(workspace, path, expected, data)
            target = "result" if apply else "base"
            if any(_current(workspace, part["path"]) != part[target] for part in members):
                raise GraphFlowError("批量写入后文件哈希不一致。", 409)
        except (GraphFlowError, OSError) as exc:
            raise GraphFlowError("批量操作未完成；请先核对中断状态，不要重复写入。", 503) from exc
        _state(db, preview_id, done, purge=not apply)
    return {"preview_id": preview_id, "path": row["path"], "paths": [part["path"] for part in members], "state": done}


def recover(workspace: Path, preview_id: str) -> dict:
    with _db(workspace) as db:
        row = _find(db, preview_id)
        if row["state"] not in {"applying", "rolling_back"} or row["updated"] > time.time() - 30:
            raise GraphFlowError("没有可恢复操作，或操作尚未超过 30 秒。", 409)
        members = _members(db, row)
        if members:
            current = [_current(workspace, part["path"]) for part in members]
            if all(digest == part["base"] for digest, part in zip(current, members)):
                state = "failed" if row["state"] == "applying" else "rolled_back"
            elif all(digest == part["result"] for digest, part in zip(current, members)):
                state = "applied"
            else:
                state = "recovery_needed"
            _state(db, preview_id, state, purge=state in {"failed", "rolled_back"})
            return {"preview_id": preview_id, "path": row["path"], "paths": [part["path"] for part in members],
                    "state": state, "written": False}
        current = _current(workspace, row["path"])
        if row["state"] == "applying":
            state = "applied" if current == row["result"] else "failed"
        else:
            state = "rolled_back" if current == row["base"] else "applied"
        _state(db, preview_id, state, purge=state == "rolled_back" or (state == "failed" and current == row["base"]))
        return {"preview_id": preview_id, "path": row["path"], "state": state, "written": False}
