"""自进化引擎（参考 Hermes Agent 的「前台自觉 + 后台巡检」双引擎）。

引擎一 · 前台自觉：会用工具的客户端（Claude / Cursor / Hermes 等 MCP 客户端）从 MCP 指令里拿到 SKILLS_GUIDANCE，
        复杂任务做完、踩坑修好、发现非显而易见的流程时自己调用 skill_manage 保存；发现技能过时立即 patch。
        调用 skill_manage 会把计数器归零。
引擎二 · 后台巡检：代理每转发一轮对话计数器 +1（跨会话、跨任务累积，不重置）；达到阈值（默认 10）且没有前台保存时，
        在后台线程里让维护模型回看最近的对话，按「试错、绕路、主人纠正」三条线索决定新建 / 修补技能；
        同时（联合巡检）顺手提取值得记住的星星、提出核心记忆提案（只提案，永远不直接改核心）。
自修复：本轮注入过的技能、被差评的技能会连同全文一起交给巡检，优先 patch 修好。
"""
from __future__ import annotations

import json
import threading
import time

from .. import config
from ..db import estimate_tokens, log_event
from . import skills

SKILLS_GUIDANCE = (
    "【星愿·技能自进化】完成复杂任务（5 步以上）、修好棘手错误、或发现不显而易见的流程后，用 skill_manage(action='create') "
    "把做法存成技能，下次直接复用。使用某个技能时发现它过时、不完整或有错，立即用 skill_manage(action='patch') 修补，"
    "不要等主人提醒——不维护的技能会变成负担。技能写「教训」而不是「日志」：祈使句规则 + 原因，一条一条。"
)

REVIEW_PROMPT = """你是「星愿」记忆库的技能巡检员。下面是主人与 AI 最近的对话。请判断是否需要保存或更新「技能」（AI 的操作手册 / 程序性记忆）。

重点关注：
1. 是否用了一个不简单的方法完成任务，过程中有试错、绕路，或因为中途发现的情况改变了做法？
2. 主人是否期望或纠正成另一种做法 / 结果？
3. 本轮用到的技能（下方附全文）是否过时、不完整或有错？有的话用 patch 修好（old_string 摘自原文，new_string 是改后内容）。
4. 已有相关技能就更新它（patch），没有且做法可复用才新建（create）。顺利且一次性的任务不要存。不要编造对话里没有的内容。
<<MEMORY_RULE>>
技能格式（create 的 content 必须是完整 SKILL.md）：
---
name: 小写英文-连字符
description: 一句话：什么时候用 + 做什么（≤120 字）
tags: [标签]
---
# 标题
## 适用场景
## 步骤
1. ……
## 注意 / 踩过的坑
- 规则 + 原因

只输出 JSON，不要解释：
{"skills":[{"action":"create|patch","name":"","category":"","content":"","old_string":"","new_string":"","reason":""}],
 <<MEMORY_FIELDS>>"summary":"一句话说明做了什么；没有就写 Nothing to save."}

已有技能（名字：说明）：
<<INDEX>>

本轮用到 / 被主人差评、需要优先检查的技能全文：
<<USED>>

最近的对话：
<<TEXT>>"""

MEMORY_RULE = ("5. 联合巡检：顺便把值得长期记住的事实放进 stars（content 一两句、summary ≤30 字、tags、importance 1~5）；"
               "关于 AI身份 / 主人称呼 / 主人喜好 的稳定信息放进 core_proposals（只是提案，等主人批准）。\n")
MEMORY_FIELDS = ('"stars":[{"content":"","summary":"","tags":[""],"importance":3}],'
                 '"core_proposals":[{"section":"preferences","mode":"append","content":"","reason":""}],')

_lock = threading.Lock()
_buf_lock = threading.Lock()
_running = threading.Event()


def _cfg() -> dict:
    return config.load_config().get("evolve", {})


def _buf_path():
    return config.SOUL_DIR / "evolve_buffer.json"


def _load_buf() -> list[dict]:
    return skills._read_json(_buf_path(), [])


def _save_buf(b: list[dict]) -> None:
    skills.atomic_write(_buf_path(), json.dumps(b, ensure_ascii=False))


# ---------------- 计数器 ----------------

def status() -> dict:
    st = config.load_state()
    c = _cfg()
    return {"enabled": bool(c.get("enabled", True)), "iters_since_skill": int(st.get("evolve_iters", 0)),
            "nudge_interval": int(c.get("nudge_interval", 10)), "running": _running.is_set(),
            "buffer_turns": len(_load_buf()), "needs_fix": st.get("evolve_needs_fix", {}),
            "last_review": st.get("evolve_last_review"), "approval": c.get("approval", "auto"), **skills.stats()}


def note_skill_manage() -> None:
    """前台调用了 skill_manage（AI 自觉保存/修补）→ 计数器归零（对应 Hermes 的 _iters_since_skill = 0）。"""
    config.update_state(evolve_iters=0)


def mark_needs_fix(name: str, comment: str = "") -> None:
    st = config.load_state()
    nf = dict(st.get("evolve_needs_fix") or {})
    nf[name] = comment or "主人差评"
    config.update_state(evolve_needs_fix=nf)


def on_turn(user: str, reply: str, used_skills: list[str] | None = None, session: str = "", source: str = "proxy") -> bool:
    """代理每转发完一轮调用一次。返回是否触发了后台巡检。"""
    c = _cfg()
    if not c.get("enabled", True):
        return False
    with _buf_lock:
        b = _load_buf()
        b.append({"t": time.time(), "session": session, "user": (user or "")[:4000], "reply": (reply or "")[:4000],
                  "skills": used_skills or [], "source": source})
        b = b[-int(c.get("buffer_turns", 40)):]
        _save_buf(b)
    n = int(config.load_state().get("evolve_iters", 0)) + 1
    interval = int(c.get("nudge_interval", 10))
    if interval > 0 and n >= interval:
        from ..maintenance import llm_ready
        if llm_ready() and not _running.is_set():
            config.update_state(evolve_iters=0)
            spawn_review(reason=f"后台巡检：距上次保存技能已 {n} 轮")
            return True
    config.update_state(evolve_iters=n)
    return False


def spawn_review(reason: str = "", focus: str = "") -> bool:
    """在守护线程里跑一次巡检（对应 Hermes 的 _spawn_background_review）。"""
    if _running.is_set():
        return False
    _running.set()

    def run():
        try:
            review(reason=reason, focus=focus)
        except Exception as e:  # pragma: no cover
            log_event("evolve_review_error", error=str(e))
        finally:
            _running.clear()

    threading.Thread(target=run, daemon=True, name="starwish-review").start()
    return True


def wait_idle(timeout: float = 60) -> bool:
    t0 = time.time()
    while _running.is_set() and time.time() - t0 < timeout:
        time.sleep(0.1)
    return not _running.is_set()


# ---------------- 巡检本体 ----------------

def _conv_text(b: list[dict], max_chars: int = 9000) -> str:
    parts, n = [], 0
    for x in reversed(b):
        seg = f"主人：{x['user']}\nAI：{x['reply']}" + (f"\n（本轮注入技能：{', '.join(x['skills'])}）" if x.get("skills") else "")
        if n + len(seg) > max_chars and parts:
            break
        parts.append(seg)
        n += len(seg)
    return "\n\n".join(reversed(parts))


def review(reason: str = "", focus: str = "", buffer: list[dict] | None = None, wait: bool = False) -> dict:
    from .. import core, maintenance, stars
    c = _cfg()
    if not maintenance.llm_ready():
        return {"ok": False, "skipped": True, "reason": "未配置 LLM API Key，无法巡检"}
    if not (_lock.acquire(timeout=120) if wait else _lock.acquire(blocking=False)):
        return {"ok": False, "skipped": True, "reason": "已有巡检在运行"}
    try:
        with _buf_lock:
            b = list(buffer if buffer is not None else _load_buf())
        st = config.load_state()
        needs_fix = dict(st.get("evolve_needs_fix") or {})
        if not b and not needs_fix:
            return {"ok": True, "skipped": True, "reason": "最近没有新对话"}
        used = []
        for x in b:
            for s in x.get("skills", []):
                if s not in used:
                    used.append(s)
        for s in needs_fix:
            if s not in used:
                used.insert(0, s)
        used_txt = []
        for name in used[:4]:
            try:
                v = skills.view(name, bump_view=False)
                note = f"（主人差评：{needs_fix[name]}）" if name in needs_fix else ""
                used_txt.append(f"=== {name} {note}===\n{v['content'][:4000]}")
            except KeyError:
                pass
        index = "\n".join(f"- {s['name']}：{s['description']}" for s in skills.list_skills()[:80]) or "（还没有技能）"
        mem = bool(c.get("review_memory", True))
        prompt = (REVIEW_PROMPT.replace("<<MEMORY_RULE>>", MEMORY_RULE if mem else "")
                  .replace("<<MEMORY_FIELDS>>", MEMORY_FIELDS if mem else "")
                  .replace("<<INDEX>>", index).replace("<<USED>>", "\n\n".join(used_txt) or "（无）")
                  .replace("<<TEXT>>", _conv_text(b) or "（无新对话，只检查上面被差评的技能）"))
        if focus:
            prompt += f"\n\n主人的特别要求：{focus}"
        raw = maintenance.chat([{"role": "user", "content": prompt}])
        data = maintenance.parse_json(raw)
        results, applied, staged = [], 0, 0
        for op in (data.get("skills") or [])[: int(c.get("max_ops", 5))]:
            act = (op.get("action") or "").strip()
            if act not in ("create", "patch", "write_file"):
                results.append({"op": act, "name": op.get("name"), "success": False, "message": "巡检只允许 create / patch / write_file"})
                continue
            kw = {k: op.get(k) for k in ("content", "category", "old_string", "new_string", "file_path", "file_content")
                  if op.get(k) not in (None, "")}
            if act == "patch" and "new_string" not in kw and op.get("new_string") == "":
                kw["new_string"] = ""
            r = skills.manage(act, op.get("name", ""), source="review", trust="agent-created",
                              note=op.get("reason") or reason, **kw)
            results.append({"op": act, "name": op.get("name"), **{k: r.get(k) for k in ("success", "message", "staged", "pending_id")}})
            if r.get("success") and not r.get("staged"):
                applied += 1
                needs_fix.pop(op.get("name"), None)
            elif r.get("staged"):
                staged += 1
        added = props = 0
        if mem:
            for s in data.get("stars") or []:
                if (s.get("content") or "").strip():
                    stars.add_star(s["content"], s.get("summary", ""), s.get("tags") or [], int(s.get("importance") or 3),
                                   source="review", merge_near=True, actor="review")
                    added += 1
            for cp in data.get("core_proposals") or []:
                try:
                    core.create_proposal(cp.get("section", ""), cp.get("content", ""), cp.get("reason", ""),
                                         source="review", mode=cp.get("mode", "append"))
                    props += 1
                except Exception:
                    pass
        with _buf_lock:  # 已巡检过的对话移出缓冲（巡检期间新来的保留）
            cur = _load_buf()
            seen = {x["t"] for x in b}
            _save_buf([x for x in cur if x["t"] not in seen])
        res = {"ok": True, "t": time.time(), "reason": reason, "skills": results, "applied": applied, "staged": staged,
               "stars_added": added, "core_proposals": props, "summary": data.get("summary", ""), "turns": len(b)}
        config.update_state(evolve_last_review=res, evolve_needs_fix=needs_fix)
        log_event("evolve_review", **{k: v for k, v in res.items() if k != "t"})
        return res
    except Exception as e:
        res = {"ok": False, "error": str(e), "t": time.time(), "reason": reason}
        config.update_state(evolve_last_review=res)
        log_event("evolve_review_error", error=str(e))
        return res
    finally:
        _lock.release()


# ---------------- 注入（渐进式披露） ----------------

def injection(query: str) -> tuple[str, list[str]]:
    """给不会调工具的客户端：只注入相关技能的「名字+说明」索引；最相关的一个且够相关、够短时附上正文。"""
    c = _cfg()
    if not c.get("inject", True) or not query.strip():
        return "", []
    hits = skills.relevant(query, k=int(c.get("inject_k", 3)))
    if not hits:
        return "", []
    budget = int(c.get("inject_budget_tokens", 400))
    lines = ["【星愿·相关技能】"]
    used = []
    top_score, top = hits[0]
    if top_score >= float(c.get("inject_full_threshold", 0.3)):
        body = skills.view(top["name"], bump_view=False)["body"].strip()
        if estimate_tokens(body) <= budget * 0.8:
            lines.append(f"▸{top['name']}（{top['description']}）\n{body}")
            used.append(top["name"])
    for _, s in hits:
        if s["name"] in used:
            continue
        ln = f"▸{s['name']}：{s['description']}"
        if estimate_tokens("\n".join(lines + [ln])) > budget:
            break
        lines.append(ln)
        used.append(s["name"])
    if not used:
        return "", []
    for n in used:
        skills.bump(n, "injections")
    lines.append("（按技能做；发现技能不对请照实际情况处理，星愿会在后台修补它。）")
    return "\n".join(lines), used
