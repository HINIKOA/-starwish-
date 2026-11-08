"""星愿记忆 v0.10.0 测试：上游三种接口格式（OpenAI 兼容 / Anthropic / OpenAI Responses）。

对话代理（含流式、工具调用、错误转换）、后台整理、检测、宇宙之眼都要能走三种格式。
用本地模拟的上游，不联网。用法：python tests/upstream_test.py
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="starwish-upstream-"))
os.environ["STARWISH_SOUL_DIR"] = str(TMP / "soul")
os.environ["STARWISH_NO_EMBED_BOOT"] = "1"

from smoke_test import check, RESULTS, free_port  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

KEY = "sk-up-1234567890"
LAST: dict = {}


def sse(events, named=False):
    async def gen():
        for e in events:
            if named:
                yield f"event: {e['type']}\n"
            yield "data: " + json.dumps(e, ensure_ascii=False) + "\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def mock():
    up = FastAPI()

    @up.post("/v1/messages")
    async def anth(req: Request, x_api_key: str = Header(""), anthropic_version: str = Header("")):
        if x_api_key != KEY or not anthropic_version:
            raise HTTPException(401, "invalid x-api-key")
        b = await req.json()
        LAST["anthropic"] = b
        if b["model"] == "bad":
            from fastapi.responses import JSONResponse
            return JSONResponse({"type": "error", "error": {"type": "not_found_error", "message": "model: bad"}}, 404)
        tool = bool(b.get("tools"))
        if not b.get("stream"):
            content = [{"type": "text", "text": "好的"}]
            if tool:
                content.append({"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "上海"}})
            return {"id": "msg_1", "model": b["model"], "content": content,
                    "stop_reason": "tool_use" if tool else "end_turn", "usage": {"input_tokens": 11, "output_tokens": 3}}
        ev = [{"type": "message_start", "message": {"model": b["model"], "usage": {"input_tokens": 11}}},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你"}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}},
              {"type": "content_block_stop", "index": 0}]
        if tool:
            ev += [{"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_2", "name": "get_weather", "input": {}}},
                   {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"city\":"}},
                   {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "\"北京\"}"}},
                   {"type": "content_block_stop", "index": 1}]
        ev += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"}, "usage": {"output_tokens": 2}},
               {"type": "message_stop"}]
        return sse(ev, named=True)

    @up.post("/v1/responses")
    async def resp(req: Request, authorization: str = Header("")):
        if authorization != f"Bearer {KEY}":
            raise HTTPException(401, "bad key")
        b = await req.json()
        LAST["responses"] = b
        tool = bool(b.get("tools"))
        if not b.get("stream"):
            out = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "想了想"}]},
                   {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好的"}]}]
            if tool:
                out.append({"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{\"city\":\"上海\"}"})
            return {"id": "resp_1", "model": b["model"], "status": "completed", "output": out,
                    "usage": {"input_tokens": 9, "output_tokens": 4}}
        ev = [{"type": "response.created", "response": {"model": b["model"]}},
              {"type": "response.output_text.delta", "output_index": 0, "delta": "你"},
              {"type": "response.output_text.delta", "output_index": 0, "delta": "好"}]
        if tool:
            ev += [{"type": "response.output_item.added", "output_index": 1,
                    "item": {"type": "function_call", "call_id": "call_2", "name": "get_weather", "arguments": ""}},
                   {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "{\"city\":"},
                   {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "\"北京\"}"}]
        ev += [{"type": "response.completed", "response": {"usage": {"input_tokens": 9, "output_tokens": 2}}}]
        return sse(ev, named=True)

    @up.get("/v1/models")
    async def models(authorization: str = Header(""), x_api_key: str = Header("")):
        if authorization != f"Bearer {KEY}" and x_api_key != KEY:
            raise HTTPException(401, "bad key")
        return {"data": [{"id": "claude-sonnet-4-5"}, {"id": "gpt-5-mini"}]}

    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(up, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return f"http://127.0.0.1:{port}"


def sse_objs(text: str) -> list:
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            d = line[5:].strip()
            if d and d != "[DONE]":
                out.append(json.loads(d))
    return out


def merged(chunks: list) -> dict:
    """把 chunk 合成一条消息（文本 + 工具调用 + 结束原因）。"""
    text, tools, fin, usage = "", {}, None, None
    for c in chunks:
        if c.get("usage"):
            usage = c["usage"]
        for ch in c.get("choices") or []:
            d = ch.get("delta") or {}
            text += d.get("content") or ""
            for tc in d.get("tool_calls") or []:
                t = tools.setdefault(tc["index"], {"id": "", "name": "", "args": ""})
                t["id"] = tc.get("id") or t["id"]
                f = tc.get("function") or {}
                t["name"] = f.get("name") or t["name"]
                t["args"] += f.get("arguments") or ""
            fin = ch.get("finish_reason") or fin
    return {"text": text, "tools": list(tools.values()), "finish": fin, "usage": usage}


def main():
    UP = mock()
    from fastapi.testclient import TestClient

    from app import config, maintenance, providers, upstream
    from app.eye import engine
    from app.server import app

    c = TestClient(app)

    print("== 1. 请求格式转换")
    body = {"model": "m", "max_tokens": 100, "temperature": 0.3, "stop": "END", "stream": True,
            "messages": [{"role": "system", "content": "你是星愿"},
                         {"role": "user", "content": [{"type": "text", "text": "看图"},
                                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
                         {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                                               "function": {"name": "f", "arguments": "{\"a\":1}"}}]},
                         {"role": "tool", "tool_call_id": "c1", "content": "结果"},
                         {"role": "user", "content": "继续"}],
            "tools": [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}],
            "tool_choice": "required"}
    url, h, a = upstream.build("anthropic", "https://x.com", "k", body)
    check("Anthropic：地址补 /v1/messages、用 x-api-key", url == "https://x.com/v1/messages" and h.get("x-api-key") == "k" and "Authorization" not in h)
    check("Anthropic：system 单独放", a["system"] == "你是星愿" and all(m["role"] != "system" for m in a["messages"]))
    check("Anthropic：图片转 base64 块", a["messages"][0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}})
    check("Anthropic：工具调用 → tool_use，参数转对象", a["messages"][1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "f", "input": {"a": 1}})
    check("Anthropic：工具结果并进 user，且与后一句合并", a["messages"][2]["role"] == "user" and a["messages"][2]["content"][0]["type"] == "tool_result"
          and a["messages"][2]["content"][1]["text"] == "继续" and len(a["messages"]) == 3)
    check("Anthropic：tools / tool_choice / stop / max_tokens", a["tools"][0]["input_schema"] == {"type": "object"} and a["tool_choice"] == {"type": "any"}
          and a["stop_sequences"] == ["END"] and a["max_tokens"] == 100 and a["stream"] is True)
    url, h, a = upstream.build("anthropic", "https://x.com/v1", "k", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    check("Anthropic：地址已带 /v1 不重复，默认 max_tokens", url == "https://x.com/v1/messages" and a["max_tokens"] == upstream.DEFAULT_MAX_TOKENS)
    url, h, r = upstream.build("responses", "https://api.openai.com/v1", "k", body)
    check("Responses：地址 /responses、Bearer", url == "https://api.openai.com/v1/responses" and h["Authorization"] == "Bearer k")
    check("Responses：system → instructions、不存储", r["instructions"] == "你是星愿" and r["store"] is False)
    check("Responses：用户图片 → input_image", r["input"][0]["content"] == [{"type": "input_text", "text": "看图"},
                                                                   {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}])
    check("Responses：工具调用与结果 → function_call / function_call_output",
          r["input"][1] == {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{\"a\":1}"}
          and r["input"][2] == {"type": "function_call_output", "call_id": "c1", "output": "结果"})
    check("Responses：max_output_tokens / tools 扁平格式", r["max_output_tokens"] == 100 and r["tools"][0]["name"] == "f"
          and r["tools"][0]["parameters"] == {"type": "object"} and r["tool_choice"] == "required")
    url, h, o = upstream.build("openai", "https://x/v1", "k", body)
    check("OpenAI 兼容：原样转发", url == "https://x/v1/chat/completions" and o is body)
    check("未知协议按 OpenAI 兼容处理", upstream.norm("xxx") == "openai" and upstream.norm("Responses") == "responses")

    print("== 2. 回应与流式转换")
    tr = upstream.StreamTranslator("anthropic", "m")
    raw = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "星愿"}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"}]).encode()
    outs = []
    for i in range(0, len(raw), 7):        # 故意切碎，跨包也要能拼回
        outs += tr.feed(raw[i:i + 7])
    m = merged(sse_objs("".join(outs)))
    check("流式：字节被切碎也能拼回", m["text"] == "星愿" and tr.text == "星愿")
    check("流式：结束原因与用量", m["finish"] == "length" and m["usage"]["prompt_tokens"] == 5 and m["usage"]["completion_tokens"] == 7)
    check("流式：以 [DONE] 结束且只结束一次", "".join(outs).count("[DONE]") == 1 and tr.finish() == [])
    tr = upstream.StreamTranslator("responses", "m")
    outs = tr.feed(b'data: {"type":"error","message":"rate limited"}\n\n')
    check("流式：上游错误转成 OpenAI 错误块", sse_objs("".join(outs))[0]["error"]["message"] == "rate limited")

    print("== 3. 对话代理（三种格式）")
    prov = [{"id": "an", "name": "Claude", "preset": "anthropic", "protocol": "anthropic", "base_url": UP, "api_key": KEY,
             "enabled": True, "models": [{"id": "claude-sonnet-4-5", "kind": "chat"}]},
            {"id": "rs", "name": "OpenAI Responses", "preset": "openai", "protocol": "responses", "base_url": UP + "/v1",
             "api_key": KEY, "enabled": True, "models": [{"id": "gpt-5-mini", "kind": "chat"}]}]
    c.put("/api/settings", json={"providers": prov, "defaults": {"chat": "an::claude-sonnet-4-5", "maintain": "rs::gpt-5-mini",
                                                                 "vision": "rs::gpt-5-mini"}})
    cfg = config.load_config()
    check("默认模型带上协议", cfg["llm"]["protocol"] == "anthropic" and cfg["llm"]["maint_protocol"] == "responses")
    check("识图默认模型 → 宇宙之眼协议", cfg["eye"]["frame_protocol"] == "responses")
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    for proto, ref in (("anthropic", "an::claude-sonnet-4-5"), ("responses", "rs::gpt-5-mini")):
        c.put("/api/settings", json={"defaults": {"chat": ref}})
        name = "Anthropic" if proto == "anthropic" else "Responses"
        r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "你好"}]})
        d = r.json()
        check(f"代理→{name}：普通回复转成 chat.completion", r.status_code == 200 and d["object"] == "chat.completion"
              and d["choices"][0]["message"]["content"] == "好的" and d["choices"][0]["finish_reason"] == "stop", r.text[:200])
        check(f"代理→{name}：用量换算", d["usage"]["prompt_tokens"] > 0 and d["usage"]["total_tokens"] == d["usage"]["prompt_tokens"] + d["usage"]["completion_tokens"])
        sent = LAST[proto]
        check(f"代理→{name}：上游收到的是自己的格式", ("messages" in sent and "max_tokens" in sent) if proto == "anthropic" else ("input" in sent and "messages" not in sent))
        r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "天气"}], "tools": tools})
        d = r.json()["choices"][0]
        check(f"代理→{name}：工具调用转回 tool_calls", d["finish_reason"] == "tool_calls" and d["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
              and json.loads(d["message"]["tool_calls"][0]["function"]["arguments"]) == {"city": "上海"})
        if proto == "responses":
            check("代理→Responses：思考摘要 → reasoning_content", d["message"].get("reasoning_content") == "想了想")
        with c.stream("POST", "/v1/chat/completions", json={"messages": [{"role": "user", "content": "你好"}], "stream": True}) as s:
            txt = "".join(s.iter_text())
            ctype = s.headers.get("content-type", "")
        m = merged(sse_objs(txt))
        check(f"代理→{name}：流式转成 chat.completion.chunk", "text/event-stream" in ctype and m["text"] == "你好" and m["finish"] == "stop"
              and txt.rstrip().endswith("data: [DONE]"), txt[:200])
        check(f"代理→{name}：流式首块带 role", sse_objs(txt)[0]["choices"][0]["delta"].get("role") == "assistant")
        with c.stream("POST", "/v1/chat/completions", json={"messages": [{"role": "user", "content": "天气"}], "stream": True, "tools": tools}) as s:
            m = merged(sse_objs("".join(s.iter_text())))
        check(f"代理→{name}：流式工具调用参数逐段拼回", m["finish"] == "tool_calls" and m["tools"] and m["tools"][0]["name"] == "get_weather"
              and json.loads(m["tools"][0]["args"]) == {"city": "北京"} and m["tools"][0]["id"])
    c.put("/api/settings", json={"defaults": {"chat": "an::claude-sonnet-4-5"}})
    c.put("/api/settings", json={"proxy": {"model_override": "bad"}})
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    check("代理：Anthropic 的错误转成 OpenAI 错误格式", r.status_code == 404 and r.json()["error"]["message"] == "model: bad")
    c.put("/api/settings", json={"proxy": {"model_override": ""}})
    ms = c.get("/v1/models").json()
    check("代理：Anthropic 的模型列表可透传", any(x["id"] == "claude-sonnet-4-5" for x in ms["data"]))

    print("== 4. 后台整理 / 检测 / 宇宙之眼")
    LAST.clear()
    out = maintenance.chat([{"role": "system", "content": "整理"}, {"role": "user", "content": "hi"}])
    check("整理模型在 Responses 服务商：走 /responses", out == "好的" and LAST.get("responses", {}).get("instructions") == "整理")
    c.put("/api/settings", json={"defaults": {"maintain": "an::claude-sonnet-4-5"}})
    LAST.clear()
    out = maintenance.chat([{"role": "user", "content": "hi"}])
    check("整理模型与对话同在 Anthropic：走 /v1/messages", out == "好的" and "anthropic" in LAST and "responses" not in LAST)
    r = c.post("/api/providers/check", json={"id": "rs", "model": "gpt-5-mini"}).json()
    check("检测 Responses 模型", r.get("ok") and "模型可用" in r.get("msg", ""), str(r))
    r = c.post("/api/providers/check", json={"id": "an", "model": "claude-sonnet-4-5"}).json()
    check("检测 Anthropic 模型", r.get("ok"), str(r))
    r = c.post("/api/providers/check", json={"id": "an"}).json()
    check("Anthropic 拉模型列表", r.get("ok"), str(r))
    bad = providers.find(config.load_config(), "rs")
    try:
        providers.check(bad["base_url"], "sk-wrong", "responses", "gpt-5-mini")
        ok = False
    except RuntimeError as e:
        ok = "API Key" in str(e)
    check("Responses 错误 Key 给出中文原因", ok)
    eye_r = {"ready": True, "protocol": "responses", "base_url": UP + "/v1", "api_key": KEY, "model": "gpt-5-mini", "timeout": 30}
    url, h, b = engine.build_request(eye_r, "看屏幕", [], "这是什么", ["data:image/jpeg;base64,BBBB"], 200)
    check("宇宙之眼 Responses：帧转成 input_image", url.endswith("/responses") and b["input"][-1]["content"][0]["type"] == "input_image")

    async def run_eye():
        return [p async for p in engine.stream(eye_r, "看屏幕", [], "这是什么", ["data:image/jpeg;base64,BBBB"], 200)]
    parts = asyncio.run(run_eye())
    txt = "".join(p.get("text", "") for p in parts if p["type"] == "delta")
    check("宇宙之眼 Responses：流式出字", txt == "你好" and parts[-1]["type"] == "done", str(parts)[:200])
    check("宇宙之眼协议清单含 responses", "responses" in engine.PROTOCOLS)
    js = (ROOT / "web" / "settings.js").read_text("utf-8")
    check("设置里可选 OpenAI Responses 格式", 'value="responses"' in js and "/responses" in js)

    ok = sum(1 for _, g, _ in RESULTS if g)
    for nme, g, dt in RESULTS:
        if not g:
            print("  ✘", nme, dt)
    print(f"\n上游接口格式结果：{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
