"""声纹后端：把一段 16kHz 单声道音频切成「谁在什么时候说话」，并给每个本段说话人一个声纹向量。

- sherpa：sherpa-onnx（pyannote 分割 + CAM++ 声纹 + 快速聚类），纯 CPU、无需 PyTorch / ffmpeg。
- fake：测试用，按纯音频率当作「声纹」，不需要任何模型。
"""
from __future__ import annotations

import os
import threading

from . import models
from .audio import estimate_f0, np, rms

_lock = threading.RLock()
_cache: dict = {}


class BackendError(RuntimeError):
    pass


def name(vcfg: dict) -> str:
    return os.environ.get("SOULSTAR_VOICE_BACKEND") or vcfg.get("backend") or "sherpa"


def get(vcfg: dict):
    n = name(vcfg)
    if n == "fake":
        return FakeBackend()
    if n == "sherpa":
        return SherpaBackend(vcfg)
    raise BackendError(f"未知声纹后端：{n}")


def _locals_from(segs: list[dict], x, sr: int, embed, max_sec: float = 12.0) -> dict:
    """把同一本段说话人的片段拼起来（最多 max_sec 秒）算声纹和音高。"""
    n = np()
    out = {}
    for loc in sorted({s["local"] for s in segs}):
        parts, total = [], 0.0
        mine = sorted((s for s in segs if s["local"] == loc), key=lambda s: s["end"] - s["start"], reverse=True)
        for s in mine:
            a, b = int(s["start"] * sr), int(s["end"] * sr)
            if b - a < sr * 0.3:
                continue
            parts.append(x[a:b])
            total += (b - a) / sr
            if total >= max_sec:
                break
        secs = sum(s["end"] - s["start"] for s in mine)
        if not parts:
            continue
        y = n.concatenate(parts)
        out[loc] = {"emb": [float(v) for v in embed(y, sr)], "f0": estimate_f0(y, sr), "seconds": round(secs, 2)}
    return out


class SherpaBackend:
    def __init__(self, vcfg: dict):
        self.vcfg = vcfg
        if not models.ready("sherpa"):
            raise BackendError("宇宙之声还没装好：先在设置里一键下载声纹插件")

    def _objs(self):
        import sherpa_onnx
        thr = float(self.vcfg.get("local_cluster_threshold", 0.5))
        key = ("sherpa", thr, str(models.model_path("embedding")), str(models.model_path("segmentation")))
        with _lock:
            if _cache.get("key") != key:
                threads = max(1, min(4, (os.cpu_count() or 2) // 2))
                emb_cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(models.model_path("embedding")), num_threads=threads)
                cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(models.model_path("segmentation"))),
                        num_threads=threads),
                    embedding=emb_cfg,
                    clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=thr),
                    min_duration_on=0.3, min_duration_off=0.5)
                if not cfg.validate():
                    raise BackendError("声纹模型文件不完整，请在设置里重新下载")
                _cache.update(key=key, sd=sherpa_onnx.OfflineSpeakerDiarization(cfg),
                              ex=sherpa_onnx.SpeakerEmbeddingExtractor(emb_cfg))
            return _cache["sd"], _cache["ex"]

    def embed(self, x, sr: int = 16000) -> list[float]:
        _, ex = self._objs()
        with _lock:
            st = ex.create_stream()
            st.accept_waveform(sr, x)
            st.input_finished()
            return list(ex.compute(st))

    def process(self, x, sr: int = 16000) -> dict:
        if sr != 16000:
            raise BackendError("需要 16kHz 音频")
        if len(x) < sr * 0.8 or rms(x) < 0.003:
            return {"segments": [], "locals": {}}
        sd, _ = self._objs()
        with _lock:
            res = sd.process(x).sort_by_start_time()
        segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "local": int(s.speaker)} for s in res]
        return {"segments": segs, "locals": _locals_from(segs, x, sr, self.embed)}


class FakeBackend:
    """测试用：每个说话人是一个纯音（频率即身份），静音隔开句子。"""
    DIM = 64

    def _freq(self, y, sr):
        n = np()
        spec = n.abs(n.fft.rfft(y * n.hanning(len(y))))
        f = n.fft.rfftfreq(len(y), 1 / sr)
        spec[f < 50] = 0
        return float(f[int(n.argmax(spec))])

    def embed(self, y, sr: int = 16000) -> list[float]:
        import math
        fr = self._freq(y, sr)
        c = fr / 10.0
        return [math.exp(-((i - c) ** 2) / 8.0) for i in range(self.DIM)]

    def process(self, x, sr: int = 16000) -> dict:
        n = np()
        hop = int(sr * 0.05)
        voiced = [rms(x[i:i + hop]) > 0.01 for i in range(0, len(x) - hop + 1, hop)]
        runs, start, gap = [], None, 0
        for i, v in enumerate(voiced + [False] * 5):
            if v:
                if start is None:
                    start = i
                gap = 0
            elif start is not None:
                gap += 1
                if gap > 4:
                    runs.append((start, i - gap + 1))
                    start, gap = None, 0
        segs, buckets = [], {}
        for a, b in runs:
            s, e = a * hop / sr, b * hop / sr
            if e - s < 0.3:
                continue
            fr = self._freq(x[a * hop:b * hop], sr)
            key = next((k for k in buckets if abs(k - fr) < 15), None)
            if key is None:
                key = fr
                buckets[key] = len(buckets)
            segs.append({"start": round(s, 2), "end": round(e, 2), "local": buckets[key]})
        _ = n
        return {"segments": segs, "locals": _locals_from(segs, x, sr, self.embed)}
