"""LLM 维护（可选）：把新归档的对话整理成星星，合并近似重复，并提出核心记忆「提案」。

没有配置 API Key 时自动跳过，其余功能照常使用。核心记忆永远不会被这里直接修改。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime

import httpx

from . import archive, config, core, stars
from .db import log_event

_run_lock = threading.Lock()

PROMPT = """你是「星愿」记忆库的整理员。下面是主人与 AI 的一段原始对话。请提取值得长期记住的信息。
要求：
1. 只提取事实、事件、决定、约定、主人的状态与偏好变化；寒暄和一次性的问答不要。
2. 每条记忆一颗「星」：content 写完整一两句（中文），summary 写不超过 30 字的摘要，tags 1~3 个短标签，importance 1~5（5 最重要）。
3. 如果发现关于「AI身份与名字」「主人是谁及称呼」「主人的喜好」的稳定信息，放进 core_proposals（section 只能是 identity / owner / preferences，mode 用 append），并写明 reason。不要编造。
4. 只输出 JSON，不要任何解释，格式：
{"stars":[{"content":"","summary":"","tags":[""],"importance":3}],"core_proposals":[{"section":"preferences","mode":"append","content":"","reason":""}]}

当前核心记忆（供参考，已有的不要重复提案）：
<<CORE>>

对话（日期 <<DAY>>）：
<<TEXT>>"""


def llm_ready(cfg: dict | None = None) -> bool:
    cfg = cfg or config.load_config()
    return bool(cfg["llm"].get("api_key") and cfg["llm"].get("base_url"))


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


def _chunks(lines: list[dict], max_chars: int = 6000) -> list[list[dict]]:
    out, cur, n = [], [], 0
    for m in lines:
        L = len(m.get("content", ""))
        if cur and n + L > max_chars:
            out.append(cur)
            cur, n = [], 0
        cur.append(m)
        n += L
    if cur:
        out.append(cur)
    return out


def pending_work() -> dict:
    st = config.load_state().get("consolidated", {})
    todo = {}
    for p in archive.iter_jsonl_files():
        rel = p.relative_to(config.SOUL_DIR).as_posix()
        with open(p, "r", encoding="utf-8") as f:
            n = sum(1 for _ in f)
        done = int(st.get(rel, 0))
        if n > done:
            todo[rel] = (done, n)
    return todo


def consolidate(max_chunks: int = 20) -> dict:
    """整理新的归档对话。返回统计结果。"""
    cfg = config.load_config()
    if not llm_ready(cfg):
        return {"ok": False, "skipped": True, "reason": "未配置 LLM API Key，跳过整理"}
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "skipped": True, "reason": "已有整理任务在运行"}
    added = merged = proposals = chunks_done = 0
    errors: list[str] = []
    try:
        todo = pending_work()
        core_txt = core.core_text() or "（空）"
        for rel, (done, n) in todo.items():
            p = config.SOUL_DIR / rel
            with open(p, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            msgs = []
            for ln in all_lines[done:n]:
                try:
                    msgs.append(json.loads(ln))
                except Exception:
                    pass
            day = p.stem if re.match(r"\d{4}-\d{2}-\d{2}", p.stem) else datetime.now().strftime("%Y-%m-%d")
            ok_all = True
            for ch in _chunks(msgs):
                if chunks_done >= max_chunks:
                    ok_all = False
                    break
                text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in ch)
                prompt = PROMPT.replace("<<CORE>>", core_txt).replace("<<DAY>>", day).replace("<<TEXT>>", text)
                try:
                    data = parse_json(chat([{"role": "user", "content": prompt}], cfg))
                except Exception as e:  # 单块失败不影响其他
                    errors.append(f"{rel}: {e}")
                    ok_all = False
                    break
                chunks_done += 1
                created = ch[0].get("t") or time.time()
                for s in data.get("stars", []) or []:
                    if not isinstance(s, dict) or not s.get("content"):
                        continue
                    r = stars.add_star(s["content"], s.get("summary", ""), s.get("tags") or [],
                                       int(s.get("importance") or 3), source=f"archive:{rel}",
                                       session=ch[0].get("session", ""), created_at=created,
                                       merge_near=True, actor="llm")
                    if r.get("merged"):
                        merged += 1
                    elif not r.get("duplicate"):
                        added += 1
                for cp in data.get("core_proposals", []) or []:
                    try:
                        core.create_proposal(cp.get("section", ""), cp.get("content", ""), cp.get("reason", ""),
                                             source=f"llm:{rel}", mode=cp.get("mode", "append"))
                        proposals += 1
                    except Exception:
                        pass
            if ok_all:
                st = config.load_state()
                cons = st.get("consolidated", {})
                cons[rel] = n
                config.update_state(consolidated=cons)
        res = {"ok": not errors, "added": added, "merged": merged, "proposals": proposals,
               "chunks": chunks_done, "errors": errors[:5]}
        config.update_state(last_consolidate={"t": time.time(), **res})
        log_event("consolidate", **res)
        return res
    finally:
        _run_lock.release()


def consolidate_async() -> None:
    threading.Thread(target=consolidate, daemon=True).start()
