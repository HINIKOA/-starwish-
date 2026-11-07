"""宇宙之眼 · 会话：文字版「画面日记」、预算、主动吐槽限速、结束时沉淀成星星。

画面（图片）默认不落盘；日记只记文字：主人说的话、AI 的话、台词、场景备注。
存放：soul/eye/sessions/<id>.jsonl（第一行是 meta）。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .. import config, core, stars
from ..db import log_event

_lock = threading.RLock()
PACE_GAP = {"off": None, "calm": 30.0, "normal": 15.0, "chatty": 8.0}
CHARS_PER_SEC = 5.0  # 估算朗读时长
KINDS = ("user", "ai", "subtitle", "scene", "note")


class BudgetError(RuntimeError):
    pass


class SessionNotFound(KeyError):
    pass


def eye_dir() -> Path:
    d = config.soul_path("eye", "sessions")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(sid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f\-]{6,40}", sid or ""):
        raise SessionNotFound(sid)
    return eye_dir() / f"{sid}.jsonl"


def _read(sid: str) -> tuple[dict, list[dict]]:
    p = _path(sid)
    if not p.exists():
        raise SessionNotFound(sid)
    lines = [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]
    meta = lines[0] if lines else {}
    entries = [x for x in lines[1:] if x.get("kind") != "_meta"]
    for x in lines[1:]:
        if x.get("kind") == "_meta":
            meta.update(x.get("patch") or {})
    return meta, entries


def _append(sid: str, obj: dict) -> None:
    with _lock, _path(sid).open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def start(title: str = "", source: str = "screen", engine: str = "") -> dict:
    sid = uuid.uuid4().hex[:12]
    meta = {"kind": "_head", "id": sid, "title": (title or "").strip() or "一起看", "source": source,
            "engine": engine, "started_at": time.time(), "ended_at": None, "star_id": None}
    with _lock:
        _path(sid).write_text(json.dumps(meta, ensure_ascii=False) + "\n", "utf-8")
    log_event("eye_start", id=sid, title=meta["title"], source=source, engine=engine)
    return public(meta, [])


def _patch(sid: str, **kw) -> None:
    _append(sid, {"kind": "_meta", "t": time.time(), "patch": kw})


def add(sid: str, kind: str, text: str, **extra) -> dict:
    if kind not in KINDS:
        raise ValueError(f"未知日记类型：{kind}")
    text = (text or "").strip()
    if not text:
        raise ValueError("内容为空")
    meta, _ = _read(sid)
    if meta.get("ended_at"):
        raise ValueError("这次观看已经结束")
    e = {"kind": kind, "t": time.time(), "text": text[:2000], **{k: v for k, v in extra.items() if v is not None}}
    _append(sid, e)
    return e


def public(meta: dict, entries: list[dict]) -> dict:
    m = {k: v for k, v in meta.items() if k != "kind"}
    m["count"] = {k: sum(1 for e in entries if e["kind"] == k) for k in KINDS}
    end = m.get("ended_at") or time.time()
    m["minutes"] = round((end - m.get("started_at", end)) / 60, 1)
    return m


def get(sid: str, with_entries: bool = True) -> dict:
    meta, entries = _read(sid)
    d = public(meta, entries)
    if with_entries:
        d["entries"] = entries
    return d


def list_sessions(limit: int = 30) -> list[dict]:
    out = []
    for p in sorted(eye_dir().glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(get(p.stem, with_entries=False))
        except Exception:
            continue
    return out


def history_messages(sid: str, turns: int = 12) -> list[dict]:
    """最近几轮文字对话（图片永不进历史）。主动吐槽也算 assistant 发言。"""
    _, entries = _read(sid)
    msgs = []
    for e in entries:
        if e["kind"] == "user":
            msgs.append({"role": "user", "content": e["text"]})
        elif e["kind"] == "ai" and e["text"].strip("…. ") != "":
            msgs.append({"role": "assistant", "content": e["text"]})
    return msgs[-max(0, turns) * 2:] if turns else []


def recent_context(sid: str, seconds: float = 90, max_chars: int = 600) -> str:
    """最近的台词与场景备注，按时间顺序拼成一段。"""
    _, entries = _read(sid)
    now = time.time()
    lines = [f"[{e['kind'] == 'subtitle' and '台词' or '场景'}] {e['text']}" for e in entries
             if e["kind"] in ("subtitle", "scene") and now - e["t"] <= seconds]
    s = "\n".join(lines)
    return s[-max_chars:]


# ---------------- 主动吐槽限速 ----------------
def auto_allowed(sid: str, pace: str) -> tuple[bool, float]:
    """切场景触发的主动发言：距 AI 上一句「说完」至少 gap 秒。返回 (允许, 还需等待秒数)。"""
    gap = PACE_GAP.get(pace or "normal", 15.0)
    if gap is None:
        return False, 0.0
    _, entries = _read(sid)
    last = next((e for e in reversed(entries) if e["kind"] in ("ai", "user")), None)
    if not last:
        return True, 0.0
    speak = len(last["text"]) / CHARS_PER_SEC if last["kind"] == "ai" else 0
    wait = last["t"] + speak + gap - time.time()
    return wait <= 0, max(0.0, wait)


# ---------------- 预算 ----------------
def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def usage_today() -> dict:
    u = (config.load_state().get("eye_usage") or {}).get(_today()) or {}
    return {"date": _today(), "tokens": int(u.get("tokens", 0)), "requests": int(u.get("requests", 0))}


def check_budget(cfg: dict) -> None:
    e = cfg.get("eye") or {}
    u = usage_today()
    if e.get("daily_max_requests") and u["requests"] >= int(e["daily_max_requests"]):
        raise BudgetError(f"今天的请求次数已用完（{u['requests']}/{e['daily_max_requests']}）")
    if e.get("daily_budget_tokens") and u["tokens"] >= int(e["daily_budget_tokens"]):
        raise BudgetError(f"今天的 token 预算已用完（{u['tokens']}/{e['daily_budget_tokens']}）")


def record_usage(tokens: int) -> dict:
    with _lock:
        st = config.load_state()
        all_u = st.get("eye_usage") or {}
        all_u = {k: all_u[k] for k in sorted(all_u)[-30:]}
        u = all_u.get(_today()) or {"tokens": 0, "requests": 0}
        u["tokens"] = int(u["tokens"]) + max(0, int(tokens or 0))
        u["requests"] = int(u["requests"]) + 1
        all_u[_today()] = u
        config.update_state(eye_usage=all_u)
    return usage_today()


# ---------------- 系统提示：核心记忆 + 相关星星 + 陪看规则 ----------------
def system_prompt(cfg: dict, sid: str, query: str, mode: str) -> str:
    e = cfg.get("eye") or {}
    meta, _ = _read(sid)
    parts = []
    try:
        ct = core.core_text()
        if ct.strip():
            parts.append(ct)
    except Exception:
        pass
    try:
        rec = stars.recall(query or meta.get("title", ""), budget_tokens=int(e.get("recall_budget_tokens", 300)), bump=False)
        if rec.get("items"):
            parts.append(rec.get("text", ""))
    except Exception:
        pass
    src = {"screen": "主人的电脑屏幕", "camera": "摄像头", "tab": "浏览器标签页"}.get(meta.get("source"), "屏幕")
    n = int(e.get("reply_chars", 60))
    rule = (f"【宇宙之眼】你正通过{src}和主人一起看「{meta.get('title', '')}」。附带的图片是按时间顺序截取的最近画面"
            "（分镜图从左到右、从上到下，每格角上标着几秒前，最后一格是现在）。图片只在这一轮可见，之后你只会记得自己说过的话。"
            f"像坐在旁边一起看的朋友那样说话：口语、自然，尽量不超过 {n} 字；只说主人问的或真正有意思的，不要逐格描述；看不清就直说。")
    if mode == "auto":
        rule += "这一轮是画面刚切换时你主动开口的机会：真有意思再说一句；没什么可说就只回复「…」。"
    parts.append(rule)
    return "\n\n".join(p for p in parts if p)


# ---------------- 结束：沉淀成星星 ----------------
def _diary_text(meta: dict, entries: list[dict], max_chars: int = 5000) -> str:
    who = {"user": "主人", "ai": "我", "subtitle": "台词", "scene": "场景", "note": "备注"}
    rows = [f"{who[e['kind']]}：{e['text']}" for e in entries if e["text"].strip("…. ")]
    s = "\n".join(rows)
    return s[-max_chars:]


def end(sid: str, make_star: bool = True, llm_summary: str | None = None) -> dict:
    meta, entries = _read(sid)
    if meta.get("ended_at"):
        return get(sid, with_entries=False)
    now = time.time()
    star = None
    talk = [e for e in entries if e["kind"] in ("user", "ai") and e["text"].strip("…. ")]
    if make_star and talk:
        mins = round((now - meta["started_at"]) / 60)
        day = datetime.fromtimestamp(meta["started_at"]).strftime("%Y-%m-%d")
        if llm_summary:
            content = llm_summary.strip()
        else:
            owner_lines = [e["text"] for e in talk if e["kind"] == "user"][:3]
            content = (f"{day} 和主人通过宇宙之眼一起看了「{meta['title']}」，约 {mins} 分钟，聊了 {len(talk)} 句。"
                       + (f"主人说过：{'；'.join(owner_lines)}" if owner_lines else ""))
        star = stars.add_star(content, summary=f"一起看「{meta['title']}」"[:30], tags=["宇宙之眼", meta["title"][:20]],
                              importance=3, source=f"eye:{sid}", session=sid, actor="eye")
    _patch(sid, ended_at=now, star_id=star and star.get("id"))
    log_event("eye_end", id=sid, title=meta["title"], star_id=star and star.get("id"), entries=len(entries))
    d = get(sid, with_entries=False)
    d["star"] = star
    return d


SUMMARY_PROMPT = """下面是主人和我（AI）通过「宇宙之眼」一起看「{title}」时的文字日记。
请用第一人称「我」写一段 2~4 句的回忆，记下看了什么、主人的反应和喜好、我们聊到的有意思的点。只输出这段话。

{diary}"""


def summary_messages(sid: str) -> list[dict] | None:
    meta, entries = _read(sid)
    diary = _diary_text(meta, entries)
    if not diary.strip():
        return None
    return [{"role": "user", "content": SUMMARY_PROMPT.format(title=meta["title"], diary=diary)}]
