"""三路线索里的「台词」和「画面」两路：自我介绍识别、LLM 读台词推断名字、视觉模型回答里的【认人】标记。"""
from __future__ import annotations

import json
import re

NAME = r"[\u4e00-\u9fa5A-Za-z·]{1,8}"
_SELF = re.compile(rf"(?:我叫|我的名字(?:叫|是)|我名叫|鄙人|在下|本人)\s*({NAME})")
_BAD = {"什么", "谁", "一个", "这个", "那个", "自己", "大家", "你们", "我们", "他们", "警察", "医生", "老师", "朋友", "没有", "不是"}
TAG = "【认人】"
_TAG_RE = re.compile(r"【认人】([^\n]*)")
_ITEM = re.compile(r"((?:男|女|人)\d+|主人)\s*[=＝是:：]\s*([^（(；;,，。\s]{1,12})\s*(?:[（(]([^）)]{0,40})[）)])?")


def self_intro(text: str) -> str | None:
    """「我叫库珀」→ 库珀。"""
    m = _SELF.search(text or "")
    if not m:
        return None
    n = re.split(r"[，。！？、,.!?的了啊呀吧呢吗嘛]", m.group(1))[0]
    if len(n) < 1 or any(n.startswith(b) for b in _BAD):
        return None
    return n[:6]


def parse_tags(text: str) -> tuple[str, list[dict]]:
    """把视觉模型回答里的【认人】行拿出来：返回 (去掉标记后的话, [{label, name, note}])。"""
    items = []
    for m in _TAG_RE.finditer(text or ""):
        for it in _ITEM.finditer(m.group(1)):
            items.append({"label": it.group(1), "name": it.group(2).strip(), "note": (it.group(3) or "").strip()})
    clean = _TAG_RE.sub("", text or "").strip()
    return clean, items


class TagFilter:
    """流式输出时把【认人】标记藏起来，不让浏览器显示、不让 TTS 念出来。"""

    def __init__(self):
        self.buf = ""
        self.hidden = False

    def feed(self, delta: str) -> str:
        if self.hidden:
            return ""
        self.buf += delta
        i = self.buf.find(TAG)
        if i >= 0:
            out, self.buf, self.hidden = self.buf[:i], "", True
            return out
        # 末尾可能是标记的开头（如「【认」），先扣住
        keep = 0
        for k in range(1, len(TAG)):
            if self.buf.endswith(TAG[:k]):
                keep = k
        out = self.buf[: len(self.buf) - keep] if keep else self.buf
        self.buf = self.buf[len(out):]
        return out

    def flush(self) -> str:
        out = "" if self.hidden else self.buf
        self.buf = ""
        return out


VISUAL_RULE = ("【宇宙之声】你还能听出是谁在说话。说话人表：{roster}。{recent}"
               "如果从画面里能看出某个说话人是谁（比如字幕、片头人名、别人对着他喊名字，或者正在说话的人的样子），"
               "就在回答最后另起一行写：【认人】男1=库珀（画面依据，10 字内）；可以写多个，用分号隔开。"
               "看不出就不要写这一行，也不要瞎猜。说话时请用说话人表里的名字称呼他们。")


def recent_line(recent: list[tuple[dict, float, float]], display) -> str:
    if not recent:
        return ""
    parts = []
    for s, a, b in recent[-6:]:
        parts.append(f"{display(s)} {a:.0f}~{b:.0f} 秒前" if b >= 1 else f"{display(s)} 刚才（{a:.0f} 秒前开始）")
    return "最近说话的人（相对现在，对应分镜图上的时间）：" + "，".join(parts) + "。"


IDENTIFY_PROMPT = """下面是一部影片「{title}」的台词，每句前面标着说话人编号（声纹识别得到，可能有少量错误）。
现在的说话人表：{roster}

请根据台词里的称呼（比如 A 对 B 说「库珀，你得走了」，那 B 很可能叫库珀）、自我介绍、别人提到的名字，推断每个编号的名字。
只输出 JSON 数组，例如：[{{"speaker": "男1", "name": "库珀", "evidence": "女1 喊他库珀", "confidence": 0.8}}]
没有把握的不要写；名字只写称呼本身（不要带「先生」「老师」之类）；不知道就输出 []。

台词：
{lines}"""


def identify_messages(title: str, roster: str, lines: list[str]) -> list[dict]:
    return [{"role": "user", "content": IDENTIFY_PROMPT.format(title=title or "未知影片", roster=roster or "（空）",
                                                              lines="\n".join(lines[-80:]))}]


def parse_identify(text: str) -> list[dict]:
    s = (text or "").strip()
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for x in arr if isinstance(arr, list) else []:
        if not isinstance(x, dict):
            continue
        spk, name = str(x.get("speaker", "")).strip(), str(x.get("name", "")).strip()
        try:
            conf = float(x.get("confidence", 0.6))
        except Exception:
            conf = 0.6
        if spk and name and conf >= 0.5:
            out.append({"speaker": spk, "name": name, "evidence": str(x.get("evidence", ""))[:80], "confidence": conf})
    return out
