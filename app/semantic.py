"""可选语义检索：仅当已安装 sentence-transformers 且配置开启时启用，否则静默退化为 FTS + bigram。"""
from __future__ import annotations

import threading

_model = None
_model_name = None
_lock = threading.Lock()
_failed = False


def available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("sentence_transformers") is not None and importlib.util.find_spec("numpy") is not None
    except Exception:
        return False


def _get_model(name: str):
    global _model, _model_name, _failed
    if _failed:
        return None
    with _lock:
        if _model is not None and _model_name == name:
            return _model
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            _model = SentenceTransformer(name)
            _model_name = name
            return _model
        except Exception:
            _failed = True
            return None


def enabled(cfg: dict) -> bool:
    return bool(cfg.get("memory", {}).get("semantic")) and available() and not _failed


def embed(texts: list[str], cfg: dict):
    m = _get_model(cfg["memory"]["semantic_model"])
    if m is None:
        return None
    return m.encode(texts, normalize_embeddings=True)


def store(conn, star_id: int, text: str, cfg: dict) -> None:
    if not enabled(cfg):
        return
    vecs = embed([text], cfg)
    if vecs is None:
        return
    import numpy as np

    conn.execute(
        "INSERT OR REPLACE INTO embeddings(star_id, model, vec) VALUES (?,?,?)",
        (star_id, cfg["memory"]["semantic_model"], np.asarray(vecs[0], dtype="float32").tobytes()),
    )


def search(conn, query: str, cfg: dict, limit: int = 100) -> dict[int, float]:
    """返回 {star_id: 余弦相似度}；不可用时返回空字典。"""
    if not enabled(cfg):
        return {}
    q = embed([query], cfg)
    if q is None:
        return {}
    import numpy as np

    rows = conn.execute(
        "SELECT e.star_id, e.vec FROM embeddings e JOIN stars s ON s.id=e.star_id WHERE s.deleted_at IS NULL AND s.fused_into IS NULL AND e.model=?",
        (cfg["memory"]["semantic_model"],),
    ).fetchall()
    if not rows:
        return {}
    ids = [r[0] for r in rows]
    mat = np.vstack([np.frombuffer(r[1], dtype="float32") for r in rows])
    sims = mat @ np.asarray(q[0], dtype="float32")
    order = np.argsort(-sims)[:limit]
    return {ids[i]: float(sims[i]) for i in order if sims[i] > 0.35}
