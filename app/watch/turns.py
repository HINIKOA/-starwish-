"""一轮对话：主人说的话 + AI 的回复 + 用到的工具。监控 AI 只看这一轮，没有上下文。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class Turn:
    client: str
    session_id: str = ""
    cwd: str = ""
    ts: str = ""                 # ISO 8601
    user_text: str = ""
    assistant_text: str = ""
    tool_summary: str = ""
    turn_complete: bool = False
    start: int = 0               # 在源里的位置（JSONL 为字节偏移；OpenCode 为毫秒时间戳）
    end: int = 0
    tools: list = field(default_factory=list)

    def add_assistant(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.assistant_text = (self.assistant_text + "\n" + text).strip() if self.assistant_text else text

    def add_tool(self, desc: str) -> None:
        desc = (desc or "").strip()
        if desc:
            self.tools.append(desc)
            self.tool_summary = "; ".join(self.tools)[:800]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("tools", None)
        return d


def clip(text: str, limit: int) -> str:
    """长文本保留开头和结尾（结尾通常是结论）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head - 12
    return text[:head] + "\n…（中间省略）…\n" + text[-tail:]


_TRIVIAL = re.compile(r"^(好|好的|嗯|ok|okay|谢谢|thx|thanks|收到|继续|行|可以|是的|对|y|yes|hi|hello|你好|在吗)[!！。.~～]*$", re.I)


def is_trivial(t: Turn, min_chars: int = 12) -> bool:
    """琐碎的一轮（寒暄、单字回复且 AI 也没干活）直接跳过，不花模型调用。"""
    u = (t.user_text or "").strip()
    a = (t.assistant_text or "").strip()
    if not u and not a:
        return True
    if t.tool_summary:
        return False
    if _TRIVIAL.match(u) and len(a) < 40:
        return True
    return len(u) + len(a) < min_chars


def tool_desc(name: str, args) -> str:
    """工具调用写成一小段：名字 + 最关键的参数（文件 / 命令 / 路径）。"""
    name = str(name or "tool")
    key = ""
    if isinstance(args, str):
        try:
            import json
            args = json.loads(args)
        except Exception:
            key = args
    if isinstance(args, dict):
        for k in ("file_path", "path", "command", "cmd", "pattern", "url", "query", "filePath"):
            v = args.get(k)
            if v:
                key = " ".join(map(str, v)) if isinstance(v, list) else str(v)
                break
    key = key.replace("\n", " ").strip()
    return f"{name}({key[:80]})" if key else name
