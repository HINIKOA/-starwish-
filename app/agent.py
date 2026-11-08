"""对话抽屉（v0.11.0）：主人用自然语言指挥 AI 管理星愿。

- 用「默认对话模型」（主模型）+ 工具调用；面板是主人通道：改核心记忆直接生效（上锁时转提案）。
- 有上下文：历史存 soul/agent/chat.json，按上下文窗口（默认 400,000 tokens）取用；
  累计到阈值（默认 380,000）自动压缩旧历史（摘要 + 最近几轮），任务抽屉里能看到。
- 没配主模型时：支持 /状态 /归纳 /整理 /搜索 关键词 /清空 几个离线指令。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import config, context, core, jobs, maintenance, stars

SYSTEM = """你是「星愿记忆」的管家 AI，主人在面板里的对话抽屉指挥你管理记忆库。
- 用提供的工具完成操作，不要假装做了；做完用一两句中文说明结果（带上星的编号）。
- 改核心记忆前确认主人的意思明确；删除星之前如果不确定是哪一颗，先搜索并列出候选。
- 星图像银河系：核心记忆是星系核，大类是恒星系，小类是行星系，每条记忆是一颗星球。
当前核心记忆：
<<CORE>>"""

TOOLS_SPEC = [
    ("search_memory", "按关键词或意思搜索记忆星", {"query": ("string", "搜索内容"), "limit": ("integer", "最多几条，默认 8")}, ["query"]),
    ("add_star", "新增一颗记忆星", {"content": ("string", "内容"), "tags": ("array", "标签"), "importance": ("integer", "1~5"),
                                   "major": ("string", "大类（恒星系），可空"), "minor": ("string", "小类（行星系），可空")}, ["content"]),
    ("edit_star", "修改一颗记忆星", {"id": ("integer", "星编号"), "content": ("string", "新内容"), "tags": ("array", "新标签"),
                                    "importance": ("integer", "1~5"), "major": ("string", "大类"), "minor": ("string", "小类")}, ["id"]),
    ("delete_star", "隐藏（软删除）一颗记忆星，可恢复", {"id": ("integer", "星编号")}, ["id"]),
    ("get_core", "读取核心记忆三段", {}, []),
    ("update_core", "修改核心记忆（主人通道，未上锁直接生效）", {"section": ("string", "identity / owner / preferences"),
                                                         "content": ("string", "内容"), "mode": ("string", "replace 或 append")}, ["section", "content"]),
    ("propose_core", "给核心记忆提一个提案（等主人批准）", {"section": ("string", "identity / owner / preferences"),
                                                   "content": ("string", "内容"), "reason": ("string", "理由")}, ["section", "content"]),
    ("list_proposals", "列出待批准的核心提案", {}, []),
    ("list_skills", "列出技能", {}, []),
    ("view_skill", "查看一个技能的内容", {"name": ("string", "技能名")}, ["name"]),
    ("run_digest", "立即归纳各客户端的新对话（对话监控）", {}, []),
    ("organize_stars", "让主模型整理星系：融合相同的星、给星分大类 / 小类", {}, []),
    ("set_watch", "打开或关闭某个客户端的对话监控", {"client": ("string", "claude / qoder / workbuddy / codebuddy / codex / opencode / dsh"),
                                                 "enabled": ("boolean", "是否监控")}, ["client", "enabled"]),
    ("get_status", "读取星愿的状态（星数、监控、任务、整理）", {}, []),
]


def tools() -> list[dict]:
    out = []
    for name, desc, props, req in TOOLS_SPEC:
        p = {}
        for k, (t, d) in props.items():
            p[k] = {"type": t, "description": d}
            if t == "array":
                p[k]["items"] = {"type": "string"}
        out.append({"type": "function", "function": {"name": name, "description": desc,
                                                       "parameters": {"type": "object", "properties": p, "required": req}}})
    return out


# ---------------- 历史 ----------------
_hl = threading.Lock()


def history_path():
    d = config.soul_path("agent")
    d.mkdir(parents=True, exist_ok=True)
    return d / "chat.json"


def load_history() -> list[dict]:
    p = history_path()
    try:
        h = json.loads(p.read_text("utf-8")) if p.exists() else []
        return [m for m in h if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")]
    except Exception:
        return []


def save_history(h: list[dict]) -> None:
    with _hl:
        p = history_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(h, ensure_ascii=False), "utf-8")
        os.replace(tmp, p)


def clear_history() -> None:
    save_history([])


# ---------------- 工具执行 ----------------
def _brief(s: dict) -> dict:
    return {"id": s.get("id"), "summary": s.get("summary"), "content": (s.get("content") or "")[:300], "tags": s.get("tags"),
            "major": s.get("cat_major", ""), "minor": s.get("cat_minor", "")}


def run_tool(name: str, a: dict) -> dict:
    from . import organize
    from .evolve import skills
    from .watch import store, watcher
    a = a or {}
    if name == "search_memory":
        res = stars.search(str(a.get("query") or ""))[: max(1, min(20, int(a.get("limit") or 8)))]
        return {"results": [{"id": r["id"], "summary": r.get("summary"), "tags": r.get("tags"), "relevance": r.get("relevance")} for r in res]}
    if name == "add_star":
        s = stars.add_star(str(a.get("content") or ""), "", a.get("tags") or [], int(a.get("importance") or 3),
                           source="agent", actor="owner")
        if a.get("major"):
            organize.set_category(s["id"], a["major"], a.get("minor") or "")
        return {"ok": True, "star": _brief(stars.get_star(s["id"]))}
    if name == "edit_star":
        sid = int(a["id"])
        fields = {k: a[k] for k in ("content", "tags", "importance") if a.get(k) not in (None, "")}
        if fields:
            stars.update_star(sid, **fields)
        if a.get("major") is not None and a.get("major") != "":
            organize.set_category(sid, a["major"], a.get("minor") or "")
        return {"ok": True, "star": _brief(stars.get_star(sid))}
    if name == "delete_star":
        stars.soft_delete(int(a["id"]))
        return {"ok": True, "hidden": int(a["id"])}
    if name == "get_core":
        c = core.load_core()
        return {"locked": c.get("locked"), "sections": core.sections_of(c)}
    if name == "update_core":
        mode = a.get("mode") if a.get("mode") in ("replace", "append") else "replace"
        r = core.owner_update({core.norm_section(a["section"]): (str(a["content"]), mode)}, source="panel",
                              authorized=True, instruction="对话抽屉", note="对话抽屉")
        return {"applied": r.get("applied"), "proposed": r.get("proposed"), "reason": r.get("reason", "")}
    if name == "propose_core":
        p = core.create_proposal(a["section"], str(a["content"]), reason=str(a.get("reason") or "对话抽屉"), source="agent")
        return {"ok": True, "proposal": p.get("id")}
    if name == "list_proposals":
        return {"proposals": [{"id": p["id"], "section": p["section"], "content": p["content"][:200]} for p in core.list_proposals("pending")]}
    if name == "list_skills":
        return {"skills": [{"name": s["name"], "description": s.get("description", "")} for s in skills.list_skills()][:60]}
    if name == "view_skill":
        v = skills.view(str(a["name"]))
        return {"name": a["name"], "content": str(v.get("content", ""))[:4000]}
    if name == "run_digest":
        threading.Thread(target=watcher.get().process_now, daemon=True).start()
        return {"ok": True, "started": True, "note": "已在后台归纳，任务抽屉里能看到进度"}
    if name == "organize_stars":
        threading.Thread(target=organize.run_job, args=("整理星系（对话抽屉）",), daemon=True).start()
        return {"ok": True, "started": True}
    if name == "set_watch":
        return set_watch(str(a["client"]), bool(a.get("enabled")))
    if name == "get_status":
        st = stars.stats()
        return {"stars": st["total"], "today": st["today"], "pending_proposals": st["pending_proposals"],
                "watch": watcher.get().public(), "client_tasks": store.counts(), "maintenance_model": maintenance.llm_ready(),
                "last_organize": config.load_state().get("last_organize"), "categories": organize.categories()[:40]}
    raise ValueError(f"没有这个工具：{name}")


def set_watch(client: str, enabled: bool) -> dict:
    from .watch.adapters import CLIENTS
    if client not in CLIENTS:
        raise ValueError(f"不认识的客户端：{client}")
    config.update_config({"watch": {"clients": {client: {"enabled": bool(enabled)}}}})
    return {"ok": True, "client": client, "enabled": bool(enabled)}


# ---------------- 对话 ----------------
HELP = "离线指令：/状态　/归纳　/整理　/搜索 关键词　/清空（清空对话历史）。配好「默认模型 → 对话模型」后可以直接用自然语言。"


def offline(text: str) -> dict | None:
    t = text.strip()
    if t in ("/清空", "/clear"):
        clear_history()
        return {"ok": True, "reply": "对话历史已清空。", "actions": []}
    if t in ("/状态", "/status"):
        r = run_tool("get_status", {})
        return {"ok": True, "reply": f"星星 {r['stars']} 颗（今天 +{r['today']}），待批准提案 {r['pending_proposals']}，"
                f"监控已处理 {r['watch'].get('turns', 0)} 轮；整理模型{'已配置' if r['maintenance_model'] else '未配置'}。",
                "actions": [{"tool": "get_status"}]}
    if t in ("/归纳", "/digest"):
        run_tool("run_digest", {})
        return {"ok": True, "reply": "已开始归纳各客户端的新对话。", "actions": [{"tool": "run_digest"}]}
    if t in ("/整理", "/organize"):
        run_tool("organize_stars", {})
        return {"ok": True, "reply": "已开始整理星系。", "actions": [{"tool": "organize_stars"}]}
    if t.startswith("/搜索") or t.startswith("/search"):
        q = t.split(None, 1)[1] if len(t.split(None, 1)) > 1 else ""
        r = run_tool("search_memory", {"query": q})["results"]
        lines = [f"#{x['id']} {x['summary']}" for x in r] or ["没有找到。"]
        return {"ok": True, "reply": "\n".join(lines), "actions": [{"tool": "search_memory"}]}
    return None


def chat(text: str, cfg: dict | None = None, max_rounds: int = 6) -> dict:
    cfg = cfg or config.load_config()
    text = (text or "").strip()
    if not text:
        raise ValueError("说点什么吧")
    hist = load_history()
    off = offline(text)
    ready = context.main_ready(cfg)
    if off is not None or not ready:
        out = off or {"ok": False, "reply": "还没有配置默认对话模型（设置 → 默认模型）。" + HELP, "actions": []}
        if text not in ("/清空", "/clear"):
            save_history(load_history() + [{"role": "user", "content": text, "t": time.time()},
                                           {"role": "assistant", "content": out["reply"], "t": time.time()}])
        return out
    with jobs.run("agent", "对话指令：" + text[:40]) as j:
        sys_msg = {"role": "system", "content": SYSTEM.replace("<<CORE>>", core.core_text() or "（空）")}
        user = {"role": "user", "content": text}
        hist, info = context.compact_if_needed(hist, cfg, "chat", "对话抽屉",
                                               extra_tokens=context.msg_tokens(sys_msg) + context.msg_tokens(user))
        use = context.trim_history([{"role": m["role"], "content": m["content"]} for m in hist],
                                   context.budget(cfg, "chat") - context.msg_tokens(sys_msg))
        msgs = [sys_msg] + use + [user]
        actions = []
        reply = ""
        for _ in range(max_rounds):
            data = context.main_complete(msgs, cfg, tools=tools(), temperature=0.3)
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = msg.get("content") or "（没有回复）"
                break
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for tc in calls:
                f = tc.get("function") or {}
                nm = f.get("name", "")
                try:
                    args = json.loads(f.get("arguments") or "{}") if isinstance(f.get("arguments"), str) else (f.get("arguments") or {})
                    result = run_tool(nm, args)
                    ok = True
                except Exception as e:  # noqa: BLE001
                    result, ok = {"error": f"{type(e).__name__}: {e}"}, False
                actions.append({"tool": nm, "args": args if ok else f.get("arguments"), "ok": ok,
                                "result": json.dumps(result, ensure_ascii=False)[:300]})
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": json.dumps(result, ensure_ascii=False)[:6000]})
        else:
            reply = "工具调用次数到上限了，先停在这里。" + (f"（已执行 {len(actions)} 个操作）" if actions else "")
        note = ("\n（已执行：" + "、".join(a["tool"] for a in actions) + "）") if actions else ""
        save_history(hist + [{"role": "user", "content": text, "t": time.time()},
                             {"role": "assistant", "content": reply + note, "t": time.time()}])
        j["detail"] = (reply[:200] + note)[:500]
        return {"ok": True, "reply": reply, "actions": actions, "compacted": info}
