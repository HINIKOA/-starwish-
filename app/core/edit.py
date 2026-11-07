"""核心记忆 · 修改层：所有改动都走 apply_change，统一记版本、记日志、可回滚。

谁能直接改？
  - 主人渠道（面板 / 带主人令牌的 API / 代理里主人发的指令 / MCP 带主人原话的 update_core）→ 直接生效
  - 自动渠道（LLM 后台整理 / 导入 / AI 自发的 propose_core_change）→ 只能产生提案
锁定（locked）= 冻结：任何直接修改都被拒绝；远程主人指令在锁定时会自动转成提案，不会丢。
"""
from __future__ import annotations

import re
import time

from ..db import log_event
from . import history
from .store import (SECTIONS, WRITE_LOCK, ConfirmRequiredError, CoreLockedError, load_core, norm_section,
                    sections_of, write_core)

# 来源渠道（写进日志，面板里按中文显示）
SOURCE_TITLES = {
    "panel": "面板", "api": "API（主人令牌）", "mcp": "MCP（主人指令）", "proxy-command": "对话指令 /核心",
    "proxy-natural": "对话中主人原话", "command": "指令", "proposal": "批准提案", "rollback": "回到某版本",
    "undo": "撤销", "v1": "旧版本", "import": "导入",
}


def _merge(old: str, content: str, mode: str) -> str:
    content = (content or "").strip()
    if mode == "append":
        if not old.strip():
            return content
        if content and content in [ln.strip() for ln in old.splitlines()]:  # 已有这一行，不重复追加
            return old.strip()
        return old.rstrip() + "\n" + content
    return content


def set_line(old: str, key: str, value: str) -> str:
    """把「key：value」写进一段文本：已有同名行就替换，否则追加一行。"""
    value = value.strip()
    pat = re.compile(rf"^\s*{re.escape(key)}\s*[:：]")
    lines = (old or "").splitlines()
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = f"{key}：{value}"
            return "\n".join(lines).strip()
    # 没有「key：」开头的行，但正文里有「称呼「X」/ 称呼是X / 名字叫X」这类写法 → 就地替换，避免出现两个互相矛盾的称呼
    inline = re.compile(rf"{re.escape(key)}(?:为|是|叫|作|做)?\s*[「“\"『]([^」”\"』\n]+)[」”\"』]|{re.escape(key)}(?:为|是|叫)\s*([^，,。；;\s\n]+)")
    text = "\n".join(lines)
    if inline.search(text):
        return inline.sub(lambda m: f"{key}「{value}」", text, count=1).strip()
    lines.append(f"{key}：{value}")
    return "\n".join(lines).strip()


def apply_change(changes: dict, source: str, actor: str = "owner", note: str = "", kind: str = "edit",
                 instruction: str = "", respect_lock: bool = True, extra: dict | None = None) -> dict:
    """changes: {section: (content, mode)} 或 {section: content}（替换）。
    返回 {"applied": bool, "rev": int, "changed": [...], "core": {...}}"""
    with WRITE_LOCK:
        core = load_core()
        if respect_lock and core.get("locked"):
            raise CoreLockedError("核心记忆已上锁，请先在面板「核心记忆」模块里解锁")
        before_all = sections_of(core)
        changed, before, after = [], {}, {}
        for sec, val in changes.items():
            sec = norm_section(sec)
            content, mode = val if isinstance(val, tuple) else (val, "replace")
            if mode == "set_line":
                key, value = content
                new = set_line(before_all[sec], key, value)
            else:
                new = _merge(before_all[sec], content, mode)
            if new != before_all[sec].strip() and new != before_all[sec]:
                core["sections"][sec]["content"] = new
                changed.append(sec)
                before[sec], after[sec] = before_all[sec], new
        if not changed:
            return {"applied": False, "rev": core["rev"], "changed": [], "core": core, "reason": "内容没有变化"}
        core["rev"] = int(core.get("rev") or 0) + 1
        core["updated_at"] = time.time()
        write_core(core)
        log_event("core_change", rev=core["rev"], kind=kind, source=source, actor=actor, note=note or "",
                  instruction=instruction or "", changed=changed, before=before, after=after,
                  snapshot=sections_of(core), **(extra or {}))
        return {"applied": True, "rev": core["rev"], "changed": changed, "core": core}


# ---------------- 面板 / 旧接口 ----------------

def set_section(section: str, content: str, confirm: bool, actor: str = "owner", mode: str = "replace",
                source: str = "panel", note: str = "") -> dict:
    if not confirm:
        raise ConfirmRequiredError("修改核心记忆需要 confirm=true")
    if mode not in ("append", "replace"):
        mode = "replace"
    res = apply_change({norm_section(section): (content, mode)}, source=source, actor=actor, note=note)
    return res["core"]


def set_lock(locked: bool, confirm: bool, source: str = "panel", note: str = "") -> dict:
    if not confirm:
        raise ConfirmRequiredError("更改锁定状态需要 confirm=true")
    with WRITE_LOCK:
        core = load_core()
        core["locked"] = bool(locked)
        write_core(core)
    log_event("core_lock", locked=bool(locked), rev=core["rev"], source=source, note=note or "")
    return core


# ---------------- 回滚 / 撤销 ----------------

def rollback(rev: int | None = None, confirm: bool = False, mode: str = "to", source: str = "panel",
             legacy_no: int | None = None, note: str = "") -> dict:
    """mode="to"：回到第 rev 版修改后的状态（rev=0 表示最初的空白）；
    mode="undo"：撤销第 rev 版这一次修改（只把它改过的分区恢复成修改前）。"""
    if not confirm:
        raise ConfirmRequiredError("回滚核心记忆需要 confirm=true")
    if rev == 0 and mode == "to":
        target = {k: "" for k in SECTIONS}
        label = "r0（初始空白）"
    else:
        e = history.find_entry(rev=rev, legacy_no=legacy_no)
        if not e:
            raise KeyError(f"找不到版本 r{rev}" if rev is not None else f"找不到旧版本记录 #{legacy_no}")
        label = f"r{e['rev']}" if e.get("rev") is not None else f"旧记录#{e.get('legacy_no')}"
        if mode == "undo":
            target = dict(e["before"])
        else:
            target = dict(e["snapshot"] or e["after"])
    res = apply_change({k: (v, "replace") for k, v in target.items()}, source="undo" if mode == "undo" else "rollback",
                       kind="undo" if mode == "undo" else "rollback",
                       note=note or (f"撤销 {label} 的修改" if mode == "undo" else f"回到 {label}"),
                       extra={"target_rev": rev, "target_legacy": legacy_no, "rollback_mode": mode, "via": source})
    return res


def undo_last(source: str = "command") -> dict:
    """撤销最近一次还没被撤销过的修改。"""
    entries = [x for x in history.all_entries() if x["kind"] != "lock" and x.get("changed")]
    raw_undos = [e for e in history._iter_raw() if e.get("type") == "core_change" and e.get("kind") == "undo"]
    undone = {e.get("target_rev") for e in raw_undos if e.get("target_rev") is not None}
    undone_legacy = {e.get("target_legacy") for e in raw_undos if e.get("target_legacy") is not None}
    for x in reversed(entries):
        if x["kind"] == "undo" or (x.get("rev") is not None and x["rev"] in undone) \
                or (x.get("legacy_no") is not None and x["legacy_no"] in undone_legacy):
            continue
        if x.get("rev") is None:
            return rollback(legacy_no=x["legacy_no"], confirm=True, mode="undo", source=source)
        return rollback(rev=x["rev"], confirm=True, mode="undo", source=source)
    return {"applied": False, "rev": load_core()["rev"], "changed": [], "reason": "没有可以撤销的修改"}


# ---------------- 主人远程指令 ----------------

def owner_update(changes: dict, source: str, authorized: bool, instruction: str = "", note: str = "") -> dict:
    """主人从任意 AI 客户端发来的修改。
    authorized=True 且未上锁 → 直接生效；否则转为提案（等主人在面板批准）。"""
    from .proposals import create_proposal

    def as_proposals(reason):
        out = []
        for sec, val in changes.items():
            content, mode = val if isinstance(val, tuple) else (val, "replace")
            if mode == "set_line":
                key, value = content
                content, mode = set_line(load_core()["sections"][norm_section(sec)]["content"], key, value), "replace"
            out.append(create_proposal(sec, content, reason=reason + (f"（原话：{instruction}）" if instruction else ""),
                                       source=source, mode=mode))
        return {"applied": False, "proposed": True, "proposals": out, "reason": reason}

    if not authorized:
        return as_proposals("未经主人授权的修改，已转为提案")
    if load_core().get("locked"):
        return as_proposals("核心记忆已上锁，主人指令已转为提案")
    res = apply_change(changes, source=source, instruction=instruction, note=note)
    res["proposed"] = False
    return res
