"""Evidence-gated candidate retrieval for model-facing memory recall."""
from __future__ import annotations

import math
import re

from . import db, semantic

_TITLE = re.compile(r"《\s*([^《》]{1,60}?)\s*》")
_CJK = re.compile(r"[\u3400-\u9fff]+")
_WORDS = re.compile(r"[a-z0-9]+")
_TERMS = re.compile(r"[\u3400-\u9fff]{2,}|[a-z0-9]{2,}")
_FILLER = re.compile(
    r"改(?:规则|版)[前后]|什么时候|是什么意思|怎么回事|原来的|现在的|旧版本?|新版本?"
    r"|请问|主人|用户|什么|怎么|如何|为何|哪些|哪里|何时|是否|多少|当前|最新|原先|原来|以前|之前|现在"
    r"|[吗呢吧呀]|\b(?:what|when|where|who|how|is|are|the|of|and|for|please)\b", re.I
)
_GENERAL = {"规则", "约定", "记录", "情况", "设定", "含义", "内容", "要求", "意思", "是在", "是指", "一个", "它的", "里的"}
_VECTOR_MARGIN = 0.05


def work_titles(text: str) -> set[str]:
    return {value.strip().casefold() for value in _TITLE.findall(text) if value.strip()}


def _focus(query: str) -> str:
    return " ".join(_FILLER.sub(" ", _TITLE.sub(" ", query.casefold())).strip(" ？?。！!：:").split())


def _terms(focus: str) -> set[str]:
    return {term for term in db.query_terms(focus) if _TERMS.fullmatch(term) and term not in _GENERAL}


def _evidence(row, focus: str, terms: set[str], titles: set[str]) -> float:
    doc = " ".join((row["content"], row["summary"], row["tags"] or "")).casefold()
    doc_titles = work_titles(doc)
    if titles and doc_titles and not (titles & doc_titles):
        return 0.0
    literal = " ".join(doc.split())
    if focus and focus in literal:
        if _CJK.search(focus):
            if len(focus) > 1 or focus not in db.STOP_CHARS:
                return 1.0
        elif re.search(r"(?<![a-z0-9])" + re.escape(focus) + r"(?![a-z0-9])", literal):
            return 1.0
    if not focus:
        return 1.0 if titles & doc_titles else 0.0

    # Titles establish scope, not evidence that the requested detail exists.
    subject = _TITLE.sub(" ", doc)
    latin = set(_WORDS.findall(subject))
    matched = {term for term in terms if term in (latin if term.isascii() else subject)}
    ratio = len(matched) / len(terms) if terms else 0.0
    if ratio >= 0.5 or len(matched) >= 2:
        return 0.77 + 0.23 * ratio
    if titles & doc_titles:
        # With a precise work, a short focus such as the rope in a particular story can use one character.
        if matched or (len(focus) == 2 and _CJK.fullmatch(focus)
                       and any(ch not in db.STOP_CHARS and ch in subject for ch in focus)):
            return 0.79
    if any(len(term) >= (4 if term.isascii() else 3) for term in matched):
        return 0.78
    if any(run[i:i + 3] in subject for run in _CJK.findall(focus)
           for i in range(len(run) - 2) if not any(part in _GENERAL for part in (run[i:i + 2], run[i + 1:i + 3]))):
        return 0.78
    return 0.0


def _lexical_rows(conn, focus: str, terms: set[str], titles: set[str], limit: int):
    search_text = " ".join((focus, *sorted(titles)))
    q = db.fts_query(search_text) or (
        db.fts_query(search_text, unigram=True) if len(search_text.strip()) == 1 else ""
    )
    if q and db.FTS_OK:
        return conn.execute(
            "SELECT s.id,s.content,s.summary,s.tags,bm25(stars_fts) AS distance FROM stars_fts f "
            "JOIN stars s ON s.id=f.rowid WHERE stars_fts MATCH ? AND s.deleted_at IS NULL AND s.fused_into IS NULL "
            "ORDER BY distance,s.id LIMIT ?", (q, limit)).fetchall()
    needles = list(dict.fromkeys([focus, *sorted(titles), *sorted(terms, key=lambda t: (-len(t), t))]))[:12]
    needles = [t for t in needles if t and (_CJK.search(t) or _WORDS.search(t))]
    if not needles:
        return []
    clauses = " OR ".join("(instr(lower(s.content),?)>0 OR instr(lower(s.summary),?)>0)" for _ in needles)
    return conn.execute(
        "SELECT s.id,s.content,s.summary,s.tags,0 AS distance FROM stars s WHERE s.deleted_at IS NULL "
        f"AND s.fused_into IS NULL AND ({clauses}) ORDER BY s.id LIMIT ?",
        (*[t for t in needles for _ in range(2)], limit)).fetchall()


def candidates(conn, query: str, cfg: dict, limit: int = 200) -> dict[int, float]:
    focus, titles = _focus(query), work_titles(query)
    if not titles and not (_CJK.search(focus) or _WORDS.search(focus)):
        return {}
    terms = _terms(focus)
    eligible = [(r, score) for r in _lexical_rows(conn, focus, terms, titles, limit)
                if (score := _evidence(r, focus, terms, titles)) > 0]
    best = min((r["distance"] for r, _ in eligible), default=0.0)
    # BM25 only orders evidence-backed hits; document length should not dominate close matches.
    scores = {r["id"]: strength * (0.94 + 0.06 * (r["distance"] / best if best < 0 else 1.0))
              for r, strength in eligible}
    try:
        sims = semantic.search(conn, query, cfg)
        if sims:
            active = semantic.active(cfg)
            floor = semantic.min_sim(cfg, active[0] if active else "small")
            sims = {sid: sim for sid, sim in sims.items() if math.isfinite(sim)
                    and (sid in scores or sim >= min(1.0, floor + _VECTOR_MARGIN))}
            for sid, score in semantic.relevance(sims, cfg).items():
                scores[sid] = max(scores.get(sid, 0.0), score)
    except Exception:
        pass
    return scores
