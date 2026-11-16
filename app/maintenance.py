"""LLM 维护（可选）：把新归档的对话整理成星星，合并近似重复，并提出核心记忆「提案」。

没有配置 API Key 时自动跳过，其余功能照常使用。核心记忆永远不会被这里直接修改。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from itertools import islice

from . import archive, config, context, core, stars
from .db import get_conn, log_event

_run_lock = threading.Lock()

PROMPT = """你是「星愿」记忆库的整理员。下面是主人与 AI 的一段原始对话。请提取值得长期记住的信息。
要求：
1. 只提取事实、事件、决定、约定、主人的状态与偏好变化；寒暄和一次性的问答不要。
2. 每条记忆一颗「星」：content 写完整一两句（中文），summary 写不超过 30 字的摘要，tags 1~3 个短主题标签，importance 1~5（5 最重要）。
   同时给 major（恒星系大类）、minor（行星系小类，可空）；优先复用已有类别，类别名 2~6 个字。
3. 如果发现关于「AI身份与名字」「主人是谁及称呼」「主人的喜好」的稳定信息，放进 core_proposals（section 只能是 identity / owner / preferences，mode 用 append），并写明 reason。不要编造。
4. 只输出 JSON，不要任何解释，格式：
{"stars":[{"content":"","summary":"","tags":[""],"major":"","minor":"","importance":3}],"core_proposals":[{"section":"preferences","mode":"append","content":"","reason":""}]}

已有类别：<<CATS>>

当前核心记忆（供参考，已有的不要重复提案）：
<<CORE>>

对话（日期 <<DAY>>）：
<<TEXT>>"""


def model_config(cfg: dict | None = None) -> dict:
    cfg = cfg or config.load_config()
    llm = dict(cfg["llm"])
    if llm.get("maint_base_url"):
        llm.update(base_url=llm["maint_base_url"], api_key=llm.get("maint_api_key", ""),
                   protocol=llm.get("maint_protocol") or llm.get("protocol"))
    llm["model"] = llm.get("maintenance_model") or llm.get("model", "")
    return {**cfg, "llm": llm}


def llm_ready(cfg: dict | None = None) -> bool:
    cfg = cfg or config.load_config()
    target = model_config(cfg)
    # Legacy settings used an empty key to explicitly pause maintenance.
    defaults = cfg.get("defaults") or {}
    return bool((target["llm"]["api_key"] or defaults.get("maintain") or defaults.get("chat")) and
                context.main_ready(target))


def chat(messages: list[dict], cfg: dict | None = None, model: str | None = None, temperature: float = 0.2) -> str:
    cfg = cfg or config.load_config()
    other = not model and cfg["llm"].get("maint_base_url")   # 整理模型在另一个服务商
    from . import upstream
    base = cfg["llm"]["maint_base_url"] if other else cfg["llm"]["base_url"]
    key = cfg["llm"].get("maint_api_key") if other else cfg["llm"]["api_key"]
    proto = (cfg["llm"].get("maint_protocol") or cfg["llm"].get("protocol")) if other else cfg["llm"].get("protocol")
    body = {"model": model or cfg["llm"].get("maintenance_model") or cfg["llm"]["model"],
            "messages": messages, "temperature": temperature, "stream": False}
    data = upstream.complete(proto or "openai", base, key, body, timeout=float(cfg["llm"].get("timeout", 120)))
    return upstream.reply_text(data)


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i:j + 1])
        raise


def _chunks(lines, start: int, max_chars: int = 6000, *, imported_session: str = ""):
    raw, messages, size, scope = [], [], 0, None
    for index, line in enumerate(lines, start):
        try:
            m = json.loads(line)
        except ValueError:
            m = None
        valid = isinstance(m, dict) and isinstance(m.get("content"), str) and bool(m["content"].strip())
        length = len(line)
        key = None
        if valid:
            if m.get("session"):
                key = ("session", m.get("source"), m["session"])
            elif m.get("batch"):
                key = ("batch", m.get("source"), m["batch"])
            elif imported_session:
                key = ("import", imported_session)
            else:
                key = ("unknown", index)
        if raw and (scope != key or size + length > max_chars):
            yield index - len(raw), index, raw, messages, scope
            raw, messages, size = [], [], 0
        scope = key
        raw.append(line)
        size += length
        if valid:
            messages.append(m)
    if raw:
        yield index + 1 - len(raw), index + 1, raw, messages, scope


def pending_work() -> dict:
    legacy = config.load_state().get("consolidated", {})
    with get_conn() as conn:
        cursors = dict(conn.execute("SELECT archive_path, MAX(end_line) FROM archive_chunks GROUP BY archive_path"))
    todo = {}
    for p in archive.iter_jsonl_files():
        rel = p.relative_to(config.SOUL_DIR).as_posix()
        if not archive.jsonl_path(rel):
            continue
        with p.open(encoding="utf-8", errors="replace") as f:
            n = sum(1 for _ in f)
        done = max(int(legacy.get(rel, 0)), cursors.get(rel, 0))
        if n > done:
            todo[rel] = (done, n)
    return todo


def _record_chunk(ref, messages, scope, extracted, proposals) -> tuple[int, int, int] | None:
    added = merged = proposed = 0
    events = []

    def emit(kind, **data):
        events.append((kind, data))

    rel, start, _, _ = ref
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute("SELECT MAX(end_line) FROM archive_chunks WHERE archive_path=?", (rel,)).fetchone()[0]
        if last is not None and last > start:
            return None
        for s in extracted:
            r = stars.add_star(s["content"], s.get("summary", ""), s.get("tags") or [], s.get("importance") or 3,
                               source=f"archive:{rel}", session=messages[0].get("session") or "",
                               created_at=messages[0].get("t") or time.time(), merge_near=True, actor="llm",
                               major=s.get("major"), minor=s.get("minor"), classify=False, conn=conn, emit=emit,
                               archive_ref=ref if scope and scope[0] != "unknown" else None)
            merged += bool(r.get("merged"))
            added += not r.get("merged") and not r.get("duplicate")
        for cp in proposals:
            core.create_proposal(cp.get("section", ""), cp["content"], cp.get("reason", ""), source=f"llm:{rel}",
                                 mode=cp.get("mode", "append"), conn=conn, emit=emit)
            proposed += 1
        conn.execute("INSERT INTO archive_chunks VALUES (?, ?, ?, ?)", ref)
    for kind, data in events:
        log_event(kind, **data)
    return added, merged, proposed


def consolidate(max_chunks: int = 20) -> dict:
    """整理新的归档对话。返回统计结果。"""
    cfg = config.load_config()
    if not llm_ready(cfg):
        return {"ok": False, "skipped": True, "reason": "未配置 LLM API Key，跳过整理"}
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "skipped": True, "reason": "已有整理任务在运行"}
    added = merged = proposals = chunks_done = attempts = 0
    errors: list[str] = []
    try:
        from . import organize
        todo = pending_work()
        core_txt = core.core_text() or "（空）"
        for rel, (done, n) in todo.items():
            if attempts >= max_chunks:
                break
            p = archive.jsonl_path(rel)
            if not p:
                continue
            day = p.stem if re.match(r"\d{4}-\d{2}-\d{2}", p.stem) else datetime.now().strftime("%Y-%m-%d")
            with p.open(encoding="utf-8", errors="replace") as stream:
                imported = rel if p.parent == config.ARCHIVE_DIR.resolve() / "imported" / "chatgpt" else ""
                for start, end, raw, messages, scope in _chunks(islice(stream, done, n), done, imported_session=imported):
                    if attempts >= max_chunks:
                        break
                    try:
                        extracted = proposed = []
                        if messages:
                            attempts += 1
                            text = "\n".join(f"{m.get('role')}: {m['content']}" for m in messages)
                            prompt = (PROMPT.replace("<<CORE>>", core_txt).replace("<<CATS>>", organize.category_text())
                                      .replace("<<DAY>>", day).replace("<<TEXT>>", text))
                            data = parse_json(chat([{"role": "user", "content": prompt}], cfg))
                            extracted, proposed = (_items(data, key) for key in ("stars", "core_proposals"))
                            for s in extracted:
                                major, minor, tags = organize.metadata(s)
                                if not major or not tags:
                                    a, b, extra = organize.classify_new(s["content"], s.get("summary") or "", tags, cfg)
                                    if not major:
                                        major, minor = a, b
                                    tags += extra
                                s.update(major=major, minor=minor, tags=tags)
                        ref = (rel, start, end, archive.range_digest(raw))
                        counts = _record_chunk(ref, messages, scope, extracted, proposed)
                    except Exception as e:
                        errors.append(f"{rel}:{start + 1}-{end}: {e}")
                        break
                    if counts is None:
                        break
                    a, m, p_count = counts
                    added += a
                    merged += m
                    proposals += p_count
                    chunks_done += bool(messages)
        res = {"ok": not errors, "added": added, "merged": merged, "proposals": proposals,
               "chunks": chunks_done, "errors": errors[:5]}
        config.update_state(last_consolidate={"t": time.time(), **res})
        log_event("consolidate", **res)
        return res
    finally:
        _run_lock.release()


def _items(data, key):
    values = data.get(key, []) if isinstance(data, dict) else None
    if values is None and isinstance(data, dict):
        values = []
    if not isinstance(values, list) or any(not isinstance(item, dict) or not isinstance(item.get("content"), str)
                                          for item in values):
        raise ValueError(f"整理结果的 {key} 格式不正确")
    return [item for item in values if item["content"].strip()]


def consolidate_async() -> None:
    threading.Thread(target=consolidate, daemon=True).start()
