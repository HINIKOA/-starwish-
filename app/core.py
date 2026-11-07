"""核心记忆：恒定的三段，永不被自动改写。

- 只能通过面板 / API 并携带 confirm=true 修改；锁定时任何修改都被拒绝。
- LLM 维护只能创建「提案」，由主人在面板里批准。
"""
from __future__ import annotations

import json
import os
import time

from . import config
from .db import estimate_tokens, get_conn, log_event

SECTIONS = {
    "identity": "AI身份与名字",
    "owner": "主人是谁及称呼",
    "preferences": "主人的喜好",
}
_ALIASES = {v: k for k, v in SECTIONS.items()}


class CoreLockedError(Exception):
    pass


class ConfirmRequiredError(Exception):
    pass


def norm_section(section: str) -> str:
    s = (section or "").strip()
    if s in SECTIONS:
        return s
    if s in _ALIASES:
        return _ALIASES[s]
    raise ValueError(f"未知的核心记忆分区：{section}（只有 identity / owner / preferences）")


def _default_core() -> dict:
    return {
        "version": 1,
        "locked": False,
        "updated_at": None,
        "sections": {k: {"title": v, "content": ""} for k, v in SECTIONS.items()},
    }


def load_core() -> dict:
    config.ensure_dirs()
    if not config.CORE_JSON.exists():
        core = _default_core()
        _write(core)
        return core
    try:
        core = json.loads(config.CORE_JSON.read_text("utf-8"))
    except Exception:
        core = _default_core()
    base = _default_core()
    base.update({k: v for k, v in core.items() if k != "sections"})
    for k in SECTIONS:  # 严格只有三段
        base["sections"][k]["content"] = (core.get("sections", {}).get(k, {}) or {}).get("content", "")
    return base


def render_md(core: dict) -> str:
    lines = ["# 星魂 · 核心记忆", "", "> 本文件由 core.json 自动生成，请在面板中修改（直接改此文件不会生效）。", ""]
    for k, title in SECTIONS.items():
        lines += [f"## {title}", "", core["sections"][k]["content"].strip() or "（空）", ""]
    return "\n".join(lines)


def _write(core: dict) -> None:
    tmp = config.CORE_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(core, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, config.CORE_JSON)
    config.CORE_MD.write_text(render_md(core), "utf-8")


def core_text(core: dict | None = None) -> str:
    """注入用的精简文本（token 极少）。"""
    core = core or load_core()
    parts = []
    for k, title in SECTIONS.items():
        c = core["sections"][k]["content"].strip()
        if c:
            parts.append(f"▸{title}：{c}")
    if not parts:
        return ""
    return "【星魂·核心记忆（恒定）】\n" + "\n".join(parts)


def core_tokens() -> int:
    return estimate_tokens(core_text())


def set_section(section: str, content: str, confirm: bool, actor: str = "owner", mode: str = "replace") -> dict:
    if not confirm:
        raise ConfirmRequiredError("修改核心记忆需要 confirm=true")
    sec = norm_section(section)
    core = load_core()
    if core.get("locked"):
        raise CoreLockedError("核心记忆已上锁，请先解锁")
    old = core["sections"][sec]["content"]
    if mode == "append" and old.strip():
        new = old.rstrip() + "\n" + content.strip()
    else:
        new = content.strip()
    core["sections"][sec]["content"] = new
    core["updated_at"] = time.time()
    _write(core)
    log_event("core_edit", section=sec, old=old, new=new, actor=actor)
    return core


def set_lock(locked: bool, confirm: bool) -> dict:
    if not confirm:
        raise ConfirmRequiredError("更改锁定状态需要 confirm=true")
    core = load_core()
    core["locked"] = bool(locked)
    _write(core)
    log_event("core_lock", locked=bool(locked))
    return core


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
        set_section(p["section"], content, confirm=True, actor=f"proposal#{pid}", mode=p["mode"])
    with get_conn() as c:
        c.execute(
            "UPDATE proposals SET status=?, decided_at=? WHERE id=?",
            ("approved" if approve else "rejected", time.time(), pid),
        )
    log_event("proposal_" + ("approve" if approve else "reject"), id=pid)
    return get_proposal(pid)
