"""思考等级测试：python tests/effort_test.py

覆盖：三种协议怎么传思考等级、温度处理、Anthropic 思考块回传、模型不认时自动去掉重试、
星愿助手按配置带上档位、/思考 指令、设置接口与对话抽屉 UI。
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
T = tempfile.mkdtemp(prefix="sw-effort-")
os.environ.update(STARWISH_SOUL_DIR=T + "/soul", STARWISH_NO_EMBED_BOOT="1", STARWISH_NO_WATCH="1",
                  STARWISH_NO_DAILY="1", STARWISH_HOME=T + "/home", NO_PROXY="127.0.0.1,localhost")

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASS, FAIL = [], []
SEEN: list = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✔ " if cond else "  ✘ ") + name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def mock():
    up = FastAPI()

    @up.post("/v1/chat/completions")
    async def chat(req: Request):
        b = await req.json()
        SEEN.append(("openai", b))
        if b["model"] == "old" and "reasoning_effort" in b:
            return JSONResponse({"error": {"message": "Unrecognized request argument supplied: reasoning_effort",
                                           "type": "invalid_request_error"}}, 400)
        return {"id": "c1", "object": "chat.completion", "model": b["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}]}

    @up.post("/v1/messages")
    async def anth(req: Request):
        b = await req.json()
        SEEN.append(("anthropic", b))
        if b["model"] == "claude-old" and "output_config" in b:
            return JSONResponse({"type": "error", "error": {"type": "invalid_request_error",
                                                            "message": "output_config: Extra inputs are not permitted"}}, 400)
        if b["model"] == "claude-tool" and len(b["messages"]) == 1:
            return {"id": "m1", "model": b["model"], "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "thinking", "thinking": "先查一下", "signature": "SIG123"},
                                {"type": "tool_use", "id": "toolu_1", "name": "get_status", "input": {}}]}
        return {"id": "m2", "model": b["model"], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "text", "text": "好的"}]}

    @up.post("/v1/responses")
    async def resp(req: Request):
        b = await req.json()
        SEEN.append(("responses", b))
        return {"id": "r1", "model": b["model"], "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好"}]}]}

    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(up, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return f"http://127.0.0.1:{port}/v1"


def last(kind):
    return [b for k, b in SEEN if k == kind][-1]


def main():
    from app import upstream, config, agent, context
    from app.server import app
    BASE = mock()
    msgs = [{"role": "user", "content": "hi"}]

    print("== 1. 档位定义（和 GPT / Claude Opus 一致）")
    check("8 个档位：默认 / none / minimal / low / medium / high / xhigh / max",
          upstream.EFFORTS == ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"])
    check("不认得的值当默认", upstream.norm_effort("超级") == "default" and upstream.norm_effort(None) == "default")
    check("Responses 写法 {effort: high} 也认", upstream._effort_of({"reasoning_effort": {"effort": "high"}}) == "high")

    print("== 2. OpenAI Chat Completions")
    upstream.complete("openai", BASE, "k", {"model": "gpt-5.2", "messages": msgs, "temperature": 0.3, "reasoning_effort": "xhigh"})
    b = last("openai")
    check("reasoning_effort=xhigh 原样传", b.get("reasoning_effort") == "xhigh")
    check("思考时去掉 temperature（GPT-5 类不接受）", "temperature" not in b)
    upstream.complete("openai", BASE, "k", {"model": "gpt-5.2", "messages": msgs, "temperature": 0.3, "reasoning_effort": "none"})
    b = last("openai")
    check("none 照传且保留 temperature", b.get("reasoning_effort") == "none" and b.get("temperature") == 0.3)
    upstream.complete("openai", BASE, "k", {"model": "gpt-4o", "messages": msgs, "temperature": 0.3})
    b = last("openai")
    check("默认档：不传 reasoning_effort", "reasoning_effort" not in b and b.get("temperature") == 0.3)
    _, _, p = upstream.build("openai", BASE, "k", {"model": "x", "messages": msgs, "reasoning_effort": "auto-ish"})
    check("代理场景：不认得的档位原样透传", p.get("reasoning_effort") == "auto-ish")
    n = len(SEEN)
    r = upstream.complete("openai", BASE, "k", {"model": "old", "messages": msgs, "temperature": 0.3, "reasoning_effort": "high"})
    retry = [b for k, b in SEEN[n:] if k == "openai"]
    check("模型不认思考等级：自动去掉重试一次", len(retry) == 2 and "reasoning_effort" not in retry[1] and retry[1].get("temperature") == 0.3)
    check("重试成功并标记 effort_dropped", r.get("effort_dropped") is True and upstream.reply_text(r) == "好")

    print("== 3. Anthropic（Claude Opus 等）")
    upstream.complete("anthropic", BASE, "k", {"model": "claude-opus", "messages": msgs, "temperature": 0.3, "reasoning_effort": "max"})
    b = last("anthropic")
    check("effort 放在 output_config.effort", b.get("output_config") == {"effort": "max"})
    check("max 档：max_tokens 至少 64000（思考也算输出）", b["max_tokens"] >= 64000)
    check("开思考等级时不传 temperature", "temperature" not in b)
    upstream.complete("anthropic", BASE, "k", {"model": "claude-opus", "messages": msgs, "reasoning_effort": "none"})
    check("Claude 没有 none：按 low", last("anthropic").get("output_config") == {"effort": "low"})
    upstream.complete("anthropic", BASE, "k", {"model": "claude-opus", "messages": msgs, "reasoning_effort": "minimal"})
    check("Claude 没有 minimal：按 low", last("anthropic").get("output_config") == {"effort": "low"})
    upstream.complete("anthropic", BASE, "k", {"model": "claude-opus", "messages": msgs, "max_tokens": 2000, "reasoning_effort": "high"})
    check("调用方自己给了 max_tokens 就不改", last("anthropic")["max_tokens"] == 2000)
    upstream.complete("anthropic", BASE, "k", {"model": "claude-opus", "messages": msgs, "temperature": 0.3})
    b = last("anthropic")
    check("默认档：不传 output_config，温度照旧", "output_config" not in b and b.get("temperature") == 0.3)
    r = upstream.complete("anthropic", BASE, "k", {"model": "claude-old", "messages": msgs, "reasoning_effort": "high"})
    check("老 Claude 不认 effort：自动去掉重试", r.get("effort_dropped") is True and "output_config" not in last("anthropic"))
    r = upstream.complete("anthropic", BASE, "k", {"model": "claude-tool", "messages": msgs, "reasoning_effort": "high"})
    m = r["choices"][0]["message"]
    check("思考 + 工具调用：解析出带签名的思考块", m.get("thinking_blocks", [{}])[0].get("signature") == "SIG123")
    _, _, p = upstream.build("anthropic", BASE, "k", {"model": "claude-tool", "messages": msgs + [
        {"role": "assistant", "content": "", "tool_calls": m["tool_calls"], "thinking_blocks": m["thinking_blocks"]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "{}"}]})
    blocks = p["messages"][1]["content"]
    check("下一轮把思考块放在工具调用前原样传回", blocks[0]["type"] == "thinking" and blocks[0]["signature"] == "SIG123" and blocks[1]["type"] == "tool_use")
    _, _, p = upstream.build("openai", BASE, "k", {"model": "x", "messages": [{"role": "assistant", "content": "", "thinking_blocks": [{}]}]})
    check("OpenAI 协议不带 thinking_blocks", "thinking_blocks" not in p["messages"][0])

    print("== 4. OpenAI Responses")
    upstream.complete("responses", BASE, "k", {"model": "gpt-5.2", "messages": msgs, "temperature": 0.3, "reasoning_effort": "medium"})
    b = last("responses")
    check("reasoning.effort=medium", b.get("reasoning") == {"effort": "medium"} and "temperature" not in b)
    upstream.complete("responses", BASE, "k", {"model": "gpt-5.2", "messages": msgs, "temperature": 0.3})
    b = last("responses")
    check("默认档：不传 reasoning", "reasoning" not in b and b.get("temperature") == 0.3)

    print("== 5. 星愿助手")
    c = TestClient(app)
    cfg = config.load_config()
    check("默认配置 llm.agent_effort = default", cfg["llm"].get("agent_effort") == "default")
    got = []

    def fake(messages, cfg, tools=None, temperature=0.2, effort=None):
        got.append(effort)
        return {"choices": [{"message": {"role": "assistant", "content": "好"}}]}
    context.main_complete = fake
    context.main_ready = lambda cfg: True
    c.put("/api/settings", json={"llm": {"agent_effort": "xhigh"}})
    r = c.post("/api/agent/chat", json={"text": "你好"}).json()
    check("设置页改档位后，对话按该档位调用", got[-1] == "xhigh" and r.get("effort") == "xhigh")
    r = c.post("/api/agent/chat", json={"text": "/思考 低"}).json()
    check("/思考 低 → low", r["ok"] and config.load_config()["llm"]["agent_effort"] == "low")
    r = c.post("/api/agent/chat", json={"text": "/思考 最高"}).json()
    check("/思考 最高 → max", config.load_config()["llm"]["agent_effort"] == "max")
    r = c.post("/api/agent/chat", json={"text": "/思考"}).json()
    check("/思考 不带参数：显示当前档位", "最高" in r["reply"])
    r = c.post("/api/agent/chat", json={"text": "/思考 乱写"}).json()
    check("/思考 乱写：拒绝并列出可选", not r["ok"] and "超高" in r["reply"])
    r = c.post("/api/agent/chat", json={"text": "/effort medium"}).json()
    check("/effort medium 英文也行", config.load_config()["llm"]["agent_effort"] == "medium")
    h = c.get("/api/agent/history").json()
    check("历史接口返回当前档位和 8 个选项", h["effort"] == "medium" and len(h["efforts"]) == 8)

    def fake_drop(messages, cfg, tools=None, temperature=0.2, effort=None):
        return {"choices": [{"message": {"role": "assistant", "content": "好"}}], "effort_dropped": True}
    context.main_complete = fake_drop
    r = c.post("/api/agent/chat", json={"text": "再来"}).json()
    check("模型不支持时对话里提示一句", "不支持思考等级" in r.get("effort_note", ""))

    calls = []

    def fake_tool(messages, cfg, tools=None, temperature=0.2, effort=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"choices": [{"message": {"role": "assistant", "content": "", "thinking_blocks": [{"type": "thinking", "thinking": "x", "signature": "S"}],
                                             "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "get_status", "arguments": "{}"}}]}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "完成"}}]}
    context.main_complete = fake_tool
    c.post("/api/agent/chat", json={"text": "看看状态"})
    am = [m for m in calls[-1] if m.get("role") == "assistant" and m.get("tool_calls")]
    check("工具调用轮：思考块随助手消息带回下一轮", am and am[-1].get("thinking_blocks", [{}])[0].get("signature") == "S")

    print("== 6. 界面")
    js = (ROOT / "web" / "drawers.js").read_text("utf-8")
    st = (ROOT / "web" / "settings.js").read_text("utf-8")
    check("对话抽屉标题栏有思考等级下拉", 'id="chatEffort"' in js and "agent_effort" in js)
    check("设置 → 默认模型 有「思考等级」卡片", "function effortCard" in st and 'sel("llm.agent_effort"' in st)
    check("前后端档位一致", all(f'["{k}"' in st for k in upstream.EFFORTS))

    print(f"\n思考等级结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
