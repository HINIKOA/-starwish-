"""主模型整理星系（v0.11.0）：
  · 内容相同的星 → 融合（复用 fusion.py）；
  · 内容不同但同一类 → 放到一起：每颗星有 大类（恒星系，cat_major）和 小类（行星系，cat_minor），
    主模型负责给新星分类、新建 / 合并类别；星图位置由类别推出（galaxy.py）。
主模型默认 400,000 tokens 上下文，一次能看很多星。没配主模型时按标签粗分。
对话监控归纳出新星后会自动排队整理一次（任务抽屉可见）；也可以手动或在对话抽屉里让它整理。
"""
from __future__ import annotations

import json
import threading
import time

from . import config, context, fusion, jobs, maintenance
from .db import get_conn, log_event

META_TAGS = {"未闭环", "任务", "偏好", "失败", "对话", "已闭环"}
ACTIVE = "deleted_at IS NULL AND fused_into IS NULL"

PROMPT = """你是「星愿」记忆库的主模型，负责把记忆星整理成一个银河系：
- 恒星系 = 大类（如 体育、代码、工作、家人、健康），行星系 = 大类下的小类（如 代码 > 环境配置）。
- 给下面「待分类的星」每颗定一个 major（大类）和 minor（小类，可以为空）。尽量复用「已有类别」，确实是新领域才新建；类别名 2~6 个字，中文。
- 如果已有类别里有意思重复的（如「编程」和「代码」），在 merge 里合并（from → to）。
只输出 JSON：{"assign":[{"id":1,"major":"","minor":""}],"merge":[{"from_major":"","from_minor":"","to_major":"","to_minor":""}]}
（merge 的 from_minor 留空表示整个大类合并过去。）

已有类别（大类 / 小类：星数）：
<<CATS>>

待分类的星（编号｜摘要｜标签）：
<<STARS>>"""

_lock = threading.Lock()
_sched = {"pending": False, "running": False, "job": None}


def history_path():
    d = config.soul_path("agent")
    d.mkdir(parents=True, exist_ok=True)
    return d / "organizer.json"


def load_history() -> list[dict]:
    """主模型整理时的累计上下文（之前的分类决定），到阈值自动压缩。"""
    p = history_path()
    try:
        h = json.loads(p.read_text("utf-8")) if p.exists() else []
        return [m for m in h if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")]
    except Exception:
        return []


def save_history(msgs: list[dict]) -> None:
    p = history_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(msgs, ensure_ascii=False), "utf-8")
    import os
    os.replace(tmp, p)


def categories() -> list[dict]:
    with get_conn() as c:
        rows = c.execute(f"SELECT cat_major, cat_minor, COUNT(*) n FROM stars WHERE {ACTIVE} AND cat_major!='' "
                         "GROUP BY cat_major, cat_minor ORDER BY n DESC").fetchall()
    return [{"major": r["cat_major"], "minor": r["cat_minor"], "count": r["n"]} for r in rows]


def set_category(sid: int, major: str, minor: str = "") -> None:
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM stars WHERE id=?", (sid,)).fetchone():
            raise KeyError("星星不存在")
        c.execute("UPDATE stars SET cat_major=?, cat_minor=? WHERE id=?", (_clean(major), _clean(minor) if major else "", sid))
    log_event("star_category", id=sid, major=major, minor=minor)


def _clean(s) -> str:
    return str(s or "").strip().replace("/", "·")[:16]


def heuristic(tags: list[str]) -> tuple[str, str]:
    """没有主模型时：按标签粗分（去掉「任务 / 偏好」这类状态标签）。"""
    real = [t for t in tags if t and t not in META_TAGS]
    if real:
        return real[0], (real[1] if len(real) > 1 else "")
    if "任务" in tags or "未闭环" in tags:
        return "任务", ""
    if "偏好" in tags:
        return "偏好", ""
    return "", ""


def _inherit_fused(c) -> int:
    """融合星继承成员里最多的类别。"""
    n = 0
    for r in c.execute(f"SELECT id, fused_from FROM stars WHERE {ACTIVE} AND cat_major='' AND fused_from!=''").fetchall():
        ids = [int(x) for x in json.loads(r["fused_from"] or "[]")]
        if not ids:
            continue
        rows = c.execute(f"SELECT cat_major, cat_minor FROM stars WHERE id IN ({','.join('?' * len(ids))}) AND cat_major!=''",
                         ids).fetchall()
        if rows:
            best = max({(x[0], x[1]) for x in rows}, key=lambda k: sum(1 for x in rows if (x[0], x[1]) == k))
            c.execute("UPDATE stars SET cat_major=?, cat_minor=? WHERE id=?", (best[0], best[1], r["id"]))
            n += 1
    return n


def _apply_merge(c, m: dict) -> int:
    fm, fn, tm, tn = (_clean(m.get(k)) for k in ("from_major", "from_minor", "to_major", "to_minor"))
    if not fm or not tm or (fm, fn) == (tm, tn):
        return 0
    if fn:
        cur = c.execute(f"UPDATE stars SET cat_major=?, cat_minor=? WHERE {ACTIVE} AND cat_major=? AND cat_minor=?", (tm, tn, fm, fn))
    elif tn:
        cur = c.execute(f"UPDATE stars SET cat_major=?, cat_minor=? WHERE {ACTIVE} AND cat_major=?", (tm, tn, fm))
    else:
        cur = c.execute(f"UPDATE stars SET cat_major=? WHERE {ACTIVE} AND cat_major=?", (tm, fm))
    return cur.rowcount


def organize(cfg: dict | None = None, use_llm: bool = True, fuse: bool = True, max_batches: int = 20) -> dict:
    cfg = cfg or config.load_config()
    res = {"fused": 0, "assigned": 0, "merged": 0, "by_llm": False, "batches": 0, "errors": []}
    if fuse:
        try:
            res["fused"] = len(fusion.auto_fuse(cfg, force=True))
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"融合：{e}")
    with get_conn() as c:
        res["assigned"] += _inherit_fused(c)
    llm_ok = use_llm and context.main_ready(cfg)
    budget = context.budget(cfg, "chat")
    seen: set = set()
    while llm_ok and res["batches"] < max_batches:
        with get_conn() as c:
            rows = c.execute(f"SELECT id, summary, content, tags FROM stars WHERE {ACTIVE} AND cat_major='' ORDER BY created_at DESC").fetchall()
        rows = [r for r in rows if r["id"] not in seen]
        if not rows:
            break
        cats = categories()
        cat_txt = "\n".join(f"- {x['major']} / {x['minor'] or '（无小类）'}：{x['count']}" for x in cats) or "（还没有）"
        lines = [f"{r['id']}｜{(r['summary'] or r['content'] or '')[:120]}｜{','.join(json.loads(r['tags'] or '[]'))}" for r in rows]
        base_cost = context.estimate_tokens(PROMPT + cat_txt)
        packed, _ = context.pack(lines, max(500, budget - base_cost))
        if not packed:
            break
        ids = {int(ln.split("｜", 1)[0]) for ln in packed}
        seen |= ids
        prompt = PROMPT.replace("<<CATS>>", cat_txt).replace("<<STARS>>", "\n".join(packed))
        try:
            hist, info = context.compact_if_needed(load_history(), cfg, "chat", "主模型整理星系",
                                                   extra_tokens=context.estimate_tokens(prompt))
            if info:
                res["compacted"] = res.get("compacted", 0) + 1
            data = context.main_complete(hist + [{"role": "user", "content": prompt}], cfg)
            reply = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            out = maintenance.parse_json(reply)
            save_history(hist + [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}])
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"主模型：{e}"[:200])
            break
        res["batches"] += 1
        res["by_llm"] = True
        with get_conn() as c:
            for a in out.get("assign") or []:
                try:
                    sid = int(a.get("id"))
                except (TypeError, ValueError, AttributeError):
                    continue
                major = _clean(a.get("major"))
                if sid in ids and major:
                    c.execute("UPDATE stars SET cat_major=?, cat_minor=? WHERE id=?", (major, _clean(a.get("minor")), sid))
                    res["assigned"] += 1
            for m in out.get("merge") or []:      # 先分类、再合并（新分的星也跟着并过去）
                if isinstance(m, dict):
                    res["merged"] += _apply_merge(c, m)
    if not res["by_llm"]:
        with get_conn() as c:
            for r in c.execute(f"SELECT id, tags FROM stars WHERE {ACTIVE} AND cat_major=''").fetchall():
                mj, mn = heuristic(json.loads(r["tags"] or "[]"))
                if mj:
                    c.execute("UPDATE stars SET cat_major=?, cat_minor=? WHERE id=?", (_clean(mj), _clean(mn), r["id"]))
                    res["assigned"] += 1
    res["categories"] = len({x["major"] for x in categories()})
    config.update_state(last_organize={"t": time.time(), **{k: v for k, v in res.items() if k != "errors"}})
    log_event("organize", **{k: v for k, v in res.items() if v})
    return res


def run_job(title: str = "整理星系") -> dict:
    with jobs.run("organize", title) as j:
        r = organize()
        j["detail"] = f"融合 {r['fused']}，分类 {r['assigned']}，合并类别 {r['merged']}，共 {r['categories']} 个恒星系" + \
            ("（主模型）" if r["by_llm"] else "（按标签粗分）") + (("；" + "；".join(r["errors"])) if r["errors"] else "")
        if r["errors"] and not r["assigned"]:
            j["status"] = "failed"
        return r


def schedule(delay: float = 5.0) -> bool:
    """排队整理一次（防抖：正在跑就等它跑完再来一次）。返回是否新排了队。"""
    with _lock:
        if _sched["running"]:
            _sched["pending"] = True
            return False
        if _sched["job"]:
            return False
        _sched["job"] = jobs.start("organize", "整理星系（排队中）")
        jid = _sched["job"]

    def go():
        time.sleep(delay)
        while True:
            with _lock:
                _sched["running"], _sched["pending"] = True, False
            try:
                r = organize()
                jobs.finish(jid, "done", f"融合 {r['fused']}，分类 {r['assigned']}，合并类别 {r['merged']}", title="整理星系")
            except Exception as e:  # noqa: BLE001
                jobs.finish(jid, "failed", str(e)[:300], title="整理星系")
            with _lock:
                _sched["running"] = False
                if not _sched["pending"]:
                    _sched["job"] = None
                    return
    threading.Thread(target=go, daemon=True, name="starwish-organize").start()
    return True
