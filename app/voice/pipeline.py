"""宇宙之声 · 流水线：浏览器每隔几秒送来一段 16kHz PCM → 分人 → 比对声纹 → （可选）逐句转写 → 记进画面日记 → 认名字。"""
from __future__ import annotations

import threading
import time

import httpx

from .. import config, maintenance
from ..db import log_event
from . import audio, backend, naming
from .registry import Registry, VoiceError

_locks: dict[str, threading.Lock] = {}
_glock = threading.Lock()
_identifying: set[str] = set()


def vcfg_of(cfg: dict) -> dict:
    return cfg.get("voice") or {}


def _slock(sid: str) -> threading.Lock:
    with _glock:
        return _locks.setdefault(sid, threading.Lock())


def stt_ready(cfg: dict) -> bool:
    e = cfg.get("eye") or {}
    base = e.get("stt_base_url") or cfg["llm"].get("base_url")
    key = e.get("stt_api_key") or cfg["llm"].get("api_key")
    return bool(base and key)


def transcribe(cfg: dict, wav: bytes) -> str:
    e = cfg.get("eye") or {}
    base = (e.get("stt_base_url") or cfg["llm"].get("base_url") or "").rstrip("/")
    key = e.get("stt_api_key") or cfg["llm"].get("api_key") or ""
    r = httpx.post(base + "/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                   data={"model": e.get("stt_model") or "whisper-1", "language": "zh"},
                   files={"file": ("seg.wav", wav, "audio/wav")}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"转写服务出错：HTTP {r.status_code}")
    return (r.json().get("text") or "").strip()


def registry(sid: str, cfg: dict | None = None) -> Registry:
    from ..eye import session as es
    cfg = cfg or config.load_config()
    meta = es.get(sid, with_entries=False)
    return Registry.load(sid, meta.get("title", ""), use_library=vcfg_of(cfg).get("use_library", True))


def _merge_runs(segs: list[dict], gap: float = 0.6) -> list[dict]:
    out: list[dict] = []
    for s in sorted(segs, key=lambda x: x["start"]):
        if out and out[-1]["spk"] == s["spk"] and s["start"] - out[-1]["end"] <= gap:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append(dict(s))
    return out


def process_chunk(cfg: dict, sid: str, pcm: bytes, sr: int = 16000, t0: float | None = None,
                  transcribe_fn=None) -> dict:
    """处理一段音频。返回本段每句话的说话人（和台词），以及最新的说话人表。"""
    from ..eye import session as es
    v = vcfg_of(cfg)
    meta = es.get(sid, with_entries=False)
    if meta.get("ended_at"):
        raise VoiceError("这次观看已经结束")
    x = audio.pcm16_to_float(pcm)
    if sr != 16000:
        x = audio.resample(x, sr, 16000)
        sr = 16000
    dur = len(x) / sr
    t0 = float(t0) if t0 else time.time() - dur
    if dur > 60:
        raise VoiceError("一段音频最长 60 秒")
    be = backend.get(v)
    t_start = time.time()
    res = be.process(x, sr)
    proc_ms = int((time.time() - t_start) * 1000)
    with _slock(sid):
        reg = registry(sid, cfg)
        mapping, scores, created = {}, {}, []
        for loc, info in res["locals"].items():
            spk, score, new = reg.match(info["emb"], info["seconds"], info["f0"], v, t=t0)
            mapping[loc], scores[loc] = spk["id"], score
            if new:
                created.append(spk["label"])
        segs = [{"start": s["start"], "end": s["end"], "spk": mapping[s["local"]]} for s in res["segments"] if s["local"] in mapping]
        runs = _merge_runs(segs)
        do_stt = v.get("transcribe", True) and (cfg.get("eye") or {}).get("stt_mode") == "api" and (transcribe_fn or stt_ready(cfg))
        lines, names = [], []
        for r in runs:
            d = r["end"] - r["start"]
            reg.add_timeline(r["spk"], t0 + r["start"], d)
            text = ""
            if do_stt and d >= 0.6:
                wav = audio.float_to_wav(x[int(max(0, r["start"] - 0.15) * sr): int(min(dur, r["end"] + 0.15) * sr)], sr)
                try:
                    text = (transcribe_fn or (lambda w: transcribe(cfg, w)))(wav)
                except Exception as ex:  # noqa: BLE001
                    log_event("voice_stt_failed", sid=sid, error=str(ex)[:120])
            spk = reg.resolve(r["spk"])
            line = {"start": round(t0 + r["start"], 2), "end": round(t0 + r["end"], 2), "speaker": spk["id"], "label": spk["label"],
                    "display": reg.display(spk), "color": spk["color"], "text": text}
            if text:
                es.add(sid, "subtitle", text, speaker=spk["id"], t_audio=line["start"])
                reg.d["lines_since_identify"] = reg.d.get("lines_since_identify", 0) + 1
                n = naming.self_intro(text)
                if n:
                    ch = reg.propose(spk["id"], n, "self_intro", f"自己说「{text[:24]}」", v)
                    if ch:
                        names.append(ch)
            lines.append(line)
        reg.d["chunks"] = reg.d.get("chunks", 0) + 1
        merges = []
        every = int(v.get("recluster_every", 4))
        if every and reg.d["chunks"] % every == 0:
            merges = reg.recluster(v)
        reg.save()
        want_identify = (int(v.get("auto_identify_every", 20)) > 0
                         and reg.d.get("lines_since_identify", 0) >= int(v.get("auto_identify_every", 20))
                         and maintenance.llm_ready(cfg))
        out = {"lines": lines, "new_speakers": created, "names": names, "merges": merges, "proc_ms": proc_ms,
               "audio_sec": round(dur, 2), "roster": reg.public()}
    if want_identify:
        identify_async(cfg, sid)
    return out


# ---------------- 台词线索：LLM 推断名字 ----------------
def dialogue_lines(sid: str, reg: Registry) -> list[str]:
    from ..eye import session as es
    d = es.get(sid)
    rows = []
    for e in d["entries"]:
        if e["kind"] == "subtitle" and e.get("speaker"):
            s = reg.resolve(e["speaker"])
            rows.append(f"[{s['label'] if s else '?'}] {e['text']}")
    return rows


def identify(cfg: dict, sid: str, chat=None) -> list[dict]:
    v = vcfg_of(cfg)
    with _slock(sid):
        reg = registry(sid, cfg)
        lines = dialogue_lines(sid, reg)
        roster = reg.roster_text()
        title = reg.d.get("title", "")
    if not lines:
        return []
    msgs = naming.identify_messages(title, roster, lines)
    text = (chat or (lambda m: maintenance.chat(m, cfg)))(msgs)
    props = naming.parse_identify(text)
    changes = []
    with _slock(sid):
        reg = registry(sid, cfg)
        for p in props:
            ch = reg.propose(p["speaker"], p["name"], "dialogue", p["evidence"], v)
            if ch:
                changes.append(ch)
        reg.d["lines_since_identify"] = 0
        reg.save()
    log_event("voice_identify", sid=sid, proposals=len(props), changes=len(changes))
    return changes


def identify_async(cfg: dict, sid: str) -> None:
    if sid in _identifying:
        return
    _identifying.add(sid)

    def run():
        try:
            identify(cfg, sid)
        except Exception as ex:  # noqa: BLE001
            log_event("voice_identify_failed", sid=sid, error=str(ex)[:160])
        finally:
            _identifying.discard(sid)
    threading.Thread(target=run, daemon=True).start()


# ---------------- 画面线索：提问时告诉视觉模型谁在说话，回答里带【认人】 ----------------
def active(sid: str) -> bool:
    return Registry.exists(sid)


def prompt_rule(sid: str) -> str:
    if not Registry.exists(sid):
        return ""
    reg = Registry.load(sid)
    if not reg.active():
        return ""
    rec = naming.recent_line(reg.recent_speakers(40), reg.display)
    return naming.VISUAL_RULE.format(roster=reg.roster_text(), recent=rec)


def apply_visual_tags(cfg: dict, sid: str, items: list[dict]) -> list[dict]:
    if not items or not Registry.exists(sid):
        return []
    v = vcfg_of(cfg)
    changes = []
    with _slock(sid):
        reg = registry(sid, cfg)
        for it in items:
            ch = reg.propose(it["label"], it["name"], "visual", it.get("note") or "画面", v)
            if ch:
                changes.append(ch)
        reg.save()
    return changes


def display_name(sid: str, spk_id: str) -> str:
    try:
        return Registry.load(sid).display(spk_id)
    except Exception:
        return spk_id


def cast_text(sid: str) -> str:
    if not Registry.exists(sid):
        return ""
    reg = Registry.load(sid)
    named = [s for s in reg.active() if s.get("name_status") == "confirmed" and not s.get("owner")]
    return "、".join(f"{s['name']}（{s['label']}）" for s in named)


def end_session(cfg: dict, sid: str) -> dict:
    if not Registry.exists(sid):
        return {"saved": 0, "cast": ""}
    with _slock(sid):
        reg = Registry.load(sid)
        if vcfg_of(cfg).get("recluster_on_end", True):
            reg.recluster(vcfg_of(cfg))
            reg.save()
        saved = reg.save_to_library() if vcfg_of(cfg).get("use_library", True) else 0
    return {"saved": saved, "cast": cast_text(sid)}


def enroll_owner(cfg: dict, pcm: bytes, sr: int = 16000) -> dict:
    from .registry import set_owner
    x = audio.pcm16_to_float(pcm)
    if sr != 16000:
        x = audio.resample(x, sr, 16000)
    secs = len(x) / 16000
    if secs < 3:
        raise VoiceError("录音太短，至少说 3 秒")
    if audio.rms(x) < 0.003:
        raise VoiceError("几乎没有声音，检查一下麦克风")
    emb = backend.get(vcfg_of(cfg)).embed(x, 16000)
    return set_owner(emb, secs)
