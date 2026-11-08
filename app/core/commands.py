"""核心记忆 · 主人指令解析。

两种写法（只看最后一条用户消息）：

1) 斜杠指令（精确，推荐）
   /核心                      查看当前核心记忆
   /核心 帮助                 指令说明
   /核心 历史                 最近 5 次修改
   /核心 撤销                 撤销最近一次修改
   /核心 回到 r3              回到第 3 版
   /核心 主人 九天，称呼天天   替换「主人是谁及称呼」整段
   /核心 喜好 + 爱喝冰美式     给「主人的喜好」追加一行
   /核心 称呼 = 天天          只改「称呼：」这一行（名字 = 小星 同理）

2) 自然语言（需以「记住」开头，或包含「以后叫我 / 以后你叫」等明确说法）
   记住，以后叫我天天          → 主人段「称呼：天天」
   记住，你以后叫小星          → 身份段「名字：小星」
   记住，我叫九天              → 主人段「名字：九天」
   记住，我喜欢吃辣 / 我不吃香菜 → 喜好段追加一行
"""
from __future__ import annotations

import re

from . import edit, history
from .store import SECTIONS, load_core, norm_section

SLASH = re.compile(r"^\s*[/／](核心|core)(?:\s+|$)(.*)$", re.S | re.I)
_Q = "「」“”\"'‘’《》"
_END = r"[\s。．.!！~～]*$"

HELP = ("核心记忆指令：\n"
        "/核心 —— 查看\n/核心 历史 —— 最近修改\n/核心 撤销 —— 撤销最近一次修改\n/核心 回到 r3 —— 回到第 3 版\n"
        "/核心 主人 <内容> —— 替换整段（分区：身份 / 主人 / 喜好）\n/核心 喜好 + <内容> —— 追加一行\n"
        "/核心 称呼 = 天天 —— 只改「称呼：」那一行（名字 = 小星 同理）\n"
        "也可以直接说：记住，以后叫我X / 记住，你叫X / 记住，我喜欢X")

LINE_KEYS = {"称呼": ("owner", "称呼"), "名字": ("identity", "名字"), "昵称": ("owner", "称呼")}


def _clean(v: str) -> str:
    v = re.sub(_END, "", v.strip())
    v = re.sub(r"(吧|就好|就行|好了|就可以了|可以吗|好吗|哦|啦)$", "", v)
    return v.strip(_Q + " ，,")


def parse(text: str, natural: bool = True) -> dict | None:
    """返回 None（不是指令）或 {"type": "slash"|"natural", "op": ..., ...}"""
    text = (text or "").strip()
    if not text:
        return None
    m = SLASH.match(text)
    if m:
        return _parse_slash(m.group(2).strip())
    if natural and len(text) <= 160:
        return _parse_natural(text)
    return None


def _parse_slash(rest: str) -> dict:
    base = {"type": "slash"}
    if rest in ("", "查看", "show", "看", "看看"):
        return {**base, "op": "show"}
    if rest in ("帮助", "help", "?", "？"):
        return {**base, "op": "help"}
    if rest in ("历史", "history", "版本"):
        return {**base, "op": "history"}
    if rest in ("撤销", "undo", "撤回"):
        return {**base, "op": "undo"}
    m = re.match(r"^(?:回到|回滚到?|rollback)\s*r?(\d+)\s*$", rest, re.I)
    if m:
        return {**base, "op": "rollback", "rev": int(m.group(1))}
    m = re.match(r"^(\S+?)\s*[=＝]\s*(.+)$", rest, re.S)
    if m and m.group(1) in LINE_KEYS:
        sec, key = LINE_KEYS[m.group(1)]
        return {**base, "op": "edit", "changes": {sec: ((key, _clean(m.group(2))), "set_line")}}
    m = re.match(r"^(\S+?)\s*(?:[+＋]|追加)\s*(.+)$", rest, re.S)
    if m:
        try:
            return {**base, "op": "edit", "changes": {norm_section(m.group(1)): (m.group(2).strip(), "append")}}
        except ValueError:
            pass
    m = re.match(r"^(\S+?)(?:\s+|[:：])(.+)$", rest, re.S)
    if m:
        try:
            return {**base, "op": "edit", "changes": {norm_section(m.group(1)): (m.group(2).strip(), "replace")}}
        except ValueError:
            pass
    return {**base, "op": "help", "error": f"没看懂这条指令：{rest}"}


_TRIGGER = re.compile(r"^(?:请|你要|帮我)?(?:记住|记好|记一下)[了啊呀哦]?[，,：:。\s]*(.*)$", re.S)
_CALL_ME = re.compile(r"(?:以后|今后|从现在起|从今天起)?[，,\s]*(?:你)?(?:就)?(?:都)?(?:叫|称呼|喊)我(?:为|做|作)?\s*(.+)$", re.S)
_YOUR_NAME = re.compile(r"你(?:以后|今后|从现在起)?(?:的名字)?(?:就)?(?:叫|是|改名叫|改叫)\s*(.+)$", re.S)
_MY_NAME = re.compile(r"^我(?:的名字)?(?:叫|是叫|名叫|的名字是)\s*(.+)$", re.S)
_PREF = re.compile(r"^我(?:最|很|特别|比较)?(喜欢|爱|偏爱|偏好|讨厌|不喜欢|不吃|不爱|不要|不能吃|对.+过敏)(.*)$", re.S)


def _parse_natural(text: str) -> dict | None:
    m = _TRIGGER.match(text)
    explicit = bool(m)
    body = (m.group(1) if m else text).strip()
    if not explicit and not re.search(r"(以后|今后|从现在起|从今天起)[，,\s]*(你)?(就)?(都)?(叫|称呼|喊)我|你以后(就)?叫", body):
        return None
    base = {"type": "natural", "op": "edit", "instruction": text}
    m2 = _CALL_ME.search(body)
    if m2:
        v = _clean(m2.group(1))
        if v and len(v) <= 20:
            return {**base, "changes": {"owner": (("称呼", v), "set_line")}, "summary": f"称呼：{v}"}
    m3 = _YOUR_NAME.search(body)
    if m3:
        v = _clean(m3.group(1))
        if v and len(v) <= 20:
            return {**base, "changes": {"identity": (("名字", v), "set_line")}, "summary": f"名字：{v}"}
    if explicit:
        m5 = _MY_NAME.match(body)
        if m5:
            v = _clean(m5.group(1))
            if v and len(v) <= 20:
                return {**base, "changes": {"owner": (("名字", v), "set_line")}, "summary": f"主人名字：{v}"}
        m4 = _PREF.match(body)
        if m4:
            line = _clean(m4.group(1) + m4.group(2))
            if line and len(line) <= 80:
                return {**base, "changes": {"preferences": (line, "append")}, "summary": line}
    return None


# ---------------- 执行 ----------------

def _fmt_core() -> str:
    c = load_core()
    lines = [f"【星愿·核心记忆】r{c['rev']}{' 🔒已上锁' if c.get('locked') else ''}"]
    for k, t in SECTIONS.items():
        lines.append(f"▸{t}：{c['sections'][k]['content'].strip() or '（空）'}")
    return "\n".join(lines)


def _fmt_history(n: int = 5) -> str:
    items = history.list_history(n, with_diff=False, include_lock=False)
    if not items:
        return "还没有修改记录。"
    out = ["最近的核心记忆修改："]
    for x in items:
        tag = f"r{x['rev']}" if x.get("rev") is not None else "旧"
        secs = "、".join(SECTIONS.get(s, s) for s in x["changed"])
        out.append(f"{tag} · {x.get('ts', '')[:16].replace('T', ' ')} · {edit.SOURCE_TITLES.get(x['source'], x['source'])} · {secs}"
                   + (f" · {x['note']}" if x.get("note") else ""))
    return "\n".join(out)


def _describe(res: dict) -> str:
    if res.get("proposed"):
        ids = "、".join(f"#{p['id']}" for p in res.get("proposals", []))
        return f"⚠ {res.get('reason')}：提案 {ids}，请到星愿面板「核心记忆」模块批准。"
    if not res.get("applied"):
        return "核心记忆没有变化（" + (res.get("reason") or "内容相同") + "）。"
    secs = "、".join(SECTIONS[s] for s in res["changed"])
    return f"✅ 已更新核心记忆 r{res['rev']}（{secs}）。说「/核心 撤销」可以撤回。"


def execute(cmd: dict, source: str, authorized: bool) -> dict:
    """执行一条已解析的指令，返回 {"reply": 文本, "result": ...}"""
    op = cmd.get("op")
    if op == "show":
        return {"reply": _fmt_core(), "result": None}
    if op == "help":
        return {"reply": ((cmd.get("error") + "\n\n") if cmd.get("error") else "") + HELP, "result": None}
    if op == "history":
        return {"reply": _fmt_history(), "result": None}
    if op in ("undo", "rollback"):
        if not authorized:
            return {"reply": "只有主人可以撤销或回滚核心记忆。", "result": None}
        if load_core().get("locked"):
            return {"reply": "核心记忆已上锁，请先在面板解锁再回滚。", "result": None}
        try:
            res = edit.undo_last(source=source) if op == "undo" else edit.rollback(rev=cmd["rev"], confirm=True, source=source)
        except KeyError as e:
            return {"reply": str(e).strip("'"), "result": None}
        res["proposed"] = False
        return {"reply": _describe(res) + ("\n" + _fmt_core() if res.get("applied") else ""), "result": res}
    if op == "edit":
        res = edit.owner_update(cmd["changes"], source=source, authorized=authorized,
                                instruction=cmd.get("instruction", ""))
        return {"reply": _describe(res), "result": res}
    return {"reply": HELP, "result": None}


def run_text(text: str, source: str, authorized: bool, natural: bool = True) -> dict | None:
    cmd = parse(text, natural=natural)
    if not cmd:
        return None
    out = execute(cmd, source=source, authorized=authorized)
    out["cmd"] = {k: v for k, v in cmd.items() if k != "changes"}
    return out
