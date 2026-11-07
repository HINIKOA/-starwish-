"""SQLite 存储 + 中文分词（jieba，缺失时退化为二元组 bigram）+ 追加式事件日志。"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from . import config

_init_lock = threading.Lock()
_initialized_for: str | None = None
FTS_OK = True

try:  # jieba 可选
    import jieba  # type: ignore

    jieba.setLogLevel(logging.WARNING)
    HAS_JIEBA = True
except Exception:  # pragma: no cover
    jieba = None
    HAS_JIEBA = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS stars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL,
    last_recalled_at REAL,
    recall_count INTEGER NOT NULL DEFAULT 0,
    importance INTEGER NOT NULL DEFAULT 3,
    boost REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT '',
    session TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL DEFAULT '',
    deleted_at REAL,
    tok TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_stars_created ON stars(created_at);
CREATE INDEX IF NOT EXISTS idx_stars_hash ON stars(hash);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'append',
    content TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS embeddings (
    star_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    vec BLOB NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    global _initialized_for, FTS_OK
    with _init_lock:
        key = str(config.DB_PATH)
        if _initialized_for == key and config.DB_PATH.exists():
            return
        config.ensure_dirs()
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS stars_fts USING fts5(tok, tokenize='unicode61')"
                )
                FTS_OK = True
            except sqlite3.OperationalError:
                FTS_OK = False
            conn.commit()
        finally:
            conn.close()
        _initialized_for = key


@contextmanager
def get_conn():
    init_db()
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------- 分词 ----------------
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_PUNCT = re.compile(r"^[\W_]+$", re.UNICODE)


def bigrams(text: str) -> list[str]:
    out = []
    for run in _CJK.findall(text or ""):
        if len(run) == 1:
            continue
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


STOP_CHARS = set("的了是在我你他她它和与也就都而及着啊吗呢吧呀哦嗯这那有个一不人们之其为以于上下")


def unigrams(text: str) -> list[str]:
    return [ch for run in _CJK.findall(text or "") for ch in run if ch not in STOP_CHARS]


def tokenize(text: str, with_unigrams: bool = False) -> list[str]:
    """索引用：jieba 搜索模式分词 + CJK 二元组(+单字) + 英文/数字词，全部小写。"""
    text = text or ""
    toks: list[str] = []
    if HAS_JIEBA:
        for t in jieba.cut_for_search(text):
            t = t.strip().lower()
            if not t or _PUNCT.match(t):
                continue
            if _CJK.fullmatch(t) and len(t) < 2:
                continue
            toks.append(t)
    toks.extend(bigrams(text))
    toks.extend(w.lower() for w in _WORD.findall(text))
    if with_unigrams:
        toks.extend(unigrams(text))
    return toks


def tok_string(text: str) -> str:
    return " ".join(tokenize(text, with_unigrams=True))


def query_terms(query: str) -> list[str]:
    seen, out = set(), []
    for t in tokenize(query):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:64]


def fts_query(query: str, unigram: bool = False) -> str:
    terms = list(dict.fromkeys(unigrams(query)))[:32] if unigram else query_terms(query)
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def jaccard(a: str, b: str) -> float:
    sa, sb = set(bigrams(a)) | set(_WORD.findall((a or "").lower())), set(bigrams(b)) | set(_WORD.findall((b or "").lower()))
    if not sa or not sb:
        return 1.0 if (a or "").strip() == (b or "").strip() else 0.0
    return len(sa & sb) / len(sa | sb)


def estimate_tokens(text: str) -> int:
    """粗估 token：汉字约 1 个/字，其他约 3.5 字符/个（偏保守）。"""
    if not text:
        return 0
    cjk = sum(len(r) for r in _CJK.findall(text))
    other = len(text) - cjk
    return int(cjk + other / 3.5 + 0.999)


# ---------------- 事件日志（只追加，永久可追溯） ----------------
_ev_lock = threading.Lock()


def log_event(etype: str, **data) -> None:
    config.ensure_dirs()
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "t": time.time(), "type": etype}
    rec.update(data)
    line = json.dumps(rec, ensure_ascii=False)
    with _ev_lock:
        with open(config.EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_events(limit: int = 100) -> list[dict]:
    if not config.EVENTS_PATH.exists():
        return []
    with open(config.EVENTS_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()[-limit:]
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out
