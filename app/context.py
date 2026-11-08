"""上下文预算（v0.11.0）：凡是需要上下文的地方（对话抽屉、主模型整理星系）默认按 400,000 tokens 的窗口分配。
对话监控（watch）刻意不带上下文，不走这里。"""
from __future__ import annotations

from .db import estimate_tokens  # noqa: F401（organize 也从这里用）

DEFAULT_WINDOW = 400_000


def window(cfg: dict, role: str = "chat") -> int:
    llm = cfg.get("llm") or {}
    key = "maint_context_tokens" if role == "maintain" else "context_tokens"
    try:
        v = int(llm.get(key) or DEFAULT_WINDOW)
    except (TypeError, ValueError):
        v = DEFAULT_WINDOW
    return max(4000, v)


def budget(cfg: dict, role: str = "chat", share: float = 0.7, reserve: int = 8000) -> int:
    """可用于输入的 token 数：窗口 × share，再留出回复余量。"""
    return max(1000, int(window(cfg, role) * share) - reserve)


def msg_tokens(m: dict) -> int:
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return estimate_tokens(str(c or "")) + 4


def trim_history(msgs: list[dict], limit_tokens: int) -> list[dict]:
    """从最新往回保留，总量不超过预算；至少保留最后一条。"""
    out, total = [], 0
    for m in reversed(msgs):
        t = msg_tokens(m)
        if out and total + t > limit_tokens:
            break
        out.append(m)
        total += t
    return list(reversed(out))


def pack(lines: list[str], limit_tokens: int) -> tuple[list[str], int]:
    """按顺序装行，直到预算用完。返回 (装进去的, 用掉的 tokens)。"""
    out, total = [], 0
    for ln in lines:
        t = estimate_tokens(ln) + 1
        if total + t > limit_tokens:
            break
        out.append(ln)
        total += t
    return out, total


def main_ready(cfg: dict) -> bool:
    llm = cfg.get("llm") or {}
    return bool(llm.get("base_url") and llm.get("model") and (llm.get("api_key") or "localhost" in llm.get("base_url", "")
                                                              or "127.0.0.1" in llm.get("base_url", "")))


def main_complete(messages: list[dict], cfg: dict, tools: list | None = None, temperature: float = 0.2) -> dict:
    """调用主模型（默认对话模型，按服务商协议）。返回 chat.completion 字典。测试里会被替换。"""
    from . import upstream
    llm = cfg["llm"]
    body = {"model": llm["model"], "messages": messages, "temperature": temperature, "stream": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return upstream.complete(llm.get("protocol") or "openai", llm["base_url"], llm.get("api_key", ""), body,
                             timeout=float(llm.get("timeout", 120)))


# ---------------- 自动压缩 ----------------
COMPACT_PROMPT = """下面是一段较早的对话历史（主人与「星愿」助手）。把它压缩成一份摘要，供之后继续对话时参考：
保留主人的要求与偏好、做过的操作与结果、尚未完成的事、重要的编号 / 名称；去掉寒暄和重复。用中文，条目式，不超过 1500 字。

<<HISTORY>>"""
SUMMARY_TAG = "【之前对话的摘要】"


def compact_threshold(cfg: dict, role: str = "chat") -> int:
    llm = cfg.get("llm") or {}
    try:
        th = int(llm.get("compact_tokens") or 380_000)
    except (TypeError, ValueError):
        th = 380_000
    return max(2000, min(th, window(cfg, role)))


def total_tokens(msgs: list[dict]) -> int:
    return sum(msg_tokens(m) for m in msgs)


def _plain_summary(msgs: list[dict], limit_chars: int = 3000) -> str:
    out = []
    for m in msgs:
        c = str(m.get("content") or "").replace("\n", " ")
        out.append(f"{'主人' if m.get('role') == 'user' else '星愿'}：{c[:200]}")
    txt = "\n".join(out)
    return txt[-limit_chars:]


def compact_if_needed(msgs: list[dict], cfg: dict, role: str = "chat", where: str = "对话抽屉",
                      summarize=None, extra_tokens: int = 0) -> tuple[list[dict], dict | None]:
    """累计上下文（历史 + 这次要加的 extra_tokens）达到阈值（默认 380,000）→ 把较早的历史压成一段摘要，
    保留摘要 + 最近几轮（约窗口的 15%）。记日志、在任务抽屉里显示。返回 (新历史, 压缩信息或 None)。"""
    th = compact_threshold(cfg, role)
    before = total_tokens(msgs) + extra_tokens
    if before < th or len(msgs) < 3:
        return msgs, None
    from . import jobs
    from .db import log_event
    keep_budget = max(1000, int(window(cfg, role) * 0.15))
    recent = trim_history(msgs, keep_budget)
    if len(recent) >= len(msgs):
        recent = msgs[-2:]
    old = msgs[: len(msgs) - len(recent)]
    jid = jobs.start("compact", f"自动压缩上下文（{where}）")
    by_llm = False
    text = ""
    try:
        hist = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in old)
        hist_lim = budget(cfg, role)
        if estimate_tokens(hist) > hist_lim:
            hist = hist[-hist_lim:]
        if summarize is not None:
            text = summarize(COMPACT_PROMPT.replace("<<HISTORY>>", hist))
            by_llm = True
        elif main_ready(cfg):
            data = main_complete([{"role": "user", "content": COMPACT_PROMPT.replace("<<HISTORY>>", hist)}], cfg)
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            by_llm = bool(text.strip())
    except Exception as e:  # noqa: BLE001  —— 模型压缩失败就朴素截取
        text = ""
        log_event("context_compact_error", where=where, error=str(e)[:200])
    if not text.strip():
        text = _plain_summary(old)
    prev = [m for m in old if str(m.get("content", "")).startswith(SUMMARY_TAG)]
    if prev and SUMMARY_TAG not in text:   # 旧摘要不要丢
        text = str(prev[-1]["content"])[len(SUMMARY_TAG):][:2000] + "\n" + text
    new = [{"role": "system", "content": SUMMARY_TAG + text.strip()}] + recent
    info = {"where": where, "before": before, "after": total_tokens(new) + extra_tokens, "threshold": th,
            "dropped": len(old), "kept": len(recent), "by_llm": by_llm}
    log_event("context_compact", **info)
    jobs.finish(jid, "done", f"{before:,} → {info['after']:,} tokens（阈值 {th:,}），压缩了 {len(old)} 条，保留最近 {len(recent)} 条"
                + ("" if by_llm else "（朴素摘要）"))
    return new, info
