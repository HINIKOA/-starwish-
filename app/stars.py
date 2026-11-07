"""星星（非核心记忆）：增删改查、亮度、分层召回。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime

from . import config, semantic
from .db import (estimate_tokens, fts_query, get_conn, jaccard, log_event,
                 query_terms, tok_string)
from . import db as _db

FIELDS = ("id", "content", "summary", "tags", "created_at", "updated_at", "last_recalled_at",
          "recall_count", "importance", "boost", "source", "session", "deleted_at")


def _hash(content: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", content or "").strip().encode("utf-8")).hexdigest()


def auto_summary(content: str, limit: int = 60) -> str:
    t = re.sub(r"[#>*`\-]+\s*", "", content or "")
    t = re.sub(r"\s+", " ", t).strip()
    m = re.search(r"^(.{8,}?[。！？!?；;])", t)
    s = m.group(1) if m and len(m.group(1)) <= limit else t[:limit]
    return s + ("…" if len(t) > len(s) else "")


def _norm_tags(tags) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,，;；\s]+", tags)
    out = []
    for t in tags:
        t = str(t).strip().lstrip("#")
        if t and t not in out:
            out.append(t[:32])
    return out[:12]


# ---------------- 亮度 ----------------

def brightness(row, now: float | None = None, cfg: dict | None = None) -> float:
    """亮度 = 时间衰减 × (1+ln(1+召回次数)) × 重要度系数 × 手动增益，下限 floor，上限 3。

    时间衰减按「最后一次活动」（创建或被召回）计算，半衰期可配置（默认 30 天）。
    老星会变暗，但永远不低于 floor —— 不会消失。
    """
    cfg = cfg or config.load_config()
    mem = cfg["memory"]
    now = now or time.time()
    ref = max(row["created_at"] or 0, row["last_recalled_at"] or 0)
    age_days = max(0.0, (now - ref) / 86400.0)
    hl = max(0.1, float(mem.get("half_life_days", 30)))
    decay = 0.5 ** (age_days / hl)
    rc = int(row["recall_count"] or 0)
    imp = int(row["importance"] or 3)
    b = decay * (1 + math.log(1 + rc)) * (0.4 + 0.2 * imp) * float(row["boost"] or 1.0)
    return round(min(3.0, max(float(mem.get("brightness_floor", 0.08)), b)), 4)


def to_dict(row, cfg=None, now=None, full=True) -> dict:
    d = {k: row[k] for k in FIELDS if k in row.keys()}
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["brightness"] = brightness(row, now, cfg)
    if not full:
        d.pop("content", None)
    return d


# ---------------- 增删改 ----------------

def _index(conn, sid: int, content: str, summary: str, tags: list[str]) -> None:
    tok = tok_string(f"{summary}\n{content}\n{' '.join(tags)}")
    conn.execute("UPDATE stars SET tok=? WHERE id=?", (tok, sid))
    if _db.FTS_OK:
        conn.execute("DELETE FROM stars_fts WHERE rowid=?", (sid,))
        conn.execute("INSERT INTO stars_fts(rowid, tok) VALUES (?,?)", (sid, tok))


def add_star(content: str, summary: str = "", tags=None, importance: int = 3, source: str = "",
             session: str = "", created_at: float | None = None, merge_near: bool = False,
             actor: str = "api") -> dict:
    content = (content or "").strip()
    if not content:
        raise ValueError("内容为空")
    tags = _norm_tags(tags)
    importance = max(1, min(5, int(importance or 3)))
    summary = (summary or "").strip() or auto_summary(content)
    h = _hash(content)
    cfg = config.load_config()
    with get_conn() as c:
        dup = c.execute("SELECT * FROM stars WHERE hash=? AND deleted_at IS NULL", (h,)).fetchone()
        if dup:
            return {**to_dict(dup, cfg), "duplicate": True}
        if merge_near:
            near = find_near_duplicate(c, content, summary)
            if near is not None:
                return merge_into(c, near, content, summary, tags, importance, source, cfg)
        now = time.time()
        cur = c.execute(
            "INSERT INTO stars(content, summary, tags, created_at, updated_at, importance, source, session, hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (content, summary, json.dumps(tags, ensure_ascii=False), created_at or now, now, importance,
             source or "", session or "", h),
        )
        sid = cur.lastrowid
        _index(c, sid, content, summary, tags)
        try:
            semantic.store(c, sid, f"{summary}\n{content}", cfg)
        except Exception:
            pass
        row = c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone()
        d = to_dict(row, cfg)
    log_event("star_add", id=sid, content=content, summary=summary, tags=tags, importance=importance,
              source=source, session=session, created_at=created_at or now, actor=actor)
    return d


def find_near_duplicate(conn, content: str, summary: str = "", threshold: float = 0.6):
    q = fts_query(summary + " " + content[:300])
    if not q or not _db.FTS_OK:
        return None
    rows = conn.execute(
        "SELECT s.* FROM stars_fts f JOIN stars s ON s.id=f.rowid WHERE stars_fts MATCH ? AND s.deleted_at IS NULL"
        " ORDER BY bm25(stars_fts) LIMIT 10", (q,)).fetchall()
    best, best_s = None, 0.0
    for r in rows:
        s = jaccard(content, r["content"])
        if s > best_s:
            best, best_s = r, s
    return best if best_s >= threshold else None


def merge_into(conn, row, content, summary, tags, importance, source, cfg) -> dict:
    old_tags = json.loads(row["tags"] or "[]")
    new_tags = _norm_tags(old_tags + list(tags or []))
    new_content = row["content"]
    if jaccard(content, row["content"]) < 0.95 and content not in row["content"]:
        new_content = row["content"].rstrip() + "\n——\n" + content
    imp = max(int(row["importance"]), int(importance or 3))
    now = time.time()
    conn.execute("UPDATE stars SET content=?, tags=?, importance=?, updated_at=?, hash=? WHERE id=?",
                 (new_content, json.dumps(new_tags, ensure_ascii=False), imp, now, _hash(new_content), row["id"]))
    _index(conn, row["id"], new_content, row["summary"], new_tags)
    r2 = conn.execute("SELECT * FROM stars WHERE id=?", (row["id"],)).fetchone()
    log_event("star_merge", id=row["id"], merged_content=content, source=source)
    return {**to_dict(r2, cfg), "merged": True}


def get_star(sid: int, include_deleted: bool = True) -> dict | None:
    with get_conn() as c:
        r = c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone()
        if not r or (r["deleted_at"] and not include_deleted):
            return None
        return to_dict(r)


def update_star(sid: int, **fields) -> dict:
    allowed = {"content", "summary", "tags", "importance"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    with get_conn() as c:
        r = c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone()
        if not r:
            raise KeyError("星星不存在")
        old = to_dict(r)
        content = fields.get("content", r["content"]).strip() or r["content"]
        summary = fields.get("summary", r["summary"])
        summary = (summary or "").strip() or auto_summary(content)
        tags = _norm_tags(fields["tags"]) if "tags" in fields else json.loads(r["tags"])
        imp = max(1, min(5, int(fields.get("importance", r["importance"]))))
        c.execute("UPDATE stars SET content=?, summary=?, tags=?, importance=?, updated_at=?, hash=? WHERE id=?",
                  (content, summary, json.dumps(tags, ensure_ascii=False), imp, time.time(), _hash(content), sid))
        _index(c, sid, content, summary, tags)
        new = to_dict(c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone())
    changes = {k: {"old": old.get(k), "new": new.get(k)} for k in ("content", "summary", "tags", "importance")
               if old.get(k) != new.get(k)}
    log_event("star_edit", id=sid, changes=changes)
    return new


def adjust_boost(sid: int, factor: float) -> dict:
    with get_conn() as c:
        r = c.execute("SELECT boost FROM stars WHERE id=?", (sid,)).fetchone()
        if not r:
            raise KeyError("星星不存在")
        nb = max(0.1, min(5.0, float(r["boost"]) * factor))
        c.execute("UPDATE stars SET boost=?, updated_at=? WHERE id=?", (nb, time.time(), sid))
    log_event("star_brighten" if factor > 1 else "star_dim", id=sid, boost=nb)
    return get_star(sid)


def soft_delete(sid: int, restore: bool = False) -> dict:
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM stars WHERE id=?", (sid,)).fetchone():
            raise KeyError("星星不存在")
        c.execute("UPDATE stars SET deleted_at=? WHERE id=?", (None if restore else time.time(), sid))
    log_event("star_restore" if restore else "star_soft_delete", id=sid)
    return get_star(sid)


def list_stars(limit: int = 50, offset: int = 0, include_deleted: bool = False, tag: str | None = None) -> list[dict]:
    cfg = config.load_config()
    now = time.time()
    with get_conn() as c:
        where = "1=1" if include_deleted else "deleted_at IS NULL"
        params: list = []
        if tag:
            where += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')
        rows = c.execute(f"SELECT * FROM stars WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                         (*params, limit, offset)).fetchall()
        return [to_dict(r, cfg, now) for r in rows]


def star_map() -> dict:
    """星图用的紧凑数据：[id, 创建时间, 亮度, 重要度, 主标签, 摘要]。"""
    cfg = config.load_config()
    now = time.time()
    with get_conn() as c:
        rows = c.execute("SELECT * FROM stars WHERE deleted_at IS NULL ORDER BY created_at").fetchall()
        out = []
        for r in rows:
            tags = json.loads(r["tags"] or "[]")
            out.append([r["id"], int(r["created_at"]), brightness(r, now, cfg), r["importance"],
                        tags[0] if tags else "", (r["summary"] or "")[:48]])
    return {"stars": out, "now": int(now)}


# ---------------- 检索与召回 ----------------

def _candidates(conn, query: str, cfg: dict, limit: int = 200) -> dict[int, float]:
    """返回 {id: 相关度(0~1]}。FTS5(bm25) 为主；可选语义检索并入；都没有则 LIKE 兜底。"""
    rel: dict[int, float] = {}
    q = fts_query(query)
    if q and _db.FTS_OK:
        rows = conn.execute(
            "SELECT f.rowid AS id, bm25(stars_fts) AS s FROM stars_fts f JOIN stars s ON s.id=f.rowid"
            " WHERE stars_fts MATCH ? AND s.deleted_at IS NULL ORDER BY s LIMIT ?", (q, limit)).fetchall()
        if rows:
            best = min(r["s"] for r in rows) or -1e-9
            for r in rows:
                rel[r["id"]] = max(0.05, (r["s"] / best) if best < 0 else 0.5)
        if len(rel) < 5:  # 词/二元组命中太少 → 单字兜底（权重减半）
            uq = fts_query(query, unigram=True)
            if uq:
                urows = conn.execute(
                    "SELECT f.rowid AS id, bm25(stars_fts) AS s FROM stars_fts f JOIN stars s ON s.id=f.rowid"
                    " WHERE stars_fts MATCH ? AND s.deleted_at IS NULL ORDER BY s LIMIT ?", (uq, limit)).fetchall()
                if urows:
                    ub = min(r["s"] for r in urows) or -1e-9
                    for r in urows:
                        v = 0.5 * max(0.05, (r["s"] / ub) if ub < 0 else 0.5)
                        if r["id"] not in rel:
                            rel[r["id"]] = v
    try:
        for sid, sim in semantic.search(conn, query, cfg).items():
            rel[sid] = max(rel.get(sid, 0.0), min(1.0, (sim - 0.35) / 0.45))
    except Exception:
        pass
    if not rel:
        terms = [t for t in query_terms(query)][:8] or [query.strip()]
        for t in terms:
            if not t:
                continue
            for r in conn.execute("SELECT id FROM stars WHERE deleted_at IS NULL AND (content LIKE ? OR summary LIKE ?)"
                                  " LIMIT 50", (f"%{t}%", f"%{t}%")).fetchall():
                rel[r["id"]] = rel.get(r["id"], 0) + 1.0 / len(terms)
    return rel


def search(query: str, limit: int = 5000) -> list[dict]:
    """面板搜索：不计入召回次数。"""
    cfg = config.load_config()
    now = time.time()
    with get_conn() as c:
        rel = _candidates(c, query, cfg, limit)
        if not rel:
            return []
        rows = c.execute(f"SELECT * FROM stars WHERE id IN ({','.join('?' * len(rel))})", list(rel)).fetchall()
        out = []
        for r in rows:
            d = to_dict(r, cfg, now, full=False)
            d["relevance"] = round(rel[r["id"]], 4)
            out.append(d)
    out.sort(key=lambda d: -d["relevance"])
    return out


def recall_index() -> dict:
    """第一层：极简索引（几十个 token）。"""
    with get_conn() as c:
        n = c.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM stars WHERE deleted_at IS NULL").fetchone()
        tag_rows = c.execute("SELECT tags FROM stars WHERE deleted_at IS NULL").fetchall()
        pend = c.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
    counts: dict[str, int] = {}
    for r in tag_rows:
        for t in json.loads(r[0] or "[]"):
            counts[t] = counts.get(t, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "-"
    text = (f"星{n[0]}颗 {fmt(n[1])}~{fmt(n[2])} 标签:" + ",".join(f"{k}({v})" for k, v in top)
            + " | 用recall(query)按需取摘要,recall_detail(id)取全文")
    return {"count": n[0], "from": fmt(n[1]), "to": fmt(n[2]), "top_tags": top, "pending_proposals": pend,
            "text": text, "tokens": estimate_tokens(text)}


def recall(query: str, budget_tokens: int | None = None, bump: bool = True, limit_candidates: int = 200) -> dict:
    """第二层：在 token 预算内返回排好序的星星摘要。排序 = 相关度 × (1 + w × 亮度归一)。"""
    cfg = config.load_config()
    budget = int(budget_tokens or cfg["memory"].get("recall_budget_default", 600))
    w = float(cfg["memory"].get("brightness_weight", 0.5))
    now = time.time()
    query = (query or "").strip()
    header = "【星魂·相关记忆】(#编号 日期 摘要；需要全文可调用 recall_detail)"
    used = estimate_tokens(header)
    items = []
    with get_conn() as c:
        if query:
            rel = _candidates(c, query, cfg, limit_candidates)
        else:
            rel = {}
        if rel:
            rows = c.execute(f"SELECT * FROM stars WHERE id IN ({','.join('?' * len(rel))})", list(rel)).fetchall()
        elif not query:
            rows = c.execute("SELECT * FROM stars WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 300").fetchall()
        else:
            rows = []
        scored = []
        for r in rows:
            b = brightness(r, now, cfg)
            relv = rel.get(r["id"], 1.0)
            scored.append((relv * (1 + w * min(1.0, b / 1.5)), b, r))
        scored.sort(key=lambda x: -x[0])
        lines = []
        for score, b, r in scored:
            day = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d")
            line = f"#{r['id']} {day} {r['summary']}"
            t = estimate_tokens(line) + 1
            if used + t > budget:
                continue
            used += t
            lines.append(line)
            items.append({"id": r["id"], "date": day, "summary": r["summary"], "score": round(score, 4),
                          "brightness": b, "tags": json.loads(r["tags"] or "[]")})
            if used >= budget - 4:
                break
        if bump and items:
            ids = [i["id"] for i in items]
            c.execute(f"UPDATE stars SET recall_count=recall_count+1, last_recalled_at=? WHERE id IN ({','.join('?' * len(ids))})",
                      (now, *ids))
    text = (header + "\n" + "\n".join(lines)) if items else ""
    tokens = estimate_tokens(text)
    if bump and items:
        log_event("recall", query=query[:200], ids=[i["id"] for i in items], tokens=tokens)
        st = config.load_state()
        hist = (st.get("recall_tokens") or [])[-49:] + [tokens]
        config.update_state(recall_tokens=hist)
    return {"query": query, "budget_tokens": budget, "used_tokens": tokens, "items": items, "text": text}


def recall_detail(sid: int, bump: bool = True, raw_chars: int = 4000) -> dict | None:
    """第三层：单颗星的全文 + 原始对话片段（如有）。"""
    with get_conn() as c:
        r = c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        if bump:
            c.execute("UPDATE stars SET recall_count=recall_count+1, last_recalled_at=? WHERE id=?", (time.time(), sid))
            r = c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone()
    d = to_dict(r)
    d["raw"] = _raw_for(r["source"], raw_chars)
    if bump:
        log_event("recall_detail", id=sid)
    return d


def _raw_for(source: str, limit: int) -> str:
    """source 形如 archive:<相对路径> 时，读回原始对话（只读）。"""
    if not source or not source.startswith("archive:"):
        return ""
    rel = source.split(":", 1)[1].split("#", 1)[0]
    p = (config.SOUL_DIR / rel).resolve()
    if config.SOUL_DIR not in p.parents or not p.exists():
        return ""
    try:
        txt = p.read_text("utf-8", errors="replace")
    except Exception:
        return ""
    if p.suffix == ".jsonl":
        out = []
        for ln in txt.splitlines():
            try:
                m = json.loads(ln)
                out.append(f"{m.get('role', '?')}: {m.get('content', '')}")
            except Exception:
                pass
        txt = "\n".join(out)
    return txt[:limit]


def stats() -> dict:
    from . import core

    cfg = config.load_config()
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with get_conn() as c:
        total = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NULL").fetchone()[0]
        deleted = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NOT NULL").fetchone()[0]
        today = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NULL AND created_at>=?", (today0,)).fetchone()[0]
        pend = c.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
        rng = c.execute("SELECT MIN(created_at), MAX(created_at) FROM stars WHERE deleted_at IS NULL").fetchone()
    hist = config.load_state().get("recall_tokens") or []
    ct = core.core_tokens()
    avg = int(sum(hist) / len(hist)) if hist else None
    budget = int(cfg["proxy"]["budget_tokens"])
    return {
        "total": total, "deleted": deleted, "today": today, "pending_proposals": pend,
        "core_tokens": ct, "recall_budget": budget, "avg_recall_tokens": avg,
        "est_tokens_per_turn": ct + (avg if avg is not None else budget),
        "from": rng[0], "to": rng[1], "fts": _db.FTS_OK, "jieba": _db.HAS_JIEBA,
        "semantic": semantic.enabled(cfg),
        "archive_files": sum(1 for _ in config.ARCHIVE_DIR.rglob("*") if _.is_file()) if config.ARCHIVE_DIR.exists() else 0,
    }


def backfill_embeddings(max_n: int = 500) -> int:
    cfg = config.load_config()
    if not semantic.enabled(cfg):
        return 0
    n = 0
    with get_conn() as c:
        rows = c.execute("SELECT s.id, s.summary, s.content FROM stars s LEFT JOIN embeddings e ON e.star_id=s.id"
                         " WHERE e.star_id IS NULL AND s.deleted_at IS NULL LIMIT ?", (max_n,)).fetchall()
        for r in rows:
            semantic.store(c, r["id"], f"{r['summary']}\n{r['content']}", cfg)
            n += 1
    return n
