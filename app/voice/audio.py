"""音频小工具：PCM ↔ 浮点、WAV 封装、音高（F0）估计与男女判断。需要 numpy（插件依赖）。"""
from __future__ import annotations

import io
import struct
import wave


def np():
    import numpy  # 插件依赖，按需导入
    return numpy


def pcm16_to_float(data: bytes):
    n = np()
    if len(data) % 2:
        data = data[:-1]
    return n.frombuffer(data, dtype=n.int16).astype(n.float32) / 32768.0


def float_to_wav(x, sr: int = 16000) -> bytes:
    n = np()
    pcm = (n.clip(x, -1, 1) * 32767).astype(n.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_float(data: bytes):
    with wave.open(io.BytesIO(data)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError("只支持 16 位 WAV")
    x = pcm16_to_float(raw)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def resample(x, sr: int, target: int = 16000):
    if sr == target or len(x) == 0:
        return x
    n = np()
    m = int(round(len(x) * target / sr))
    return n.interp(n.linspace(0, len(x) - 1, m), n.arange(len(x)), x).astype(n.float32)


def rms(x) -> float:
    n = np()
    return float(n.sqrt(n.mean(n.square(x)))) if len(x) else 0.0


def estimate_f0(x, sr: int = 16000, fmin: float = 70.0, fmax: float = 400.0) -> float | None:
    """自相关法估计说话音高（中位数，Hz）。没有浊音就返回 None。"""
    n = np()
    if len(x) < sr * 0.2:
        return None
    win, hop = int(sr * 0.04), int(sr * 0.02)
    lo, hi = int(sr / fmax), int(sr / fmin)
    energy_floor = max(1e-4, rms(x) * 0.5)
    f0s = []
    for i in range(0, len(x) - win, hop):
        fr = x[i:i + win]
        if rms(fr) < energy_floor:
            continue
        fr = fr - fr.mean()
        ac = n.correlate(fr, fr, mode="full")[win - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        seg = ac[lo:hi]
        if not len(seg):
            continue
        k = int(n.argmax(seg))
        if seg[k] < 0.45:
            continue
        f0s.append(sr / (lo + k))
    if len(f0s) < 3:
        return None
    return float(n.median(f0s))


def gender_of(f0: float | None, split_hz: float = 165.0) -> str:
    if not f0:
        return ""
    return "男" if f0 < split_hz else "女"


def wav_header_ok(b: bytes) -> bool:
    return len(b) > 44 and b[:4] == b"RIFF" and b[8:12] == b"WAVE"


__all__ = ["pcm16_to_float", "float_to_wav", "wav_to_float", "resample", "rms", "estimate_f0", "gender_of", "struct"]
