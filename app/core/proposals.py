"""核心记忆 · 提案：LLM 后台整理、导入、AI 自发的修改都只进入这里，等主人批准。"""
from __future__ import annotations

import time

from ..db import get_conn, log_event
from .edit import set_section
from .store import SECTIONS, ConfirmRequiredError, norm_section


# ---------------- 提案 ----------------

def create_proposal(section: str, content: str, reason: str = "", source: str = "", mode: str = "append") -> dict:
    sec = norm_section(section)
    if mode not in ("append", "replace"):
        mode = "append"
    content = (content or "").strip()
    if not content:
        raise ValueError("提案内容为空")
    with get_conn() as c:
        dup = c.execute(
            "SELECT id FROM proposals WHERE section=? AND content=? AND status='pending'", (sec, content)
        ).fetchone()
        if dup:
            return get_proposal(dup["id"], c)
        cur = c.execute(
            "INSERT INTO proposals(section, mode, content, reason, source, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sec, mode, content, reason or "", source or "", "pending", time.time()),
        )
        pid = cur.lastrowid
        p = get_proposal(pid, c)
    log_event("proposal_create", id=pid, section=sec, mode=mode, content=content, reason=reason, source=source)
    return p


def get_proposal(pid: int, conn=None) -> dict | None:
    def _q(c):
        r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["section_title"] = SECTIONS.get(d["section"], d["section"])
        return d

    if conn is not None:
        return _q(conn)
    with get_conn() as c:
        return _q(c)


def list_proposals(status: str | None = "pending") -> list[dict]:
    with get_conn() as c:
        if status:
            rows = c.execute("SELECT * FROM proposals WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM proposals ORDER BY id DESC LIMIT 500").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["section_title"] = SECTIONS.get(d["section"], d["section"])
        out.append(d)
    return out


def decide_proposal(pid: int, approve: bool, confirm: bool, edited_content: str | None = None) -> dict:
    p = get_proposal(pid)
    if not p:
        raise KeyError("提案不存在")
    if p["status"] != "pending":
        raise ValueError("该提案已处理")
    if approve:
        if not confirm:
            raise ConfirmRequiredError("批准提案会修改核心记忆，需要 confirm=true")
        content = edited_content if edited_content is not None else p["content"]
        set_section(p["section"], content, confirm=True, actor=f"proposal#{pid}", mode=p["mode"], source="proposal",
                    note=f"批准提案 #{pid}" + (f"：{p['reason']}" if p.get("reason") else ""))
    with get_conn() as c:
        c.execute(
            "UPDATE proposals SET status=?, decided_at=? WHERE id=?",
            ("approved" if approve else "rejected", time.time(), pid),
        )
    log_event("proposal_" + ("approve" if approve else "reject"), id=pid)
    return get_proposal(pid)
