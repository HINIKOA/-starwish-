"""核心记忆 · 存储层：三段恒定内容（core.json 为唯一真源，core.md 为只读镜像）。"""
from __future__ import annotations

import json
import os
import threading

from .. import config
from ..db import estimate_tokens

SECTIONS = {
    "identity": "AI身份与名字",
    "owner": "主人是谁及称呼",
    "preferences": "主人的喜好",
}
# 分区别名（指令里可以用中文简称）
_ALIASES = {v: k for k, v in SECTIONS.items()}
_ALIASES.update({
    "身份": "identity", "AI身份": "identity", "名字": "identity", "ai": "identity", "AI": "identity", "我是谁": "identity",
    "主人": "owner", "称呼": "owner", "主人是谁": "owner", "对方": "owner",
    "喜好": "preferences", "偏好": "preferences", "爱好": "preferences", "pref": "preferences", "prefs": "preferences",
})

# 进程内写锁（MCP 与网页服务是两个进程时，以 core.json 原子替换保证文件完整）
WRITE_LOCK = threading.RLock()


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
    if s.lower() in _ALIASES:
        return _ALIASES[s.lower()]
    raise ValueError(f"未知的核心记忆分区：{section}（只有 identity 身份 / owner 主人 / preferences 喜好）")


def default_core() -> dict:
    return {
        "version": 2,
        "rev": 0,
        "locked": False,
        "updated_at": None,
        "sections": {k: {"title": v, "content": ""} for k, v in SECTIONS.items()},
    }


def load_core() -> dict:
    config.ensure_dirs()
    if not config.CORE_JSON.exists():
        core = default_core()
        write_core(core)
        return core
    try:
        core = json.loads(config.CORE_JSON.read_text("utf-8"))
    except Exception:
        core = default_core()
    base = default_core()
    base.update({k: v for k, v in core.items() if k != "sections"})
    base["version"] = 2
    base["rev"] = int(core.get("rev") or 0)
    for k in SECTIONS:  # 严格只有三段
        base["sections"][k]["content"] = (core.get("sections", {}).get(k, {}) or {}).get("content", "")
    return base


def sections_of(core: dict) -> dict:
    return {k: core["sections"][k]["content"] for k in SECTIONS}


def render_md(core: dict) -> str:
    lines = ["# 星愿 · 核心记忆", "",
             f"> 版本 r{core.get('rev', 0)}{' · 🔒已上锁' if core.get('locked') else ''}。"
             "本文件由 core.json 自动生成，请在面板「核心记忆」模块或用主人指令修改（直接改此文件不会生效）。", ""]
    for k, title in SECTIONS.items():
        lines += [f"## {title}", "", core["sections"][k]["content"].strip() or "（空）", ""]
    return "\n".join(lines)


def write_core(core: dict) -> None:
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
    return "【星愿·核心记忆（恒定）】\n" + "\n".join(parts)


def core_tokens() -> int:
    return estimate_tokens(core_text())
