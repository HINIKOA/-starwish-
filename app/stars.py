"""星星（非核心记忆）：增删改查、亮度、分层召回。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from contextlib import nullcontext
from datetime import datetime

from . import archive, config, semantic, stars_retrieval
from .db import (estimate_tokens, fts_query, get_conn, jaccard, log_event,
                 query_terms, tok_string)
from . import db as _db

FIELDS = ("id", "content", "summary", "tags", "created_at", "updated_at", "last_recalled_at",
          "recall_count", "importance", "boost", "source", "session", "deleted_at", "fused_into", "generation", "fused_from",
          "cat_major", "cat_minor")


def _hash(content: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", content or "").strip().encode("utf-8")).hexdigest()


def auto_summary(content: str, limit: int = 60) -> str:
    # Strip line prefixes, not punctuation inside identifiers or values.
    t = re.sub(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]*|[-*+][ \t]+)", "", content or "")
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
    if "fused_from" in d:
        d["fused_from"] = json.loads(d["fused_from"]) if d["fused_from"] else []
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


def _reembed(conn, sid: int, text: str) -> None:
    """内容变了：旧向量全部作废，按当前档位重算（接口档交给后台）。"""
    try:
        semantic.forget(conn, sid)
        semantic.store(conn, sid, text, config.load_config())
    except Exception:
        pass


def add_star(content: str, summary: str = "", tags=None, importance: int = 3, source: str = "",
             session: str = "", created_at: float | None = None, merge_near: bool = False,
             actor: str = "api", *, major: str = "", minor: str = "", classify: bool = True,
             conn=None, emit=None, archive_ref=None) -> dict:
    from . import maintenance, organize

    content = (content or "").strip()
    if not content:
        raise ValueError("内容为空")
    tags = _norm_tags(tags)
    importance = max(1, min(5, int(importance or 3)))
    summary = (summary or "").strip() or auto_summary(content)
    h = _hash(content)
    cfg = config.load_config()
    major, minor, _ = organize.metadata({"major": major, "minor": minor})
    emit = emit if emit is not None else log_event

    def existing(c):
        dup = c.execute("SELECT * FROM stars WHERE hash=? AND deleted_at IS NULL AND fused_into IS NULL", (h,)).fetchone()
        if dup:
            return {**to_dict(dup, cfg), "duplicate": True}
        absorbed = c.execute("SELECT fused_into FROM stars WHERE hash=? AND deleted_at IS NULL AND fused_into IS NOT NULL", (h,)).fetchone()
        if absorbed:  # 这条记忆已经被融合进某颗星了
            top = absorbed["fused_into"]
            for _ in range(10):
                nxt = c.execute("SELECT fused_into FROM stars WHERE id=?", (top,)).fetchone()
                if not nxt or not nxt["fused_into"]:
                    break
                top = nxt["fused_into"]
            r = c.execute("SELECT * FROM stars WHERE id=?", (top,)).fetchone()
            if r and not r["deleted_at"]:
                return {**to_dict(r, cfg), "duplicate": True}
        if merge_near:
            near = find_near_duplicate(c, content, summary)
            if near is not None:
                return merge_into(c, near, content, summary, tags, importance, source, cfg, emit=emit)
        return None

    if conn is None and classify and (not major or not tags) and maintenance.llm_ready(cfg):
        with get_conn() as c:
            duplicate = existing(c)
            if duplicate:
                return duplicate
        auto_major, auto_minor, auto_tags = organize.classify_new(content, summary, tags, cfg)
        if not major:
            major, minor = auto_major, auto_minor
        tags = _norm_tags(tags + auto_tags)

    with (get_conn() if conn is None else nullcontext(conn)) as c:
        if conn is None:
            c.execute("BEGIN IMMEDIATE")
        duplicate = existing(c)
        if duplicate:
            return duplicate
        now = time.time()
        cur = c.execute(
            "INSERT INTO stars(content, summary, tags, created_at, updated_at, importance, source, session, hash, cat_major, cat_minor)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (content, summary, json.dumps(tags, ensure_ascii=False), created_at or now, now, importance,
             source or "", session or "", h, major, minor),
        )
        sid = cur.lastrowid
        if archive_ref is not None:
            c.execute("INSERT INTO star_archive_refs VALUES (?, ?, ?, ?, ?)", (sid, *archive_ref))
        _index(c, sid, content, summary, tags)
        try:
            semantic.store(c, sid, f"{summary}\n{content}", cfg)
        except Exception:
            pass
        row = c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone()
        d = to_dict(row, cfg)
    emit("star_add", id=sid, content=content, summary=summary, tags=tags, importance=importance,
              source=source, session=session, created_at=created_at or now, actor=actor, major=major, minor=minor)
    return d


def find_near_duplicate(conn, content: str, summary: str = "", threshold: float = 0.6):
    q = fts_query(summary + " " + content[:300])
    if not q or not _db.FTS_OK:
        return None
    rows = conn.execute(
        "SELECT s.* FROM stars_fts f JOIN stars s ON s.id=f.rowid WHERE stars_fts MATCH ? AND s.deleted_at IS NULL AND s.fused_into IS NULL"
        " ORDER BY bm25(stars_fts) LIMIT 10", (q,)).fetchall()
    best, best_s = None, 0.0
    for r in rows:
        s = jaccard(content, r["content"])
        if s > best_s:
            best, best_s = r, s
    return best if best_s >= threshold else None


def merge_into(conn, row, content, summary, tags, importance, source, cfg, *, emit=log_event) -> dict:
    old_tags = json.loads(row["tags"] or "[]")
    new_tags = _norm_tags(old_tags + list(tags or []))
    new_content = row["content"]
    if jaccard(content, row["content"]) < 0.95 and content not in row["content"]:
        new_content = row["content"].rstrip() + "\n——\n" + content
        conn.execute("DELETE FROM star_archive_refs WHERE star_id=?", (row["id"],))
    imp = max(int(row["importance"]), int(importance or 3))
    now = time.time()
    conn.execute("UPDATE stars SET content=?, tags=?, importance=?, updated_at=?, hash=? WHERE id=?",
                 (new_content, json.dumps(new_tags, ensure_ascii=False), imp, now, _hash(new_content), row["id"]))
    _index(conn, row["id"], new_content, row["summary"], new_tags)
    _reembed(conn, row["id"], f"{row['summary']}\n{new_content}")
    r2 = conn.execute("SELECT * FROM stars WHERE id=?", (row["id"],)).fetchone()
    emit("star_merge", id=row["id"], merged_content=content, source=source)
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
        if content != r["content"] or summary != r["summary"]:
            c.execute("DELETE FROM star_archive_refs WHERE star_id=?", (sid,))
        _index(c, sid, content, summary, tags)
        _reembed(c, sid, f"{summary}\n{content}")
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
        where = "1=1" if include_deleted else "deleted_at IS NULL AND fused_into IS NULL"
        params: list = []
        if tag:
            where += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')
        rows = c.execute(f"SELECT * FROM stars WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                         (*params, limit, offset)).fetchall()
        return [to_dict(r, cfg, now) for r in rows]


def star_map() -> dict:
    """星图用的紧凑数据：[id, 创建时间, 亮度, 重要度, 主标签, 摘要, 融合代数, 融合成员编号]。"""
    cfg = config.load_config()
    now = time.time()
    with get_conn() as c:
        rows = c.execute("SELECT * FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL ORDER BY created_at").fetchall()
        out, cats = [], []
        for r in rows:
            tags = json.loads(r["tags"] or "[]")
            ff = json.loads(r["fused_from"]) if r["fused_from"] else []
            out.append([r["id"], int(r["created_at"]), brightness(r, now, cfg), r["importance"],
                        tags[0] if tags else "", (r["summary"] or "")[:48], int(r["generation"] or 0), ff])
            cats.append((r["id"], r["cat_major"] or "", r["cat_minor"] or "", int(r["created_at"])))
    from . import galaxy
    return {"stars": out, "now": int(now), "galaxy": galaxy.layout(cats)}


# ---------------- 检索与召回 ----------------

def _candidates(conn, query: str, cfg: dict, limit: int = 200, *, for_recall: bool = False) -> dict[int, float]:
    """返回 {id: 相关度(0~1]}。FTS5(bm25) 为主；可选语义检索并入；都没有则 LIKE 兜底。"""
    if for_recall:
        return stars_retrieval.candidates(conn, query, cfg, limit)
    rel: dict[int, float] = {}
    q = fts_query(query)
    if q and _db.FTS_OK:
        rows = conn.execute(
            "SELECT f.rowid AS id, bm25(stars_fts) AS s FROM stars_fts f JOIN stars s ON s.id=f.rowid"
            " WHERE stars_fts MATCH ? AND s.deleted_at IS NULL AND s.fused_into IS NULL ORDER BY s LIMIT ?", (q, limit)).fetchall()
        if rows:
            best = min(r["s"] for r in rows) or -1e-9
            for r in rows:
                rel[r["id"]] = max(0.05, (r["s"] / best) if best < 0 else 0.5)
        if len(rel) < 5:  # 词/二元组命中太少 → 单字兜底（权重减半）
            uq = fts_query(query, unigram=True)
            if uq:
                urows = conn.execute(
                    "SELECT f.rowid AS id, bm25(stars_fts) AS s FROM stars_fts f JOIN stars s ON s.id=f.rowid"
                    " WHERE stars_fts MATCH ? AND s.deleted_at IS NULL AND s.fused_into IS NULL ORDER BY s LIMIT ?", (uq, limit)).fetchall()
                if urows:
                    ub = min(r["s"] for r in urows) or -1e-9
                    for r in urows:
                        v = 0.5 * max(0.05, (r["s"] / ub) if ub < 0 else 0.5)
                        if r["id"] not in rel:
                            rel[r["id"]] = v
    try:  # 第二路：向量（语义）。和字面命中取较大值，字面完全命中仍然排最前
        for sid, v in semantic.relevance(semantic.search(conn, query, cfg), cfg).items():
            rel[sid] = max(rel.get(sid, 0.0), v)
    except Exception:
        pass
    if not rel:
        terms = [t for t in query_terms(query)][:8] or [query.strip()]
        for t in terms:
            if not t:
                continue
            for r in conn.execute("SELECT id FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL AND (content LIKE ? OR summary LIKE ?)"
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
        n = c.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL").fetchone()
        tag_rows = c.execute("SELECT tags FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL").fetchall()
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


def watch_origin(c, row, memo: dict[int, str | None]) -> str | None:
    """A fused star belongs to the watch session only if every ancestor agrees."""
    sid = row["id"]
    if sid in memo:
        return memo[sid]
    memo[sid] = None
    try:
        parents = json.loads(row["fused_from"] or "[]")
        if not isinstance(parents, list) or any(type(i) is not int for i in parents):
            return None
        if parents:
            origins = set()
            for pid in parents:
                if pid not in memo:
                    parent = c.execute("SELECT id, source, session, fused_from FROM stars WHERE id=?", (pid,)).fetchone()
                    if parent is None:
                        return None
                    watch_origin(c, parent, memo)
                origins.add(memo[pid])
            if len(origins) == 1:
                memo[sid] = origins.pop()
        elif row["source"] in ("watch:codex", "watch:codex:delegated", "watch:codex:precompact"):
            memo[sid] = row["session"] or None
    except (TypeError, ValueError, RecursionError):
        return None
    return memo[sid]


_CURRENT_VERSION = re.compile(r"现在|现行|当前|最新|新版|改(?:版|规则)后|更新后|调整后")
_PAST_VERSION = re.compile(r"旧版|原先|原来|之前|以前|改(?:版|规则)前|最初|最早|过去|作废")
_REVISION_CUE = re.compile(r"改为|改成|更改|调整为|更新为|从.{1,16}起|新规|不再|不必等|不用等|仍.{0,64}(?:但|而)")
_RETIRED_CLAUSE = re.compile(r"^\s*(?:原先|原来|旧版|过去|最初|最早|以前)[^。！？；;\n]{2,100}(?:作废|已取消|已废止)[^。！？；;\n]*")
_REAL_PROFILE_QUERY = re.compile(r"(?:真实|实际)用户|真人本人|用户本人|主人本人|我本人")
_WORK_COMPARISON = re.compile(r"比较|对比|分别|异同|各自|各有|这两|这几|两部|两本|不同作品")


def _work_titles(text: str) -> set[str]:
    return stars_retrieval.work_titles(text)


def _matches_work_titles(asked: set[str], row) -> bool:
    if not asked:
        return True
    seen = _work_titles(row["summary"]) | _work_titles(row["content"])
    return not seen or bool(asked & seen)


def _competing_work_titles(query: str, scored: list) -> list[str]:
    if not query or len(scored) < 2 or _work_titles(query) or _WORK_COMPARISON.search(query):
        return []
    top_score, _, first = scored[0]
    lead_titles = _work_titles(first["summary"] + " " + first["content"])
    if top_score <= 0 or len(lead_titles) != 1:
        return []
    lead = next(iter(lead_titles))
    terms = query_terms(query)
    lowered = query.casefold()
    if not terms or lead in lowered:
        return []
    if not any(t in (first["summary"] + " " + first["content"]).casefold() for t in terms):
        return []
    for score, _, row in scored[1:]:
        if score < top_score * 0.96:
            break
        content = (row["summary"] + " " + row["content"]).casefold()
        names = _work_titles(content)
        if len(names) == 1 and names != lead_titles and any(t in content for t in terms):
            other = next(iter(names))
            return [] if other in lowered else sorted((lead, other))
    return []


def _clarification_for_works(titles: list[str], budget: int) -> str:
    options = "或".join(f"《{name}》" for name in titles)
    for prompt in (f"可能指{options}。请先确认作品，不要合并设定。", "请先确认是哪部作品。"):
        if estimate_tokens(prompt) <= budget:
            return prompt
    return ""


def _shared_topic_anchors(query: str, reference: str) -> set[str]:
    reference = reference.casefold()
    anchors = set()
    for word in re.findall(r"[a-z0-9]{5,}|[\u3400-\u9fff]{4,}", query[:240].casefold()):
        if word.isascii():
            anchors.add(word)
        else:
            anchors.update(word[i:i + 4] for i in range(len(word) - 3))
    return {a for a in anchors if a in reference and not (_CURRENT_VERSION.search(a) or _PAST_VERSION.search(a))}


def _prefer_current_revision(query: str, scored: list) -> None:
    """Break a close tie only for an explicit current-version query on the same subject."""
    if len(scored) < 2 or not _CURRENT_VERSION.search(query) or _PAST_VERSION.search(query):
        return
    lead_score, _, lead = scored[0]
    anchors = _shared_topic_anchors(query, lead["summary"] + " " + lead["content"])
    if not anchors or lead_score <= 0:
        return
    match = None
    for i, (score, _, row) in enumerate(scored[1:], 1):
        if score < lead_score * 0.92:
            break
        if row["created_at"] <= lead["created_at"] or row["cat_major"] != lead["cat_major"]:
            continue
        candidate = row["summary"] + " " + row["content"]
        if (any(a in candidate.casefold() for a in anchors) and _REVISION_CUE.search(candidate)
                and (match is None or row["created_at"] > scored[match][2]["created_at"])):
            match = i
    if match is not None:
        _, brightness_value, row = scored.pop(match)
        scored.insert(0, (lead_score + 0.0001, brightness_value, row))


def _historical_parent(c, query: str, row):
    """A past-version query can cite the prior member of a revision fusion."""
    if not _PAST_VERSION.search(query) or _CURRENT_VERSION.search(query):
        return None
    try:
        ids = json.loads(row["fused_from"] or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(ids, list) or len(ids) < 2 or any(type(i) is not int for i in ids):
        return None
    parents = c.execute(
        f"SELECT id, summary, content FROM stars WHERE deleted_at IS NULL AND id IN ({','.join('?' * len(ids))}) "
        "ORDER BY created_at, id", ids
    ).fetchall()
    if len(parents) != len(ids):
        return None
    # A later parent can add an exception without repeating the retired rule.
    terms = query_terms(query)
    best = None
    best_score = -1
    for parent in parents:
        content = parent["content"]
        if not _shared_topic_anchors(query, content):
            continue
        for clause in re.split(r"[。！？；;\n]", content):
            match = _RETIRED_CLAUSE.match(clause)
            if match:
                summary = match.group().strip()
                relevance = sum(len(t) for t in terms if t in summary.casefold())
                if relevance > best_score:
                    best = {"id": parent["id"], "summary": summary}
                    best_score = relevance
    if best is not None:
        return best
    if not _REVISION_CUE.search(parents[-1]["content"]):
        return None
    older = parents[0 if ("最初" in query or "最早" in query) else -2]
    anchors = _shared_topic_anchors(query, parents[-1]["content"])
    return older if any(a in older["content"].casefold() for a in anchors) else None


_FALLBACK_FUSION = re.compile(r"^（由 \d+ 颗相似的记忆融合）")


def _excerpt_for_recall(query: str, summary: str, content: str, terms: list[str] | None = None) -> str:
    if not summary.endswith("…") or not query or _PAST_VERSION.search(query):
        return summary
    terms = query_terms(query) if terms is None else terms

    def score(text: str) -> int:
        lowered = text.casefold()
        return sum(len(t) for t in terms if t in lowered)

    chunks = re.findall(r"[^。；！？;]+[。；！？;]*", re.sub(r"\s+", " ", content).strip()[:600])[:12]
    best, amount = summary, score(summary)
    for i in range(len(chunks)):
        for end in range(i + 1, min(len(chunks), i + 3) + 1):
            if _CURRENT_VERSION.search(query) and any(_RETIRED_CLAUSE.match(chunk) for chunk in chunks[i:end]):
                break
            snippet = ("…" if i else "") + "".join(chunks[i:end]).strip() + ("…" if end < len(chunks) else "")
            if len(snippet) > 96:
                break
            priority = score(snippet)
            if priority > amount:
                best, amount = snippet, priority
    return best


def _fallback_fusion_evidence(c, query: str, row) -> tuple[str, int | None]:
    summary = row["summary"]
    if not query or _PAST_VERSION.search(query):
        return summary, None
    if not _FALLBACK_FUSION.match(row["content"]):
        return summary, None
    terms = query_terms(query)

    def relevance(text: str) -> int:
        lowered = text.casefold()
        return sum(len(t) for t in terms if t in lowered)

    best = relevance(summary)
    evidence_id = None
    if _CURRENT_VERSION.search(query):
        try:
            ids = json.loads(row["fused_from"] or "[]")
        except (TypeError, ValueError):
            return summary, None
        if not isinstance(ids, list) or not 2 <= len(ids) <= 32 or any(type(i) is not int for i in ids):
            return summary, None
        newest = c.execute(
            f"SELECT id, summary, content FROM stars WHERE deleted_at IS NULL AND id IN ({','.join('?' * len(ids))}) "
            "ORDER BY created_at DESC, id DESC LIMIT 1", ids
        ).fetchone()
        if newest is not None and _shared_topic_anchors(query, newest["content"]):
            option = _excerpt_for_recall(query, newest["summary"] or auto_summary(newest["content"]), newest["content"], terms)
            if relevance(option) > best and not any(_RETIRED_CLAUSE.match(s) for s in re.split(r"[。！？；;\n]", option)):
                return option, newest["id"]
        return summary, None
    pending = [row]
    visited = {row["id"]}
    while pending and len(visited) < 32:
        parent = pending.pop()
        try:
            ids = json.loads(parent["fused_from"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(ids, list):
            continue
        for sid in ids[:32]:
            if type(sid) is not int or sid in visited or len(visited) >= 32:
                continue
            visited.add(sid)
            leaf = c.execute("SELECT id, summary, content, fused_from FROM stars WHERE id=? AND deleted_at IS NULL",
                             (sid,)).fetchone()
            if leaf is None:
                continue
            if _FALLBACK_FUSION.match(leaf["content"]):
                pending.append(leaf)
            option = _excerpt_for_recall(query, leaf["summary"] or auto_summary(leaf["content"]), leaf["content"], terms)
            improved = relevance(option)
            if improved > best:
                summary, best, evidence_id = option, improved, sid
    return summary, evidence_id


def _clip_recall_summary(prefix: str, summary: str, remaining: int, query: str) -> str:
    terms = query_terms(query)
    clauses = ["…" + part + "…" for part in re.split(r"[，。；！？;]", summary)
               if len(part) >= 8 and estimate_tokens(prefix + "…" + part + "…") + 1 <= remaining]
    if clauses and terms:
        best = max(clauses, key=lambda line: sum(len(t) for t in terms if t in line.casefold()))
        if any(t in best.casefold() for t in terms):
            return best
    lo, hi = 0, len(summary)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(prefix + summary[:mid] + "…") + 1 <= remaining:
            lo = mid
        else:
            hi = mid - 1
    return summary[:lo].rstrip() + "…" if lo >= 8 else ""


def recall(query: str, budget_tokens: int | None = None, bump: bool = True, limit_candidates: int = 200) -> dict:
    """第二层：在 token 预算内返回排好序的星星摘要。排序 = 相关度 × (1 + w × 亮度归一)。"""
    cfg = config.load_config()
    budget = max(0, int(cfg["memory"].get("recall_budget_default", 600) if budget_tokens is None else budget_tokens))
    w = float(cfg["memory"].get("brightness_weight", 0.5))
    now = time.time()
    query = (query or "").strip()
    header = "【星愿·相关记忆】(#编号 日期 摘要；需要全文可调用 recall_detail)"
    items = []
    if not budget:
        return {"query": query, "budget_tokens": budget, "used_tokens": 0, "items": [], "text": ""}
    scope = os.environ.get("STARWISH_WATCH_CODEX_ONLY_SESSION")
    if scope is not None and _REAL_PROFILE_QUERY.search(query):
        return {"query": query, "budget_tokens": budget, "used_tokens": 0, "items": [], "text": "",
                "reason": "synthetic_session_scope"}
    requested_works = _work_titles(query)
    with get_conn() as c:
        if query:
            rel = _candidates(c, query, cfg, limit_candidates, for_recall=True)
        else:
            rel = {}
        if rel:
            rows = c.execute(f"SELECT * FROM stars WHERE id IN ({','.join('?' * len(rel))})", list(rel)).fetchall()
        elif not query:
            rows = c.execute("SELECT * FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL ORDER BY created_at DESC LIMIT 300").fetchall()
        else:
            rows = []
        scored = []
        origins: dict[int, str | None] = {}
        for r in rows:
            if scope is not None and watch_origin(c, r, origins) != scope:
                continue
            if not _matches_work_titles(requested_works, r):
                continue
            b = brightness(r, now, cfg)
            relv = rel.get(r["id"], 1.0)
            scored.append((relv * (1 + w * min(1.0, b / 1.5)), b, r))
        scored.sort(key=lambda x: -x[0])
        _prefer_current_revision(query, scored)
        ambiguity = _competing_work_titles(query, scored)
        if ambiguity:
            clarification = _clarification_for_works(ambiguity, budget)
            return {"query": query, "budget_tokens": budget, "used_tokens": estimate_tokens(clarification),
                    "items": [], "text": clarification, "reason": "competing_work_titles", "works": ambiguity}
        text = header
        for score, b, r in scored:
            day = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d")
            older = _historical_parent(c, query, r)
            if older:
                summary, evidence_id = f"历史版本（父星 #{older['id']}）：{older['summary']}", older["id"]
            else:
                summary, evidence_id = _fallback_fusion_evidence(c, query, r) if r["fused_from"] else (r["summary"], None)
                if evidence_id is None and not _FALLBACK_FUSION.match(r["content"]):
                    summary = _excerpt_for_recall(query, summary, r["content"])
            prefix = f"#{r['id']} {day} "
            candidate = text + "\n" + prefix + summary
            if estimate_tokens(candidate) > budget:
                summary = _clip_recall_summary(prefix, summary, budget - estimate_tokens(text), query) if not items else ""
                if not summary:
                    continue
                candidate = text + "\n" + prefix + summary
                if estimate_tokens(candidate) > budget:
                    continue
            text = candidate
            items.append({"id": r["id"], "date": day, "summary": summary, "score": round(score, 4),
                          "brightness": b, "tags": json.loads(r["tags"] or "[]")})
            if evidence_id is not None:
                items[-1]["evidence_id"] = evidence_id
            if estimate_tokens(text) >= budget:
                break
        if bump and items:
            ids = [i["id"] for i in items]
            c.execute(f"UPDATE stars SET recall_count=recall_count+1, last_recalled_at=? WHERE id IN ({','.join('?' * len(ids))})",
                      (now, *ids))
    text = text if items else ""
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
        if bump:
            c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM stars WHERE id=? AND deleted_at IS NULL AND fused_into IS NULL", (sid,)).fetchone()
        if not r:
            return None
        if bump:
            c.execute("UPDATE stars SET recall_count=recall_count+1, last_recalled_at=? WHERE id=?", (time.time(), sid))
            r = c.execute("SELECT * FROM stars WHERE id=?", (sid,)).fetchone()
        ref = _archive_ref(c, sid)
    d = to_dict(r)
    d["raw"] = archive.read_excerpt(*ref, raw_chars) if ref else ""
    if bump:
        log_event("recall_detail", id=sid)
    return d


def _archive_ref(conn, sid: int):
    return conn.execute("SELECT archive_path, start_line, end_line, digest FROM star_archive_refs WHERE star_id=?",
                        (sid,)).fetchone()


def raw_excerpt(sid: int, limit: int) -> str:
    with get_conn() as conn:
        ref = _archive_ref(conn, sid)
    return archive.read_excerpt(*ref, limit) if ref else ""


def stats() -> dict:
    from . import core

    cfg = config.load_config()
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with get_conn() as c:
        total = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL").fetchone()[0]
        deleted = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NOT NULL").fetchone()[0]
        today = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL AND created_at>=?", (today0,)).fetchone()[0]
        pend = c.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
        rng = c.execute("SELECT MIN(created_at), MAX(created_at) FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL").fetchone()
        fused = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL AND fused_from!=''").fetchone()[0]
        absorbed = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NULL AND fused_into IS NOT NULL").fetchone()[0]
    hist = config.load_state().get("recall_tokens") or []
    ct = core.core_tokens()
    avg = int(sum(hist) / len(hist)) if hist else None
    budget = int(cfg["proxy"]["budget_tokens"])
    return {
        "total": total, "deleted": deleted, "today": today, "pending_proposals": pend,
        "core_tokens": ct, "recall_budget": budget, "avg_recall_tokens": avg,
        "est_tokens_per_turn": ct + (avg if avg is not None else budget),
        "from": rng[0], "to": rng[1], "fts": _db.FTS_OK, "jieba": _db.HAS_JIEBA,
        "fused": fused, "absorbed": absorbed,
        "semantic": semantic.enabled(cfg),
        "semantic_model": (semantic.active(cfg) or (None,))[0],
        "archive_files": sum(1 for _ in config.ARCHIVE_DIR.rglob("*") if _.is_file()) if config.ARCHIVE_DIR.exists() else 0,
    }


def backfill_embeddings(max_n: int = 500) -> int:
    cfg = config.load_config()
    n = 0
    while n < max_n:
        k = semantic.backfill_step(cfg, min(64, max_n - n))
        if not k:
            break
        n += k
    return n
