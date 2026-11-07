"""星辰融合：相似的记忆星会融合、归纳、进化成一颗新星。

- 找相似：jieba 实词（去掉「主人」「喜欢」这类到处都有的词）的重合度 + 同标签加分 + （可选）语义相似度。
- 归纳：配好 LLM 时让它把几颗星写成一颗（保留所有具体事实）；没有 LLM 就朴素拼接。
- 进化：新星的「代数」= 成员最大代数 + 1；成员不删除，只是被吸收（fused_into），随时可以拆开还原。
- 时间：新星的时间取成员里最新的那颗，所以它在星图上和最新的成员一样大、一样亮。
每次融合记 star_fuse 事件（成员编号与原文），永久可追溯。
"""
from __future__ import annotations

import json
import re
import threading
import time

from . import config, semantic
from . import db as _db
from .db import fts_query, get_conn, log_event, tok_string

_lock = threading.Lock()
STOP = set("""主人 我们 你们 他们 她们 自己 喜欢 觉得 一个 一些 这个 那个 这样 那样 因为 所以 但是 而且 还是 就是 不是 没有 可以 已经
一只 一个 一杯 一次 每次 什么 怎么 时候 今天 昨天 现在 之前 以后 非常 特别 比较 一下 一起 知道 记得 告诉 说过 提到 表示 需要 希望 应该 可能 AI 用户 助手""".split())


def words(text: str) -> set[str]:
    text = (text or "").lower()
    if _db.HAS_JIEBA:
        import jieba
        ws, run = set(), ""
        for w in jieba.lcut(text) + [" "]:
            w = w.strip()
            if len(w) == 1 and re.fullmatch(r"[\u4e00-\u9fa5]", w) and w not in STOP1:
                run += w            # 「橘」「猫」这类被切开的单字，连起来当一个词
                continue
            if len(run) >= 2:
                ws.add(run)
            run = ""
            if w:
                ws.add(w)
    else:
        ws = set(_db.bigrams(text))
    return {w for w in ws if len(w) >= 2 and w not in STOP and not re.fullmatch(r"[\W_\d]+", w)}


def similarity(a: dict, b: dict, wa: set | None = None, wb: set | None = None) -> float:
    wa = wa if wa is not None else words(a["summary"] + " " + a["content"])
    wb = wb if wb is not None else words(b["summary"] + " " + b["content"])
    if not wa or not wb:
        return 0.0
    inter = len(wa & wb)
    s = inter / len(wa | wb)
    s = max(s, 0.8 * inter / min(len(wa), len(wb)) if inter >= 2 else 0)   # 一颗被另一颗包含
    ta, tb = json.loads(a["tags"] or "[]"), json.loads(b["tags"] or "[]")
    if ta and tb and ta[0] == tb[0]:
        s += 0.08
    return round(min(1.0, s), 4)


STOP1 = set("的了是在和也都很爱叫养我你他她它就还要会把被给对有个只杯次吗呢吧啊呀着过与及或而")


ACTIVE = "deleted_at IS NULL AND fused_into IS NULL"


def find_groups(cfg: dict | None = None, scan: int | None = None) -> list[dict]:
    """返回可融合的组：[{ids, score, summaries}]，最新的星优先。"""
    cfg = cfg or config.load_config()
    m = cfg["memory"]
    thr = float(m.get("fuse_threshold", 0.34))
    max_group = int(m.get("fuse_max_group", 6))
    scan = int(scan or m.get("fuse_scan", 400))
    with get_conn() as c:
        rows = c.execute(f"SELECT * FROM stars WHERE {ACTIVE} ORDER BY created_at DESC LIMIT ?", (scan,)).fetchall()
        byid = {r["id"]: r for r in rows}
        W = {r["id"]: words(r["summary"] + " " + r["content"]) for r in rows}
        parent = {i: i for i in byid}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        size = {i: 1 for i in byid}
        best_edge: dict[tuple, float] = {}
        for r in rows:
            q = fts_query(r["summary"] + " " + r["content"][:200])
            cands = []
            if q and _db.FTS_OK:
                cands = [x["id"] for x in c.execute(
                    f"SELECT s.id FROM stars_fts f JOIN stars s ON s.id=f.rowid WHERE stars_fts MATCH ? AND s.{ACTIVE.replace(' AND ', ' AND s.')}"
                    " ORDER BY bm25(stars_fts) LIMIT 10", (q,)).fetchall()]
            if not cands:
                cands = list(byid)
            for oid in cands:
                if oid == r["id"] or oid not in byid:
                    continue
                key = tuple(sorted((r["id"], oid)))
                if key in best_edge:
                    continue
                s = similarity(r, byid[oid], W[r["id"]], W[oid])
                best_edge[key] = s
        try:
            if semantic.enabled(cfg):
                for key in list(best_edge):
                    sim = semantic.pair_similarity(c, key[0], key[1]) if hasattr(semantic, "pair_similarity") else None
                    if sim is not None and sim >= 0.82:
                        best_edge[key] = max(best_edge[key], thr + 0.01)
        except Exception:
            pass
        for (a, b), s in sorted(best_edge.items(), key=lambda kv: -kv[1]):
            if s < thr:
                break
            ra, rb = find(a), find(b)
            if ra != rb and size[ra] + size[rb] <= max_group:
                parent[rb] = ra
                size[ra] += size[rb]
        groups: dict[int, list[int]] = {}
        for i in byid:
            groups.setdefault(find(i), []).append(i)
    out = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: byid[i]["created_at"])
        sc = [best_edge.get(tuple(sorted((x, y))), 0) for k, x in enumerate(ids) for y in ids[k + 1:]]
        out.append({"ids": ids, "score": round(max(sc), 3), "summaries": [byid[i]["summary"] for i in ids]})
    out.sort(key=lambda g: -max(byid[i]["created_at"] for i in g["ids"]))
    return out


FUSE_PROMPT = """下面是几颗相似的记忆。请把它们归纳成一颗：保留所有具体事实（时间、名字、数字、地点、偏好的细节），去掉重复；
有先后变化的（比如以前喜欢 A、后来改成 B）要写清楚以哪个为准。用第三人称陈述（「主人…」），不要编造。
只输出 JSON：{{"summary": "不超过 30 字的标题", "content": "归纳后的完整记忆"}}

{items}"""


def _naive(rows) -> tuple[str, str]:
    from datetime import datetime
    newest = max(rows, key=lambda r: r["created_at"])
    lines = [f"· {datetime.fromtimestamp(r['created_at']).strftime('%Y-%m-%d')} {r['content'].strip()[:160]}" for r in rows]
    return newest["summary"][:40], f"（由 {len(rows)} 颗相似的记忆融合）\n" + "\n".join(lines)


def fuse(ids: list[int], use_llm: bool = True, cfg: dict | None = None, chat=None) -> dict:
    """把几颗星融合成一颗新星。返回新星。"""
    from . import maintenance, stars as st
    cfg = cfg or config.load_config()
    ids = sorted({int(i) for i in ids})
    if len(ids) < 2:
        raise ValueError("至少要两颗星才能融合")
    with _lock:
        with get_conn() as c:
            rows = c.execute(f"SELECT * FROM stars WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
            if len(rows) != len(ids) or any(r["deleted_at"] or r["fused_into"] for r in rows):
                raise ValueError("有的星已经被隐藏或融合过了")
        summary, content, by_llm = "", "", False
        if use_llm and (chat or maintenance.llm_ready(cfg)):
            from datetime import datetime
            items = "\n".join(f"[{i + 1}] {datetime.fromtimestamp(r['created_at']).strftime('%Y-%m-%d')} {r['content'].strip()[:600]}"
                              for i, r in enumerate(sorted(rows, key=lambda r: r["created_at"])))
            try:
                txt = (chat or (lambda m: maintenance.chat(m, cfg)))([{"role": "user", "content": FUSE_PROMPT.format(items=items)}])
                d = maintenance.parse_json(txt)
                summary, content = str(d.get("summary", "")).strip(), str(d.get("content", "")).strip()
                by_llm = bool(content)
            except Exception as ex:  # noqa: BLE001
                log_event("star_fuse_llm_failed", error=str(ex)[:160])
        if not content:
            summary, content = _naive(rows)
        tags = []
        for r in sorted(rows, key=lambda r: -r["created_at"]):
            for t in json.loads(r["tags"] or "[]"):
                if t not in tags:
                    tags.append(t)
        if "融合" not in tags:
            tags.append("融合")
        now = time.time()
        gen = max(int(r["generation"] or 0) for r in rows) + 1
        with get_conn() as c:
            cur = c.execute(
                "INSERT INTO stars(content, summary, tags, created_at, updated_at, last_recalled_at, recall_count, importance, boost,"
                " source, session, hash, generation, fused_from) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (content, summary[:60] or st.auto_summary(content), json.dumps(tags[:12], ensure_ascii=False),
                 max(r["created_at"] for r in rows), now, max((r["last_recalled_at"] or 0) for r in rows) or None,
                 sum(int(r["recall_count"] or 0) for r in rows), max(int(r["importance"]) for r in rows),
                 max(float(r["boost"] or 1) for r in rows), "fuse:" + ",".join(map(str, ids)), "", st._hash(content), gen,
                 json.dumps(ids)))
            nid = cur.lastrowid
            c.execute(f"UPDATE stars SET fused_into=? WHERE id IN ({','.join('?' * len(ids))})", (nid, *ids))
            st._index(c, nid, content, summary, tags)
            try:
                semantic.store(c, nid, f"{summary}\n{content}", cfg)
            except Exception:
                pass
            row = c.execute("SELECT * FROM stars WHERE id=?", (nid,)).fetchone()
            out = st.to_dict(row, cfg)
        log_event("star_fuse", id=nid, members=ids, generation=gen, by_llm=by_llm, summary=summary, content=content,
                  member_summaries=[r["summary"] for r in rows])
        return {**out, "by_llm": by_llm}


def unfuse(sid: int) -> dict:
    """拆开一颗融合星：成员还原，融合星隐藏（不删除）。"""
    from . import stars as st
    with _lock, get_conn() as c:
        r = c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone()
        if not r or not r["fused_from"]:
            raise ValueError("这颗星不是融合出来的")
        if r["deleted_at"]:
            raise ValueError("已经拆开过了")
        ids = json.loads(r["fused_from"])
        c.execute(f"UPDATE stars SET fused_into=NULL WHERE id IN ({','.join('?' * len(ids))}) AND fused_into=?", (*ids, sid))
        c.execute("UPDATE stars SET deleted_at=? WHERE id=?", (time.time(), sid))
    log_event("star_unfuse", id=sid, members=ids)
    return {"id": sid, "restored": ids, "stars": [st.get_star(i) for i in ids]}


def members(sid: int) -> list[dict]:
    with get_conn() as c:
        rows = c.execute("SELECT id, summary, created_at, generation, fused_from FROM stars WHERE fused_into=? ORDER BY created_at", (sid,)).fetchall()
    return [{"id": r["id"], "summary": r["summary"], "created_at": r["created_at"], "generation": r["generation"]} for r in rows]


def auto_fuse(cfg: dict | None = None, chat=None, force: bool = False) -> list[dict]:
    """自动融合：新星攒够 fuse_every_new 颗（或 force）就找一次相似组并融合。"""
    cfg = cfg or config.load_config()
    m = cfg["memory"]
    if not force and not m.get("fuse_auto", True):
        return []
    stt = config.load_state()
    with get_conn() as c:
        newest = c.execute(f"SELECT COUNT(*) FROM stars WHERE {ACTIVE} AND created_at>? AND source NOT LIKE 'fuse:%'",
                           (float(stt.get("last_fuse_t") or 0),)).fetchone()[0]
    if not force and newest < int(m.get("fuse_every_new", 8)):
        return []
    config.update_state(last_fuse_t=time.time())
    done = []
    for g in find_groups(cfg)[: int(m.get("fuse_max_per_run", 5))]:
        try:
            done.append({"members": g["ids"], "star": fuse(g["ids"], use_llm=m.get("fuse_use_llm", True), cfg=cfg, chat=chat)})
        except ValueError:
            continue
    if done:
        log_event("star_fuse_run", count=len(done))
    return done


__all__ = ["find_groups", "fuse", "unfuse", "members", "auto_fuse", "similarity", "words", "tok_string"]
