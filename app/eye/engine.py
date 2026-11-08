"""宇宙之眼 · 引擎：把「最近几帧 + 文字历史」发给识图模型，并把流式回答一段段吐出来。

两个引擎：
- frame     通用帧模式：任意 OpenAI 兼容（/chat/completions）或 Anthropic 兼容（/v1/messages）识图模型。
- qwen_omni 千问 Qwen3.8-Omni-Flash 视频接口（预留）：同样支持两种协议；OpenAI 协议下帧序列按「视频」传，
            服务端不认时自动退回「多张图片」。可选让模型直接出语音（qwen_audio_out，未实测）。

图片只放进「这一次」请求，永远不进历史 —— 历史只留文字，前缀缓存一直有效，看两小时也不会越看越贵。
"""
from __future__ import annotations

import base64
import json
import struct
import time
import zlib
from typing import AsyncIterator

import httpx

from .. import config

ENGINES = {"frame": "通用帧模式", "qwen_omni": "千问 Omni 视频接口"}
PROTOCOLS = ("openai", "anthropic", "responses")
MIN_VIDEO_FRAMES = 4  # 千问视频输入至少要 4 帧；不足时按多图传


class EyeConfigError(ValueError):
    pass


def resolve(cfg: dict | None = None, engine: str | None = None) -> dict:
    """算出这次要用的协议、地址、Key、模型。"""
    cfg = cfg or config.load_config()
    e = cfg.get("eye") or {}
    engine = engine or e.get("engine") or "frame"
    if engine not in ENGINES:
        raise EyeConfigError(f"未知引擎：{engine}")
    if engine == "qwen_omni":
        protocol = e.get("qwen_protocol") or "openai"
        base = e.get("qwen_base_url_anthropic") if protocol == "anthropic" else e.get("qwen_base_url_openai")
        r = {"engine": engine, "protocol": protocol, "base_url": base or "", "api_key": e.get("qwen_api_key") or "",
             "model": e.get("qwen_model") or "qwen3.8-omni-flash", "video_mode": e.get("qwen_video_mode") or "video",
             "audio_out": bool(e.get("qwen_audio_out")) and protocol == "openai", "voice": e.get("qwen_voice") or "Cherry",
             "compose": "multi"}
    else:
        own = bool(e.get("frame_base_url"))
        llm = cfg.get("llm") or {}
        r = {"engine": engine, "protocol": (e.get("frame_protocol") or "openai") if own else "openai",
             "base_url": e.get("frame_base_url") or llm.get("base_url") or "",
             "api_key": e.get("frame_api_key") or llm.get("api_key") or "",
             "model": e.get("frame_model") or llm.get("model") or "",
             "video_mode": "images", "audio_out": False, "voice": "", "compose": e.get("frame_compose") or "grid"}
    if r["protocol"] not in PROTOCOLS:
        raise EyeConfigError(f"未知协议：{r['protocol']}")
    r["ready"] = bool(r["base_url"] and r["api_key"] and r["model"])
    r["timeout"] = float((cfg.get("llm") or {}).get("timeout", 120))
    return r


def public_view(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "api_key"} | {"api_key_set": bool(r.get("api_key"))}


# ---------------- 组装请求 ----------------
def _split_data_url(u: str) -> tuple[str, str]:
    if u.startswith("data:") and "," in u:
        head, data = u.split(",", 1)
        mt = head[5:].split(";")[0] or "image/jpeg"
        return mt, data
    return "image/jpeg", u


def _norm_frames(frames: list[str]) -> list[str]:
    out = []
    for f in frames or []:
        if not isinstance(f, str) or not f:
            continue
        out.append(f if f.startswith("data:") or f.startswith("http") else "data:image/jpeg;base64," + f)
    return out


def _merge_history(history: list[dict]) -> list[dict]:
    """Anthropic 要求 user / assistant 交替且以 user 开头：合并相邻同角色。"""
    out: list[dict] = []
    for m in history:
        if m.get("role") not in ("user", "assistant") or not m.get("content"):
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n" + m["content"]}
        else:
            out.append(dict(m))
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def build_request(r: dict, system: str, history: list[dict], text: str, frames: list[str],
                  max_tokens: int = 400, video: bool | None = None) -> tuple[str, dict, dict]:
    """返回 (url, headers, body)。video=None 时按配置自动决定。"""
    frames = _norm_frames(frames)
    base = r["base_url"].rstrip("/")
    if r["protocol"] == "anthropic":
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        headers = {"x-api-key": r["api_key"], "Authorization": f"Bearer {r['api_key']}",
                   "anthropic-version": "2023-06-01", "content-type": "application/json"}
        content: list[dict] = []
        for f in frames:
            if f.startswith("http"):
                content.append({"type": "image", "source": {"type": "url", "url": f}})
            else:
                mt, data = _split_data_url(f)
                content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
        content.append({"type": "text", "text": text})
        msgs = _merge_history(history)
        if msgs and msgs[-1]["role"] == "user":
            msgs.append({"role": "assistant", "content": "（嗯）"})
        msgs.append({"role": "user", "content": content})
        body = {"model": r["model"], "max_tokens": max_tokens, "system": system, "messages": msgs, "stream": True}
        return url, headers, body

    if r["protocol"] == "responses":   # OpenAI Responses：先按 Chat 格式组好，再统一转换
        from .. import upstream
        content = [{"type": "image_url", "image_url": {"url": f}} for f in frames] + [{"type": "text", "text": text}]
        msgs = [{"role": "system", "content": system}] + [m for m in history if m.get("content")] + [{"role": "user", "content": content}]
        return upstream.build("responses", base, r["api_key"],
                              {"model": r["model"], "messages": msgs, "stream": True, "max_tokens": max(16, max_tokens)})
    url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {r['api_key']}", "content-type": "application/json"}
    if video is None:
        video = r.get("video_mode") == "video" and len(frames) >= MIN_VIDEO_FRAMES
    content = []
    if video and frames:
        content.append({"type": "video", "video": frames})
    else:
        content += [{"type": "image_url", "image_url": {"url": f}} for f in frames]
    content.append({"type": "text", "text": text})
    msgs = [{"role": "system", "content": system}] + [m for m in history if m.get("content")] + [{"role": "user", "content": content}]
    body = {"model": r["model"], "messages": msgs, "stream": True, "max_tokens": max_tokens,
            "stream_options": {"include_usage": True}}
    if r.get("audio_out"):
        body["modalities"] = ["text", "audio"]
        body["audio"] = {"voice": r.get("voice") or "Cherry", "format": "wav"}
    return url, headers, body


# ---------------- 流式调用 ----------------
def _parse_openai(obj: dict, usage: dict) -> list[dict]:
    out = []
    if obj.get("usage"):
        u = obj["usage"]
        usage["input"] = u.get("prompt_tokens", usage.get("input", 0))
        usage["output"] = u.get("completion_tokens", usage.get("output", 0))
    for ch in obj.get("choices") or []:
        d = ch.get("delta") or ch.get("message") or {}
        c = d.get("content")
        if isinstance(c, str) and c:
            out.append({"type": "delta", "text": c})
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("text"):
                    out.append({"type": "delta", "text": part["text"]})
        a = d.get("audio") or {}
        if a.get("data"):
            out.append({"type": "audio", "data": a["data"]})
        if a.get("transcript"):
            out.append({"type": "delta", "text": a["transcript"]})
    return out


def _parse_anthropic(obj: dict, usage: dict) -> list[dict]:
    t = obj.get("type")
    if t == "message_start":
        u = (obj.get("message") or {}).get("usage") or {}
        usage["input"] = u.get("input_tokens", 0)
    elif t == "message_delta":
        u = obj.get("usage") or {}
        if "output_tokens" in u:
            usage["output"] = u["output_tokens"]
    elif t == "content_block_delta":
        d = obj.get("delta") or {}
        if d.get("type") == "text_delta" and d.get("text"):
            return [{"type": "delta", "text": d["text"]}]
    elif t == "error":
        return [{"type": "error", "message": (obj.get("error") or {}).get("message", "上游出错")}]
    return []


async def stream(r: dict, system: str, history: list[dict], text: str, frames: list[str],
                 max_tokens: int = 400, client: httpx.AsyncClient | None = None) -> AsyncIterator[dict]:
    """逐段产出 {"type":"delta"|"audio"|"error"|"done", ...}。"""
    if not r.get("ready"):
        yield {"type": "error", "message": "宇宙之眼的模型还没配置好（地址 / Key / 模型）"}
        return
    t0 = time.time()
    usage: dict = {}
    first = None
    own = client is None
    client = client or httpx.AsyncClient(timeout=r.get("timeout", 120))
    attempts = [None]
    if r["protocol"] == "openai" and r.get("video_mode") == "video" and len(_norm_frames(frames)) >= MIN_VIDEO_FRAMES:
        attempts = [True, False]  # 先按视频传，失败退回多图
    tr = None
    if r["protocol"] == "responses":
        from .. import upstream
        tr = upstream.StreamTranslator("responses", r.get("model", ""))

    def _resp_parts(obj: dict) -> list[dict]:
        out = []
        for sse in tr.event(obj):
            body = sse[6:].strip()
            if body and body != "[DONE]":
                o = json.loads(body)
                if o.get("error"):
                    out.append({"type": "error", "message": o["error"].get("message", "上游出错")})
                else:
                    out += _parse_openai(o, usage)
        return out

    try:
        for i, video in enumerate(attempts):
            url, headers, body = build_request(r, system, history, text, frames, max_tokens, video)
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    err = (await resp.aread()).decode("utf-8", "replace")[:300]
                    if i + 1 < len(attempts):
                        yield {"type": "info", "message": "上游不认视频格式，已改用多图重试"}
                        continue
                    yield {"type": "error", "message": f"上游 HTTP {resp.status_code}：{err}"}
                    return
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" not in ctype:  # 有的兼容服务忽略 stream=true
                    obj = json.loads((await resp.aread()) or b"{}")
                    if tr is not None:
                        from .. import upstream
                        obj = upstream.parse("responses", obj, r.get("model", ""))
                    parts = _parse_anthropic_full(obj, usage) if r["protocol"] == "anthropic" else _parse_openai(obj, usage)
                    for p in parts:
                        if first is None and p["type"] == "delta":
                            first = time.time()
                        yield p
                else:
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        if tr is not None:
                            parts = _resp_parts(obj)
                        else:
                            parts = _parse_anthropic(obj, usage) if r["protocol"] == "anthropic" else _parse_openai(obj, usage)
                        for p in parts:
                            if first is None and p["type"] in ("delta", "audio"):
                                first = time.time()
                            yield p
                break
    except httpx.HTTPError as e:
        yield {"type": "error", "message": f"连接上游失败：{e.__class__.__name__} {e}"}
        return
    finally:
        if own:
            await client.aclose()
    yield {"type": "done", "usage": usage, "first_ms": int(((first or time.time()) - t0) * 1000),
           "total_ms": int((time.time() - t0) * 1000)}


def _parse_anthropic_full(obj: dict, usage: dict) -> list[dict]:
    u = obj.get("usage") or {}
    usage["input"], usage["output"] = u.get("input_tokens", 0), u.get("output_tokens", 0)
    return [{"type": "delta", "text": c.get("text", "")} for c in obj.get("content") or [] if c.get("type") == "text"]


# ---------------- 能力预检：先确认模型真的能看图 ----------------
def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PROBE = "data:image/png;base64," + base64.b64encode(_png(32, 32, (230, 20, 20))).decode()


async def preflight(r: dict, client: httpx.AsyncClient | None = None) -> dict:
    """发一张纯红小图问颜色：答得出 = 能看图。不支持读图的模型往往不报错只会瞎答、白耗 token。"""
    if not r.get("ready"):
        return {"ok": False, "reason": "未配置地址 / Key / 模型"}
    text, err, done = "", "", {}
    rr = dict(r, audio_out=False, video_mode="images")
    async for p in stream(rr, "你是图像检测器。", [], "这张图片主要是什么颜色？只回答一个颜色词。", [PROBE], 20, client):
        if p["type"] == "delta":
            text += p["text"]
        elif p["type"] == "error":
            err = p["message"]
        elif p["type"] == "done":
            done = p
    ok = (not err) and any(k in text.lower() for k in ("红", "red", "赤"))
    reason = err or ("" if ok else f"模型没认出测试图（回答：{text.strip()[:40] or '空'}），它可能不支持读图")
    return {"ok": ok, "reply": text.strip()[:80], "reason": reason, "first_ms": done.get("first_ms"), "checked_at": time.time()}
