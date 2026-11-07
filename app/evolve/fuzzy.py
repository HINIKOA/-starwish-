"""模糊查找替换（参考 Hermes Agent tools/fuzzy_match 的思路，独立实现）。

AI 打补丁时往往记不清原文的每个空格、缩进、全半角标点，所以按以下顺序逐级放宽：
  1. exact            精确匹配
  2. line_trim        每行去掉首尾空白后逐行匹配（缩进 / 行尾空格差异）
  3. whitespace       把连续空白压成一个空格后匹配（制表符 vs 空格、换行差异）
  4. punct            再把全角标点归一成半角（，→, ：→: 等）
  5. block_anchor     只用首行 + 末行做锚点（中间内容 AI 记错了也能定位，仅限 ≥3 行且锚点唯一）
返回 (新文本, 替换次数, 使用的策略, 错误信息)。
"""
from __future__ import annotations

import re

_PUNCT = str.maketrans({"，": ",", "。": ".", "：": ":", "；": ";", "！": "!", "？": "?", "（": "(", "）": ")",
                        "“": '"', "”": '"', "‘": "'", "’": "'", "　": " ", "【": "[", "】": "]"})


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _spans_by_lines(content: str, old: str, norm) -> list[tuple[int, int]]:
    """按行归一后匹配，返回原文中的 (start, end) 字符区间。"""
    c_lines = content.splitlines(keepends=True)
    o_lines = [norm(x) for x in old.strip("\n").splitlines()]
    if not o_lines or not any(o_lines):
        return []
    offs, pos = [], 0
    for ln in c_lines:
        offs.append(pos)
        pos += len(ln)
    n = len(o_lines)
    out = []
    for i in range(len(c_lines) - n + 1):
        if all(norm(c_lines[i + k]) == o_lines[k] for k in range(n)):
            end = offs[i + n - 1] + len(c_lines[i + n - 1].rstrip("\r\n"))
            out.append((offs[i], end))
    return out


def _spans_by_flat(content: str, old: str, norm) -> list[tuple[int, int]]:
    """把空白压缩后匹配：建立「压缩后下标 → 原文下标」映射。"""
    target = norm(old)
    if not target:
        return []
    flat, idx = [], []
    prev_space = True
    for i, ch in enumerate(content):
        ch2 = ch.translate(_PUNCT) if norm is _flat_punct else ch
        if ch2.isspace():
            if not prev_space:
                flat.append(" ")
                idx.append(i)
            prev_space = True
        else:
            flat.append(ch2)
            idx.append(i)
            prev_space = False
    s = "".join(flat)
    out, start = [], 0
    while True:
        j = s.find(target, start)
        if j < 0:
            break
        out.append((idx[j], idx[j + len(target) - 1] + 1))
        start = j + max(1, len(target))
    return out


def _flat_ws(s: str) -> str:
    return _ws(s)


def _flat_punct(s: str) -> str:
    return _ws(s.translate(_PUNCT))


def _spans_anchor(content: str, old: str) -> list[tuple[int, int]]:
    o = [x.strip() for x in old.strip("\n").splitlines()]
    if len(o) < 3 or not o[0] or not o[-1]:
        return []
    c = content.splitlines(keepends=True)
    offs, pos = [], 0
    for ln in c:
        offs.append(pos)
        pos += len(ln)
    out = []
    for i, ln in enumerate(c):
        if ln.strip() != o[0]:
            continue
        # 末行锚点必须在合理范围内（原块行数的 0.5~2 倍）
        for j in range(i + max(1, len(o) // 2 - 1), min(len(c), i + 2 * len(o) + 1)):
            if c[j].strip() == o[-1]:
                out.append((offs[i], offs[j] + len(c[j].rstrip("\r\n"))))
                break
    return out


def fuzzy_find_and_replace(content: str, old: str, new: str, replace_all: bool = False):
    if old is None or old == "":
        return content, 0, "", "old_string 不能为空"
    if old == new:
        return content, 0, "", "old_string 与 new_string 相同，没有需要修改的内容"
    strategies = [
        ("exact", lambda: _exact(content, old)),
        ("line_trim", lambda: _spans_by_lines(content, old, lambda s: s.strip())),
        ("whitespace", lambda: _spans_by_flat(content, old, _flat_ws)),
        ("punct", lambda: _spans_by_flat(content, old, _flat_punct)),
        ("block_anchor", lambda: _spans_anchor(content, old)),
    ]
    for name, fn in strategies:
        spans = fn()
        if not spans:
            continue
        if len(spans) > 1 and not replace_all:
            return content, len(spans), name, f"找到 {len(spans)} 处匹配（策略 {name}），请提供更多上下文让它唯一，或设置 replace_all=true"
        if name == "block_anchor" and len(spans) > 1:
            return content, len(spans), name, "锚点匹配不唯一，请提供更精确的原文"
        out, last = [], 0
        for a, b in spans:
            out.append(content[last:a])
            out.append(new)
            last = b
        out.append(content[last:])
        return "".join(out), len(spans), name, None
    return content, 0, "", "没有找到要替换的原文（已尝试精确、去缩进、压缩空白、标点归一、首尾锚点）"


def _exact(content: str, old: str) -> list[tuple[int, int]]:
    out, i = [], 0
    while True:
        j = content.find(old, i)
        if j < 0:
            return out
        out.append((j, j + len(old)))
        i = j + len(old)
