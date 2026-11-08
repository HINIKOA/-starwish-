"""星愿自己的任务记录（任务抽屉「星愿」页）：对话归纳、对话抽屉里的指令、后台整理……

状态：running（进行中，橙）/ done（完成，绿）/ failed（失败，红）。只保留最近 300 条。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from .db import get_conn

_ready_for: str | None = None
_lock = threading.Lock()
KEEP = 300

DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""

KINDS = {"watch": "对话归纳", "agent": "对话指令", "maintenance": "记忆整理", "fuse": "星辰融合", "review": "技能巡检", "organize": "整理星系", "compact": "上下文压缩", "backup": "备份同步"}


def _ensure(c) -> None:
    global _ready_for
    from . import config
    key = str(config.DB_PATH)
    if _ready_for != key:
        c.executescript(DDL)
        _ready_for = key


def start(kind: str, title: str, detail: str = "") -> int:
    now = time.time()
    with _lock, get_conn() as c:
        _ensure(c)
        cur = c.execute("INSERT INTO jobs(kind,title,status,detail,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                        (kind, title[:200], "running", detail[:2000], now, now))
        jid = cur.lastrowid
        c.execute("DELETE FROM jobs WHERE id <= ?", (jid - KEEP,))
        return jid


def finish(jid: int, status: str = "done", detail: str | None = None, title: str | None = None) -> None:
    if status not in ("running", "done", "failed"):
        status = "done"
    with _lock, get_conn() as c:
        _ensure(c)
        sets, args = ["status=?", "updated_at=?"], [status, time.time()]
        if detail is not None:
            sets.append("detail=?")
            args.append(detail[:2000])
        if title is not None:
            sets.append("title=?")
            args.append(title[:200])
        c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, jid))


@contextmanager
def run(kind: str, title: str):
    """with jobs.run("watch", "归纳…") as j: j["detail"] = "…"  —— 出异常自动标记失败。"""
    jid = start(kind, title)
    box = {"id": jid, "detail": "", "status": "done", "title": None}
    try:
        yield box
    except Exception as e:
        finish(jid, "failed", (box.get("detail") or "") + f"\n出错：{e}", box.get("title"))
        raise
    else:
        finish(jid, box.get("status") or "done", box.get("detail") or "", box.get("title"))


def list_jobs(limit: int = 50) -> list[dict]:
    with get_conn() as c:
        _ensure(c)
        rows = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(1, min(300, limit)),)).fetchall()
    return [{**dict(r), "kind_title": KINDS.get(r["kind"], r["kind"])} for r in rows]


def fail_stale(max_age: float = 3600) -> int:
    """服务上次被关时还在跑的任务，标记为失败（关窗即停，不会再继续）。"""
    with get_conn() as c:
        _ensure(c)
        cur = c.execute("UPDATE jobs SET status='failed', detail=detail || '\n软件关闭时中断' , updated_at=? "
                        "WHERE status='running' AND updated_at < ?", (time.time(), time.time() - 1))
        return cur.rowcount
