"""捕获的任务：按 客户端 + 会话 + 任务 归组，碎片一条条记下，闭环时合并。"""
from __future__ import annotations

import json
import time

from .. import config
from ..db import get_conn

_ready_for: str | None = None

DDL = """
CREATE TABLE IF NOT EXISTS watch_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    client TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    task_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    star_id INTEGER,
    skill_name TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wt_session ON watch_tasks(client, session_id, status);
CREATE TABLE IF NOT EXISTS watch_fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    turn_ts TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wf_task ON watch_fragments(task_id);
"""

STATUS = ("open", "closed", "failed")


def _ensure(c) -> None:
    global _ready_for
    if _ready_for != str(config.DB_PATH):
        c.executescript(DDL)
        _ready_for = str(config.DB_PATH)


def conn():
    """with store.conn() as c: …（自动建表）"""
    class _C:
        def __enter__(self):
            self.cm = get_conn()
            c = self.cm.__enter__()
            _ensure(c)
            return c

        def __exit__(self, *a):
            return self.cm.__exit__(*a)
    return _C()


def task_key(client: str, session_id: str, key: str) -> str:
    return f"{client}:{session_id}:{key}"


def open_tasks(client: str, session_id: str, limit: int = 8) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM watch_tasks WHERE client=? AND session_id=? AND status='open' ORDER BY updated_at DESC LIMIT ?",
                         (client, session_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_task(tid: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM watch_tasks WHERE id=?", (tid,)).fetchone()
        if not r:
            return None
        frags = c.execute("SELECT * FROM watch_fragments WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    return {**dict(r), "fragments": [dict(f) for f in frags]}


def upsert_fragment(client: str, session_id: str, tkey: str, text: str, category: str, status: str,
                    cwd: str = "", turn_ts: str = "", title: str = "") -> dict:
    """加一条碎片。任务已闭环 / 失败后又出现同名任务 → 重新打开。返回任务（含碎片）。"""
    status = status if status in STATUS else "open"
    k = task_key(client, session_id, tkey)
    now = time.time()
    with conn() as c:
        r = c.execute("SELECT * FROM watch_tasks WHERE key=?", (k,)).fetchone()
        if r:
            tid = r["id"]
            c.execute("UPDATE watch_tasks SET category=COALESCE(NULLIF(?,''),category), updated_at=?, "
                      "status=CASE WHEN status='open' THEN status ELSE 'open' END WHERE id=?", (category, now, tid))
        else:
            cur = c.execute("INSERT INTO watch_tasks(key,client,session_id,task_key,title,category,cwd,status,created_at,updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (k, client, session_id, tkey, (title or text)[:60], category or "", cwd or "", "open", now, now))
            tid = cur.lastrowid
        c.execute("INSERT INTO watch_fragments(task_id,text,status,turn_ts,created_at) VALUES (?,?,?,?,?)",
                  (tid, text, status, turn_ts or "", now))
    return get_task(tid)


def update_task(tid: int, **fields) -> dict:
    allowed = {"title", "category", "status", "star_id", "skill_name", "result"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return get_task(tid)
    with conn() as c:
        c.execute(f"UPDATE watch_tasks SET {', '.join(k + '=?' for k in fields)}, updated_at=? WHERE id=?",
                  (*fields.values(), time.time(), tid))
    return get_task(tid)


def list_tasks(limit: int = 100, status: str | None = None) -> list[dict]:
    q = "SELECT t.*, (SELECT COUNT(*) FROM watch_fragments f WHERE f.task_id=t.id) AS n_fragments, " \
        "(SELECT text FROM watch_fragments f WHERE f.task_id=t.id ORDER BY id DESC LIMIT 1) AS last_fragment FROM watch_tasks t"
    args: list = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY (status='open') DESC, updated_at DESC LIMIT ?"
    args.append(max(1, min(500, limit)))
    with conn() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def counts() -> dict:
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM watch_tasks GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)
