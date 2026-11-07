"""OpenAI 兼容代理：/v1/chat/completions 与 /v1/models。

把「核心记忆 + 与最后一条用户消息相关的星星摘要」注入 system 提示，然后转发给上游
（OpenAI / DeepSeek / Claude 的 OpenAI 兼容端点 / 任何兼容服务），支持流式透传。
"""
from __future__ import annotations

import json
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import archive, config, core, stars

router = APIRouter()


def _check_key(request: Request, cfg: dict) -> None:
    key = cfg["proxy"].get("proxy_key")
    if not key:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {key}":
        raise HTTPException(401, "代理密钥不正确（在客户端 API Key 处填写星魂设置里的「代理密钥」）")


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return archive._content_text(m.get("content"))
    return ""


def build_injection(query: str, cfg: dict) -> tuple[str, dict]:
    parts = []
    ct = core.core_text()
    if ct:
        parts.append(ct)
    rec = stars.recall(query, budget_tokens=int(cfg["proxy"].get("budget_tokens", 600)), bump=True) if query.strip() else {"items": [], "text": "", "used_tokens": 0}
    if rec.get("text"):
        parts.append(rec["text"])
    if not parts:
        return "", rec
    parts.append("（以上为星魂记忆库自动注入的长期记忆，请自然地参考，不要逐字复述。）")
    return "\n\n".join(parts), rec


def inject(body: dict, cfg: dict) -> tuple[dict, dict]:
    msgs = list(body.get("messages") or [])
    query = last_user_text(msgs)
    text, rec = build_injection(query, cfg)
    if text:
        if msgs and msgs[0].get("role") == "system":
            first = dict(msgs[0])
            old = archive._content_text(first.get("content"))
            first["content"] = text + "\n\n" + old
            msgs[0] = first
        else:
            msgs.insert(0, {"role": "system", "content": text})
    body = {**body, "messages": msgs}
    return body, rec


def _log(cfg: dict, body: dict, reply: str, session: str) -> None:
    if not cfg["proxy"].get("log_to_archive"):
        return
    user = last_user_text(body.get("messages") or [])
    msgs = [{"role": "user", "content": user}]
    if reply:
        msgs.append({"role": "assistant", "content": reply})
    try:
        archive.append_messages(msgs, session=session, source="proxy:" + str(body.get("model", "")))
    except Exception:
        pass


def _upstream(cfg: dict) -> tuple[str, dict]:
    base = (cfg["llm"].get("base_url") or "").rstrip("/")
    if not base:
        raise HTTPException(500, "未配置上游 base_url")
    headers = {"Content-Type": "application/json"}
    if cfg["llm"].get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['llm']['api_key']}"
    return base, headers


@router.get("/v1/models")
async def models(request: Request):
    cfg = config.load_config()
    _check_key(request, cfg)
    try:
        base, headers = _upstream(cfg)
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(base + "/models", headers=headers)
        if r.status_code == 200:
            return JSONResponse(r.json())
    except Exception:
        pass
    m = cfg["proxy"].get("model_override") or cfg["llm"].get("model") or "soulstar"
    return {"object": "list", "data": [{"id": m, "object": "model", "created": int(time.time()), "owned_by": "soulstar"}]}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    cfg = config.load_config()
    _check_key(request, cfg)
    body = await request.json()
    session = request.headers.get("x-soulstar-session", "") or str(body.get("user", "") or "")
    if cfg["proxy"].get("model_override"):
        body["model"] = cfg["proxy"]["model_override"]
    elif not body.get("model"):
        body["model"] = cfg["llm"].get("model")
    rec = {}
    if cfg["proxy"].get("inject", True) and request.headers.get("x-soulstar-inject", "1") != "0":
        body, rec = await run_in_threadpool(inject, body, cfg)
    base, headers = _upstream(cfg)
    url = base + "/chat/completions"
    timeout = httpx.Timeout(float(cfg["llm"].get("timeout", 120)), connect=15)
    extra = {"x-soulstar-recalled": ",".join(str(i["id"]) for i in rec.get("items", [])),
             "x-soulstar-recall-tokens": str(rec.get("used_tokens", 0))}

    if body.get("stream"):
        client = httpx.AsyncClient(timeout=timeout)
        req = client.build_request("POST", url, json=body, headers=headers)
        r = await client.send(req, stream=True)
        if r.status_code != 200:
            data = await r.aread()
            await r.aclose()
            await client.aclose()
            return Response(data, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
        collected: list[bytes] = []

        async def gen():
            try:
                async for chunk in r.aiter_raw():
                    collected.append(chunk)
                    yield chunk
            finally:
                await r.aclose()
                await client.aclose()

        def after():
            reply = []
            for line in b"".join(collected).decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    delta = json.loads(payload)["choices"][0].get("delta") or {}
                    if delta.get("content"):
                        reply.append(delta["content"])
                except Exception:
                    pass
            _log(cfg, body, "".join(reply), session)

        return StreamingResponse(gen(), media_type=r.headers.get("content-type", "text/event-stream"),
                                 headers={**extra, "Cache-Control": "no-cache"}, background=BackgroundTask(after))

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=body, headers=headers)
    if r.status_code != 200:
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
    data = r.json()
    try:
        reply = data["choices"][0]["message"]["content"] or ""
    except Exception:
        reply = ""
    _log(cfg, body, reply, session)
    return JSONResponse(data, headers=extra)
