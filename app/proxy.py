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

from . import upstream
from . import archive, config, core, evolve, stars

router = APIRouter()


def _check_key(request: Request, cfg: dict) -> None:
    key = cfg["proxy"].get("proxy_key")
    if not key:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {key}":
        raise HTTPException(401, "代理密钥不正确（在客户端 API Key 处填写星愿设置里的「代理密钥」）")


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return archive._content_text(m.get("content"))
    return ""


def build_injection(query: str, cfg: dict, note: str = "") -> tuple[str, dict]:
    parts = []
    ct = core.core_text()
    if ct:
        parts.append(ct)
    rec = stars.recall(query, budget_tokens=int(cfg["proxy"].get("budget_tokens", 600)), bump=True) if query.strip() else {"items": [], "text": "", "used_tokens": 0}
    if rec.get("text"):
        parts.append(rec["text"])
    sk_txt, sk_used = evolve.review.injection(query) if query.strip() else ("", [])
    if sk_txt:
        parts.append(sk_txt)
    rec["skills"] = sk_used
    if note:
        parts.append(note)
    if not parts:
        return "", rec
    parts.append("（以上为星愿记忆自动注入的长期记忆，请自然地参考，不要逐字复述。）")
    return "\n\n".join(parts), rec


def inject(body: dict, cfg: dict, note: str = "") -> tuple[dict, dict]:
    msgs = list(body.get("messages") or [])
    query = last_user_text(msgs)
    text, rec = build_injection(query, cfg, note)
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


def _log(cfg: dict, body: dict, reply: str, session: str, rec: dict | None = None) -> None:
    user = last_user_text(body.get("messages") or [])
    try:  # 自进化：后台巡检计数 + 对话缓冲
        evolve.review.on_turn(user, reply, (rec or {}).get("skills") or [], session=session,
                              source="proxy:" + str(body.get("model", "")))
    except Exception:
        pass
    if not cfg["proxy"].get("log_to_archive"):
        return
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
    return base, upstream.headers(_proto(cfg), cfg["llm"].get("api_key", ""))


def _err(proto: str, status: int, data: bytes, ctype: str):
    """非 OpenAI 协议的上游错误，转成 OpenAI 的错误格式，客户端才看得懂。"""
    if proto == "openai":
        return Response(data, status_code=status, media_type=ctype)
    msg = upstream.error_message(proto, data) or data.decode("utf-8", "replace")[:500]
    return JSONResponse({"error": {"message": msg, "type": "upstream_error", "code": status}}, status_code=status)


def _proto(cfg: dict) -> str:
    return upstream.norm(cfg["llm"].get("protocol"))


def _models_url(cfg: dict, base: str) -> str:
    if _proto(cfg) == "anthropic" and not base.endswith("/v1"):
        return base + "/v1/models"
    return base + "/models"


# ---------------- 主人指令（核心记忆） ----------------
def _owner_ok(request: Request, cfg: dict) -> bool:
    """能走到这里说明代理密钥已通过；没设代理密钥时只信任本机请求。"""
    if cfg["proxy"].get("proxy_key"):
        return True
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def _core_command(body: dict, cfg: dict, request: Request) -> tuple[dict | None, str]:
    """返回 (斜杠指令的执行结果 或 None, 自然语言指令要附加给模型的说明)。"""
    cc = cfg.get("core") or {}
    text = last_user_text(body.get("messages") or [])
    sk = evolve.commands.parse(text) if cc.get("chat_commands", True) else None
    if sk:
        reply = evolve.commands.execute(sk, authorized=_owner_ok(request, cfg), source="command")
        if sk["type"] == "slash":
            return {"reply": reply}, ""
        return None, "（星愿提示：主人要求把刚才的做法沉淀成技能，后台巡检已启动。请简短确认。）"
    if not (cc.get("chat_commands", True) or cc.get("natural_commands", True)):
        return None, ""
    cmd = core.commands.parse(last_user_text(body.get("messages") or []), natural=bool(cc.get("natural_commands", True)))
    if not cmd or (cmd["type"] == "slash" and not cc.get("chat_commands", True)):
        return None, ""
    slash = cmd["type"] == "slash"
    out = core.commands.execute(cmd, source="proxy-command" if slash else "proxy-natural",
                                authorized=_owner_ok(request, cfg))
    if slash:
        return out, ""
    res = out.get("result") or {}
    if res.get("applied"):
        note = f"（星愿提示：主人刚刚在这条消息里更新了核心记忆 r{res['rev']}：{cmd.get('summary', '')}。请简短自然地确认已记住。）"
    elif res.get("proposed"):
        note = "（星愿提示：主人想修改核心记忆，但核心记忆已上锁 / 未授权，已转为提案等待主人在面板批准。请如实告知。）"
    else:
        note = ""
    return None, note


def _synth(body: dict, text: str) -> Response:
    """不调用上游，直接以 OpenAI 格式回复（斜杠指令用，0 token）。"""
    cid, now, model = f"starwish-core-{int(time.time() * 1000)}", int(time.time()), body.get("model") or "starwish"
    hdr = {"x-starwish-core-command": "1"}
    if body.get("stream"):
        def gen():
            first = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": model,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
            last = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream", headers={**hdr, "Cache-Control": "no-cache"})
    return JSONResponse({"id": cid, "object": "chat.completion", "created": now, "model": model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}, headers=hdr)


@router.get("/v1/models")
async def models(request: Request):
    cfg = config.load_config()
    _check_key(request, cfg)
    try:
        base, headers = _upstream(cfg)
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(_models_url(cfg, base), headers=headers)
        if r.status_code == 200:
            return JSONResponse(r.json())
    except Exception:
        pass
    m = cfg["proxy"].get("model_override") or cfg["llm"].get("model") or "starwish"
    return {"object": "list", "data": [{"id": m, "object": "model", "created": int(time.time()), "owned_by": "starwish"}]}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    cfg = config.load_config()
    _check_key(request, cfg)
    body = await request.json()
    session = request.headers.get("x-starwish-session", "") or str(body.get("user", "") or "")
    if cfg["proxy"].get("model_override"):
        body["model"] = cfg["proxy"]["model_override"]
    elif not body.get("model"):
        body["model"] = cfg["llm"].get("model")
    cmd_out, core_note = await run_in_threadpool(_core_command, body, cfg, request)
    if cmd_out is not None:  # /核心 指令：本地处理，不发给上游
        return _synth(body, cmd_out["reply"])
    rec = {}
    if cfg["proxy"].get("inject", True) and request.headers.get("x-starwish-inject", "1") != "0":
        body, rec = await run_in_threadpool(inject, body, cfg, core_note)
    base, _ = _upstream(cfg)
    proto = _proto(cfg)
    url, headers, payload = upstream.build(proto, base, cfg["llm"].get("api_key", ""), body)
    timeout = httpx.Timeout(float(cfg["llm"].get("timeout", 120)), connect=15)
    extra = {"x-starwish-recalled": ",".join(str(i["id"]) for i in rec.get("items", [])),
             "x-starwish-recall-tokens": str(rec.get("used_tokens", 0)),
             "x-starwish-skills": ",".join(rec.get("skills") or [])}

    if body.get("stream"):
        client = httpx.AsyncClient(timeout=timeout)
        req = client.build_request("POST", url, json=payload, headers=headers)
        r = await client.send(req, stream=True)
        if r.status_code != 200:
            data = await r.aread()
            await r.aclose()
            await client.aclose()
            return _err(proto, r.status_code, data, r.headers.get("content-type", "application/json"))
        tr = upstream.StreamTranslator(proto, body.get("model", ""))
        ctype = r.headers.get("content-type", "")

        async def gen():
            try:
                if proto == "openai":
                    async for chunk in r.aiter_raw():
                        tr.feed(chunk)
                        yield chunk
                elif "text/event-stream" not in ctype:   # 上游忽略了 stream：整包转成流
                    data = json.loads((await r.aread()) or b"{}")
                    full = upstream.parse(proto, data, body.get("model", ""))
                    msg = full["choices"][0]["message"]
                    tr.text_parts.append(msg.get("content") or "")
                    yield tr._chunk({"role": "assistant", "content": msg.get("content") or "",
                                     **({"tool_calls": [dict(tc, index=i) for i, tc in enumerate(msg["tool_calls"])]}
                                        if msg.get("tool_calls") else {})}).encode()
                    yield tr._chunk({}, full["choices"][0]["finish_reason"], full.get("usage")).encode()
                    yield b"data: [DONE]\n\n"
                else:
                    async for chunk in r.aiter_raw():
                        for s in tr.feed(chunk):
                            yield s.encode("utf-8")
                    for s in tr.finish():
                        yield s.encode("utf-8")
            finally:
                await r.aclose()
                await client.aclose()

        def after():
            _log(cfg, body, tr.text, session, rec)

        return StreamingResponse(gen(), media_type="text/event-stream" if proto != "openai" else (ctype or "text/event-stream"),
                                 headers={**extra, "Cache-Control": "no-cache"}, background=BackgroundTask(after))

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code != 200:
        return _err(proto, r.status_code, r.content, r.headers.get("content-type", "application/json"))
    data = upstream.parse(proto, r.json(), body.get("model", ""))
    try:
        reply = data["choices"][0]["message"]["content"] or ""
    except Exception:
        reply = ""
    _log(cfg, body, reply, session, rec)
    return JSONResponse(data, headers=extra)
