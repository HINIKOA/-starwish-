"""语义检索（向量）：三档可选，和 jieba + FTS5 混合排序。

- small：内置 bge-small-zh-v1.5 int8 ONNX（常驻默认）
- m3：BGE-M3 int8 ONNX（一键部署）
- api：Qwen3-Embedding 等 OpenAI 兼容 /embeddings 接口（单独填 Key）

选的档位没准备好时自动退回内置 small；small 也不在（依赖没装 / 模型没下）就只用字面检索，什么都不会坏。
每颗星的向量按「模型键」分开存，切换档位不会丢掉旧向量，切回来立刻可用；新档位的向量在后台慢慢补齐。
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from . import config
from .embed import models as emodels

log = logging.getLogger("soulstar.semantic")

_engines: dict[str, object] = {}
_lock = threading.RLock()
_fail: dict[str, str] = {}          # 模型键 → 加载失败原因（重置后再试）
_bf_thread: threading.Thread | None = None
_bf_state: dict = {}
_api_hook = None                    # 测试用：替换 API 调用
_stop = threading.Event()


def _at_exit() -> None:
    _stop.set()
    if _bf_thread is not None and _bf_thread.is_alive():
        _bf_thread.join(10)


import atexit  # noqa: E402

atexit.register(_at_exit)


def reset() -> None:
    with _lock:
        _engines.clear()
        _fail.clear()


def ecfg_of(cfg: dict) -> dict:
    return cfg.get("embed") or {}


# ---------------- 引擎 ----------------
class OnnxEngine:
    def __init__(self, kind: str, threads: int = 2):
        import numpy as np  # noqa: F401
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = emodels.model_dir(kind)
        meta = emodels.LOCAL[kind]
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(meta["max_len"])
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads or 2))
        so.log_severity_level = 3
        self.sess = ort.InferenceSession(str(d / "model.onnx"), sess_options=so, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def encode(self, texts: list[str]):
        import numpy as np

        out = [None] * len(texts)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))   # 按长度分批，少补齐
        for s in range(0, len(order), 16):
            idx = order[s:s + 16]
            enc = self.tok.encode_batch([texts[i] or " " for i in idx])
            L = max(len(e.ids) for e in enc)
            ids = np.zeros((len(enc), L), dtype=np.int64)
            mask = np.zeros((len(enc), L), dtype=np.int64)
            for k, e in enumerate(enc):
                ids[k, :len(e.ids)] = e.ids
                mask[k, :len(e.ids)] = 1
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            h = self.sess.run(None, feed)[0][:, 0]                         # CLS
            h = h / np.maximum(np.linalg.norm(h, axis=1, keepdims=True), 1e-9)
            for k, i in enumerate(idx):
                out[i] = h[k].astype("float32")
        return out


class ApiEngine:
    """OpenAI 兼容 /embeddings：阿里云百炼 text-embedding-v4（Qwen3-Embedding）、硅基流动 Qwen/Qwen3-Embedding-8B 等。"""

    def __init__(self, e: dict):
        self.base = (e.get("api_base_url") or "").rstrip("/")
        self.key = e.get("api_key") or ""
        self.model = e.get("api_model") or "text-embedding-v4"
        self.dim = int(e.get("api_dimensions") or 0)
        self.batch = max(1, int(e.get("api_batch") or 10))
        self.timeout = float(e.get("api_timeout") or 30)
        self._dim_ok = True

    def _post(self, texts: list[str]) -> list[list[float]]:
        body = {"model": self.model, "input": texts, "encoding_format": "float"}
        if self.dim and self._dim_ok:
            body["dimensions"] = self.dim
        if _api_hook:
            return _api_hook(self.base, self.key, body)
        r = httpx.post(self.base + "/embeddings", json=body, timeout=self.timeout,
                       headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        if r.status_code == 400 and "dimension" in r.text.lower() and self._dim_ok:
            self._dim_ok = False            # 有的服务不认 dimensions，去掉再试一次
            return self._post(texts)
        if r.status_code >= 400:
            raise RuntimeError(f"向量接口 HTTP {r.status_code}: {r.text[:200]}")
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    def encode(self, texts: list[str]):
        import numpy as np

        out = []
        for s in range(0, len(texts), self.batch):
            for v in self._post([t[:6000] or " " for t in texts[s:s + self.batch]]):
                a = np.asarray(v, dtype="float32")
                out.append(a / max(float(np.linalg.norm(a)), 1e-9))
        return out


# ---------------- 档位选择 ----------------
def wanted(cfg: dict) -> str:
    if not cfg.get("memory", {}).get("semantic", True):
        return "off"
    return ecfg_of(cfg).get("provider") or "small"


def api_configured(e: dict) -> bool:
    return bool(e.get("api_base_url") and e.get("api_key") and e.get("api_model"))


def _local_ready(kind: str) -> bool:
    return emodels.installed(kind) and emodels.deps_ok()


def key_for(provider: str, e: dict) -> str:
    if provider == "api":
        return f"api:{e.get('api_model') or 'text-embedding-v4'}:{int(e.get('api_dimensions') or 0)}"
    return {"small": "small:bge-small-zh-v1.5", "m3": "m3:bge-m3"}[provider]


def active(cfg: dict) -> tuple[str, str, str] | None:
    """返回 (实际档位, 模型键, 说明)；不可用返回 None。"""
    w = wanted(cfg)
    if w == "off":
        return None
    e = ecfg_of(cfg)
    note = ""
    if w == "api":
        if api_configured(e) and key_for("api", e) not in _fail:
            return "api", key_for("api", e), ""
        note = "向量接口没填好" if not api_configured(e) else "向量接口出错：" + _fail.get(key_for("api", e), "")
    elif w == "m3":
        if _local_ready("m3") and key_for("m3", e) not in _fail:
            return "m3", key_for("m3", e), ""
        note = "BGE-M3 还没部署" if not emodels.installed("m3") else ("依赖没装" if not emodels.deps_ok() else "BGE-M3 加载失败")
    if _local_ready("small") and key_for("small", e) not in _fail:
        return "small", key_for("small", e), (note + "，暂用内置 small") if note else ""
    return None


def enabled(cfg: dict) -> bool:
    return active(cfg) is not None


def _engine(provider: str, key: str, cfg: dict):
    with _lock:
        if key in _engines:
            return _engines[key]
        try:
            e = ecfg_of(cfg)
            eng = ApiEngine(e) if provider == "api" else OnnxEngine(provider, e.get("threads", 2))
            _engines[key] = eng
            return eng
        except Exception as ex:  # noqa: BLE001
            _fail[key] = str(ex)[:200]
            log.warning("向量模型 %s 加载失败：%s", key, ex)
            return None


def embed(texts: list[str], cfg: dict, act=None):
    act = act or active(cfg)
    if not act:
        return None
    eng = _engine(act[0], act[1], cfg)
    if eng is None:
        return None
    return eng.encode(texts)


def min_sim(cfg: dict, provider: str) -> float:
    v = float(ecfg_of(cfg).get("min_sim") or 0)
    if v > 0:
        return v
    return emodels.LOCAL[provider]["min_sim"] if provider in emodels.LOCAL else 0.35


# ---------------- 存与查 ----------------
def store(conn, star_id: int, text: str, cfg: dict) -> None:
    """新星写入时顺手算向量；API 档不在写入时联网（交给后台补齐，不拖慢存星）。"""
    act = active(cfg)
    if not act:
        return
    if act[0] == "api":
        start_backfill()
        return
    vecs = embed([text], cfg, act)
    if vecs:
        conn.execute("INSERT OR REPLACE INTO embeddings(star_id, model, vec) VALUES (?,?,?)", (star_id, act[1], vecs[0].tobytes()))


def forget(conn, star_id: int) -> None:
    """星的内容改了：删掉所有旧向量，后台重算。"""
    conn.execute("DELETE FROM embeddings WHERE star_id=?", (star_id,))


def search(conn, query: str, cfg: dict, limit: int = 100) -> dict[int, float]:
    """返回 {star_id: 原始余弦相似度}（已按门槛过滤）。"""
    act = active(cfg)
    if not act or not query.strip():
        return {}
    import numpy as np

    rows = conn.execute(
        "SELECT e.star_id, e.vec FROM embeddings e JOIN stars s ON s.id=e.star_id"
        " WHERE s.deleted_at IS NULL AND s.fused_into IS NULL AND e.model=?", (act[1],)).fetchall()
    if not rows:
        return {}
    try:
        q = embed([query], cfg, act)
    except Exception as ex:  # noqa: BLE001  接口挂了：这一次只用字面检索
        log.warning("查询向量失败：%s", ex)
        return {}
    if not q:
        return {}
    ids = [r[0] for r in rows]
    mat = np.vstack([np.frombuffer(r[1], dtype="float32") for r in rows])
    if mat.shape[1] != q[0].shape[0]:
        return {}
    sims = mat @ q[0]
    order = np.argsort(-sims)[:limit]
    floor = max(min_sim(cfg, act[0]), float(sims[order[0]]) - 0.12)   # 既要过门槛，也不能比最像的那颗差太多
    return {ids[i]: float(sims[i]) for i in order if sims[i] >= floor}


def relevance(sims: dict[int, float], cfg: dict) -> dict[int, float]:
    """把相似度换成和字面检索同一量纲的相关度（0~weight）：既看绝对值，也看和最好那颗差多少。"""
    if not sims:
        return {}
    act = active(cfg)
    floor = min_sim(cfg, act[0] if act else "small")
    w = float(ecfg_of(cfg).get("weight") or 0.9)
    top = max(sims.values())
    out = {}
    for sid, s in sims.items():
        absolute = min(1.0, (s - floor) / 0.2)
        rel = 1.0 - min(1.0, (top - s) / 0.15)
        v = w * (0.55 * absolute + 0.45 * rel)
        if v > 0.05:
            out[sid] = v
    return out


def pair_similarity(conn, a: int, b: int, cfg: dict | None = None):
    cfg = cfg or config.load_config()
    act = active(cfg)
    if not act:
        return None
    import numpy as np

    rows = {r[0]: r[1] for r in conn.execute("SELECT star_id, vec FROM embeddings WHERE model=? AND star_id IN (?,?)", (act[1], a, b))}
    if len(rows) < 2:
        return None
    va, vb = (np.frombuffer(rows[x], dtype="float32") for x in (a, b))
    return float(va @ vb) if va.shape == vb.shape else None


# ---------------- 后台补齐 ----------------
def coverage(cfg: dict | None = None) -> dict:
    from .db import get_conn

    cfg = cfg or config.load_config()
    act = active(cfg)
    with get_conn() as c:
        total = c.execute("SELECT COUNT(*) FROM stars WHERE deleted_at IS NULL AND fused_into IS NULL").fetchone()[0]
        done = c.execute("SELECT COUNT(*) FROM embeddings e JOIN stars s ON s.id=e.star_id WHERE s.deleted_at IS NULL"
                         " AND s.fused_into IS NULL AND e.model=?", (act[1] if act else "",)).fetchone()[0]
    return {"total": total, "done": done}


def backfill_step(cfg: dict, max_n: int = 64) -> int:
    from .db import get_conn

    act = active(cfg)
    if not act:
        return 0
    with get_conn() as c:
        rows = c.execute(
            "SELECT s.id, s.summary, s.content FROM stars s WHERE s.deleted_at IS NULL AND s.fused_into IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.star_id=s.id AND e.model=?) ORDER BY s.id DESC LIMIT ?",
            (act[1], max_n)).fetchall()
    if not rows:
        return 0
    vecs = embed([f"{r['summary']}\n{r['content']}" for r in rows], cfg, act)   # 联网 / 计算时不占着数据库
    if not vecs:
        return 0
    with get_conn() as c:
        c.executemany("INSERT OR REPLACE INTO embeddings(star_id, model, vec) VALUES (?,?,?)",
                      [(r["id"], act[1], v.tobytes()) for r, v in zip(rows, vecs)])
    return len(rows)


def _backfill_loop() -> None:
    _bf_state.update(state="running", t0=time.time(), n=0, error="")
    try:
        while not _stop.is_set():
            cfg = config.load_config()
            try:
                n = backfill_step(cfg)
            except Exception as ex:  # noqa: BLE001
                act = active(cfg)
                if act and act[0] == "api":
                    _fail[act[1]] = str(ex)[:200]        # 接口坏了 → 暂时退回内置
                _bf_state.update(error=str(ex)[:200])
                n = 0
            if not n:
                break
            _bf_state["n"] = _bf_state.get("n", 0) + n
        _bf_state.update(state="idle", t1=time.time())
    except Exception as ex:  # noqa: BLE001
        _bf_state.update(state="error", error=str(ex)[:200])


def start_backfill(wait: bool = False) -> None:
    global _bf_thread
    with _lock:
        running = _bf_thread is not None and _bf_thread.is_alive()
        if not running:
            _bf_thread = threading.Thread(target=_backfill_loop, daemon=True, name="embed-backfill")
            _bf_thread.start()
        th = _bf_thread
    if wait:                       # 在锁外等，补齐线程加载模型时也要用这把锁
        th.join(600)
        if running:                # 等的是一轮旧的补齐：再补一轮，确保用的是现在的档位
            start_backfill(wait=True)


def status(cfg: dict | None = None) -> dict:
    cfg = cfg or config.load_config()
    e = ecfg_of(cfg)
    act = active(cfg)
    return {
        "wanted": wanted(cfg), "active": act[0] if act else None, "key": act[1] if act else None,
        "note": act[2] if act else ("语义检索已关闭" if wanted(cfg) == "off" else "向量模型还没准备好，暂时只用字面检索"),
        "deps": emodels.dep_status(), "deps_ok": emodels.deps_ok(), "deps_mb": emodels.DEPS_SIZE_MB,
        "local": {k: emodels.local_status(k) for k in emodels.LOCAL},
        "api": {"configured": api_configured(e), "model": e.get("api_model"), "dimensions": e.get("api_dimensions"),
                "error": _fail.get(key_for("api", e), "")},
        "coverage": coverage(cfg), "backfill": dict(_bf_state), "jobs": emodels.jobs(),
    }


def test_api(cfg: dict) -> dict:
    e = ecfg_of(cfg)
    if not api_configured(e):
        raise ValueError("请先填写向量接口地址、Key 和模型")
    t = time.time()
    v = ApiEngine(e).encode(["主人冬天总穿那件黑色羽绒服", "保暖外套"])
    _fail.pop(key_for("api", e), None)
    return {"ok": True, "dim": int(v[0].shape[0]), "ms": int((time.time() - t) * 1000), "sim": round(float(v[0] @ v[1]), 3)}
