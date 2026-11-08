"""抽取：一轮对话 → 星星 / 核心提案 / 任务碎片。监控 AI 没有上下文，只看这一轮 + 该会话的未闭环任务摘要。

规则（主人定的）：
  · 自动通道绝不直接改核心记忆：重要偏好（importance ≥ core_importance）→ 核心提案；
  · 普通偏好 / 事实 → 普通星星（来源 watch:<客户端>）；
  · 任务 → 按 客户端+会话+任务 存碎片，带「未闭环」标签；闭环时合并成一个完整、可复用的任务
    （适合复用就存成技能，否则存成带「任务」标签的星），失败标「失败」。日常偏好不算任务。
"""
from __future__ import annotations

import json
import re
import zlib

from .. import config, core, maintenance, stars
from ..db import log_event
from . import store
from .turns import Turn, clip

PROMPT = """你是「星愿」记忆库的对话监控员。你看不到之前的对话，只看到下面这一轮（主人说的话 + AI 的回复 + 用到的工具）。判断其中有没有值得长期记住的东西。
规则：
- preference：主人明确表达的习惯、喜好、要求（如「以后都用中文」「我习惯用 pnpm」）。importance 1~5：长期、全局、关于身份称呼的给 4~5，只针对某件事的给 2~3。
- fact：关于主人、项目、环境的稳定事实（如「团队周五下午三点开周会」「项目用 Python 3.12」）。一次性问答、代码细节、AI 自己的推理都不要。
- task：主人交给 AI、可能要多轮才能做完的工作。task_key 用简短小写英文 / 拼音加短横线；如果下面「未闭环任务」里有同一件事，必须沿用它的 key。
  task_status：这一轮还没做完 open；这一轮明确做完了 closed；明确失败或放弃 failed。text 写这一轮在这件事上做了什么、做到哪一步（一两句）。日常偏好不算任务。
- category：短类别，如 编程 / 写作 / 工作 / 生活 / 工具，或项目名。
- 没有值得记的就输出 {"items":[]}。不要编造。
只输出 JSON，格式：{"items":[{"kind":"preference|fact|task","text":"","category":"","importance":3,"task_key":"","task_status":"open"}]}

该会话的未闭环任务：
<<OPEN>>

这一轮（客户端 <<CLIENT>>，目录 <<CWD>>，时间 <<TS>>）：
【主人】<<USER>>
【AI】<<AI>>
【工具】<<TOOLS>>"""

MERGE = """下面是同一个任务分几轮完成的记录碎片（按时间顺序）。把它们合并成一条完整、以后可以照着复用的任务记录。
只输出 JSON：{"title":"不超过 20 字","summary":"完整描述：目标、关键步骤、结果、注意事项","reusable":false,
"skill_name":"小写英文-短横线","skill_description":"一句话：什么时候用 + 做什么","skill_body":"Markdown：适用场景、步骤、注意事项"}
reusable 只在这是一套可以再次照做的流程 / 方法时为 true（一次性的修改、问答为 false）。

任务类别：<<CAT>>（客户端 <<CLIENT>>，目录 <<CWD>>）
碎片：
<<FRAGS>>"""

SCHEMA_KINDS = ("preference", "fact", "task", "none")


def llm(messages: list[dict], cfg: dict) -> str:
    """调用整理模型（按服务商协议：OpenAI 兼容 / Anthropic / Responses）。测试里会被替换。"""
    return maintenance.chat(messages, cfg, temperature=0.1)


def wcfg(cfg: dict | None = None) -> dict:
    cfg = cfg or config.load_config()
    return cfg.get("watch") or {}


def build_prompt(t: Turn, open_tasks: list[dict], cfg: dict) -> str:
    w = wcfg(cfg)
    lim = int(w.get("max_turn_chars") or 4000)
    open_txt = "\n".join(f"- key={o['task_key']}｜{o.get('category') or ''}｜{o.get('title') or ''}" for o in open_tasks) or "（无）"
    return (PROMPT.replace("<<OPEN>>", open_txt).replace("<<CLIENT>>", t.client).replace("<<CWD>>", t.cwd or "?")
            .replace("<<TS>>", t.ts or "?").replace("<<USER>>", clip(t.user_text, int(lim * 0.45)))
            .replace("<<AI>>", clip(t.assistant_text, int(lim * 0.45))).replace("<<TOOLS>>", clip(t.tool_summary, int(lim * 0.1)) or "（无）"))


def normalize(data) -> list[dict]:
    """严格校验模型输出；不合规的条目丢掉。"""
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "none").strip().lower()
        text = str(it.get("text") or "").strip()
        if kind not in SCHEMA_KINDS or kind == "none" or not text:
            continue
        try:
            imp = max(1, min(5, int(it.get("importance") or 3)))
        except (TypeError, ValueError):
            imp = 3
        st = str(it.get("task_status") or "open").strip().lower()
        if st not in store.STATUS:
            st = "open"
        key = re.sub(r"[^0-9a-z\u4e00-\u9fff_-]+", "-", str(it.get("task_key") or "").strip().lower()).strip("-")[:48]
        out.append({"kind": kind, "text": text[:600], "category": str(it.get("category") or "").strip()[:20],
                    "importance": imp, "task_key": key or ("task-" + str(zlib.crc32(text.encode("utf-8")) % 100000)), "task_status": st})
    return out


def process_turn(t: Turn, cfg: dict | None = None) -> dict:
    """抽取并落库。返回统计。出错抛异常（调用方负责重试 / 计数）。"""
    cfg = cfg or config.load_config()
    w = wcfg(cfg)
    open_t = store.open_tasks(t.client, t.session_id)
    raw = llm([{"role": "user", "content": build_prompt(t, open_t, cfg)}], cfg)
    items = normalize(maintenance.parse_json(raw))
    res = {"stars": 0, "proposals": 0, "fragments": 0, "closed": 0, "failed": 0, "skills": 0, "items": len(items)}
    src = f"watch:{t.client}"
    core_imp = int(w.get("core_importance") or 4)
    for it in items:
        cat = it["category"]
        if it["kind"] == "preference" and it["importance"] >= core_imp:
            try:
                core.create_proposal("preferences", it["text"], reason=f"对话监控：主人在 {t.client} 里表达的重要偏好", source=src)
                res["proposals"] += 1
            except Exception:
                pass
        elif it["kind"] in ("preference", "fact"):
            tags = (["偏好"] if it["kind"] == "preference" else []) + ([cat] if cat else [])
            r = stars.add_star(it["text"], "", tags or ["对话"], it["importance"], source=src, session=t.session_id,
                               merge_near=True, actor="watch")
            if not r.get("duplicate"):
                res["stars"] += 1
        elif it["kind"] == "task":
            task = store.upsert_fragment(t.client, t.session_id, it["task_key"], it["text"], cat, it["task_status"],
                                         cwd=t.cwd, turn_ts=t.ts)
            res["fragments"] += 1
            _sync_open_star(task, t)
            if it["task_status"] == "closed":
                close_task(task["id"], cfg)
                res["closed"] += 1
            elif it["task_status"] == "failed":
                fail_task(task["id"])
                res["failed"] += 1
    log_event("watch_turn", client=t.client, session=t.session_id, **{k: v for k, v in res.items() if v})
    try:   # 技能自进化：监控到的每一轮也计入巡检计数（不经代理时也能自发进化）
        from .. import evolve
        res["review"] = bool(evolve.review.on_turn(t.user_text, t.assistant_text, [], session=t.session_id, source=src))
    except Exception as e:  # noqa: BLE001
        log_event("evolve_watch_error", error=str(e))
    return res


def _frag_text(task: dict) -> str:
    return "\n".join(f"{i + 1}. {f['text']}" for i, f in enumerate(task.get("fragments") or []))


def _sync_open_star(task: dict, t: Turn | None = None) -> None:
    """未闭环任务对应一颗带「未闭环」标签的星，方便以后召回「上次做到哪」。"""
    content = f"【未闭环任务】{task.get('title') or task['task_key']}\n" + _frag_text(task)
    tags = ["未闭环"] + ([task["category"]] if task.get("category") else [])
    try:
        if task.get("star_id") and stars.get_star(task["star_id"], include_deleted=False):
            stars.update_star(task["star_id"], content=content, tags=tags)
        else:
            r = stars.add_star(content, "", tags, 3, source=f"watch:{task['client']}", session=task["session_id"], actor="watch")
            store.update_task(task["id"], star_id=r["id"])
    except Exception as e:  # noqa: BLE001
        log_event("watch_error", error=f"任务星：{e}"[:200])


def close_task(tid: int, cfg: dict | None = None, by: str = "watch") -> dict:
    """闭环：碎片合并成一个完整任务；适合复用 → 技能，否则 → 带「任务」标签的星。"""
    cfg = cfg or config.load_config()
    task = store.get_task(tid)
    if not task:
        raise KeyError("任务不存在")
    frags = _frag_text(task)
    merged = None
    if maintenance.llm_ready(cfg):
        try:
            prompt = (MERGE.replace("<<CAT>>", task.get("category") or "未分类").replace("<<CLIENT>>", task["client"])
                      .replace("<<CWD>>", task.get("cwd") or "?").replace("<<FRAGS>>", frags))
            merged = maintenance.parse_json(llm([{"role": "user", "content": prompt}], cfg))
            if not isinstance(merged, dict) or not str(merged.get("summary") or "").strip():
                merged = None
        except Exception as e:  # noqa: BLE001  —— 归纳失败就朴素拼接
            log_event("watch_error", error=f"任务合并：{e}"[:200])
            merged = None
    title = str((merged or {}).get("title") or task.get("title") or task["task_key"])[:60]
    summary = str((merged or {}).get("summary") or "").strip() or f"{title}\n" + frags
    skill_name = ""
    if merged and merged.get("reusable") and (wcfg(cfg).get("skill_on_close", True)):
        skill_name = _save_skill(task, merged, title, summary)
    content = f"【任务】{title}\n{summary}" + (f"\n（已存为技能 {skill_name}）" if skill_name else "")
    tags = ["任务"] + ([task["category"]] if task.get("category") else [])
    try:
        if task.get("star_id") and stars.get_star(task["star_id"], include_deleted=False):
            stars.update_star(task["star_id"], content=content, tags=tags, importance=4)
            sid = task["star_id"]
        else:
            sid = stars.add_star(content, "", tags, 4, source=f"watch:{task['client']}", session=task["session_id"], actor="watch")["id"]
    except Exception:
        sid = task.get("star_id")
    out = store.update_task(tid, status="closed", title=title, result=summary[:4000], skill_name=skill_name, star_id=sid)
    log_event("watch_task_close", id=tid, key=task["key"], skill=skill_name or None, by=by)
    return out


def _save_skill(task: dict, merged: dict, title: str, summary: str) -> str:
    from ..evolve import skills
    base = re.sub(r"[^a-z0-9._-]+", "-", str(merged.get("skill_name") or "").lower()).strip("-._")[:50] or "captured-task"
    if not re.match(r"^[a-z0-9]", base):
        base = "task-" + base
    desc = str(merged.get("skill_description") or title)[:180]
    body = str(merged.get("skill_body") or "").strip() or summary
    name = base
    for i in range(2, 9):
        if not skills.find(name, include_archived=True):
            break
        name = f"{base}-{i}"
    md = skills.make_skill_md(name, desc, body + f"\n\n> 来源：对话监控（{task['client']}），{task.get('cwd') or ''}",
                              tags=[task.get("category") or "任务"])
    r = skills.manage("create", name, content=md, source=f"watch:{task['client']}", trust="agent-created",
                      note="对话监控：任务闭环自动沉淀")
    if r.get("success") and not r.get("staged"):
        return name
    if r.get("staged"):
        return name + "（待批准）"
    return ""


def fail_task(tid: int, by: str = "watch") -> dict:
    task = store.get_task(tid)
    if not task:
        raise KeyError("任务不存在")
    if task.get("star_id"):
        try:
            tags = ["失败"] + ([task["category"]] if task.get("category") else [])
            stars.update_star(task["star_id"], content=f"【失败的任务】{task.get('title') or task['task_key']}\n" + _frag_text(task), tags=tags)
        except Exception:
            pass
    log_event("watch_task_fail", id=tid, key=task["key"], by=by)
    return store.update_task(tid, status="failed")


def set_status(tid: int, status: str) -> dict:
    """主人在任务抽屉里手动标记。"""
    if status == "closed":
        return close_task(tid, by="owner")
    if status == "failed":
        return fail_task(tid, by="owner")
    if status == "open":
        t = store.update_task(tid, status="open")
        if t:
            _sync_open_star(t)
        return t
    raise ValueError("状态只能是 open / closed / failed")


def to_json(v) -> str:
    return json.dumps(v, ensure_ascii=False)
