"""上游协议适配：内部统一用 OpenAI Chat Completions 格式，按服务商协议转换。

协议：
  openai     OpenAI 兼容  POST {base}/chat/completions        （原样转发）
  anthropic  Anthropic    POST {base}/v1/messages             （x-api-key）
  responses  OpenAI Responses  POST {base}/responses          （Bearer）

提供：
  build(protocol, base, key, body)  -> (url, headers, payload)
  parse(protocol, data, model)      -> chat.completion 字典
  StreamTranslator(protocol, model) -> 把上游 SSE 转成 chat.completion.chunk
  complete(...)                     -> 同步调用，返回 chat.completion（后台整理 / 检测用）
支持：文本、图片（image_url / data URL）、system、工具调用（tools / tool_calls / tool 结果）、
      temperature / top_p / max_tokens / stop、流式、用量统计、思考内容（reasoning_content）。
"""
from __future__ import annotations

import json
import time
import uuid

import httpx

PROTOCOLS = ("openai", "anthropic", "responses")
NAMES = {"openai": "OpenAI 兼容（Chat Completions）", "anthropic": "Anthropic（Messages）",
         "responses": "OpenAI Responses"}
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096

# ---------------- 思考等级（和 GPT / Claude Opus 的档位一致） ----------------
# default = 不传，用模型自己的默认；none = 不思考（GPT-5.1+）；minimal（GPT-5）；
# low / medium / high（两家都有）；xhigh（GPT-5.2+ / Claude Opus 4.7+）；max（GPT-5.6+ / Claude Opus 4.6+）。
EFFORTS = ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
EFFORT_LABELS = {"default": "默认（模型自己决定）", "none": "关闭（none）", "minimal": "最少（minimal）", "low": "低（low）",
                 "medium": "中（medium）", "high": "高（high）", "xhigh": "超高（xhigh）", "max": "最高（max）"}
# Anthropic 档位只有 low~max：none / minimal 落到 low（新模型的自适应思考关不掉，low 是最省的一档）
_A_EFFORT = {"none": "low", "minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
# 思考也算在 max_tokens 里：高档位给足输出上限（官方建议 xhigh / max 从 64k 起）
_A_MIN_TOKENS = {"medium": 16000, "high": 32000, "xhigh": 64000, "max": 64000}
_EFFORT_ERR = ("reasoning", "effort", "output_config", "thinking", "unrecognized", "unknown", "extra inputs",
               "not support", "unsupported", "invalid param", "max_tokens", "max_output_tokens")


def norm_effort(v) -> str:
    v = str(v or "").strip().lower()
    return v if v in EFFORTS else "default"


def _effort_of(body: dict) -> str:
    e = body.get("reasoning_effort")
    if isinstance(e, dict):           # 有人按 Responses 的写法传进来
        e = e.get("effort")
    return norm_effort(e)


def norm(protocol: str | None) -> str:
    p = (protocol or "openai").strip().lower()
    return p if p in PROTOCOLS else "openai"


def endpoint(protocol: str, base: str) -> str:
    base = (base or "").rstrip("/")
    p = norm(protocol)
    if p == "anthropic":
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    if p == "responses":
        return base + "/responses"
    return base + "/chat/completions"


def headers(protocol: str, key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if norm(protocol) == "anthropic":
        if key:
            h["x-api-key"] = key
        h["anthropic-version"] = ANTHROPIC_VERSION
    elif key:
        h["Authorization"] = f"Bearer {key}"
    return h


# ---------------- 通用小工具 ----------------
def _text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for p in content:
        if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"):
            out.append(p.get("text", ""))
        elif isinstance(p, str):
            out.append(p)
    return "".join(out)


def _image_url(part: dict) -> str:
    iu = part.get("image_url")
    return iu.get("url", "") if isinstance(iu, dict) else (iu or "")


def _split_data_url(u: str) -> tuple[str, str]:
    head, _, data = u.partition(",")
    mt = head[5:].split(";")[0] if head.startswith("data:") else "image/jpeg"
    return mt or "image/jpeg", data


def _args_obj(s) -> dict:
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"_raw": s}


def _new_id(prefix="chatcmpl-") -> str:
    return prefix + uuid.uuid4().hex[:24]


# ---------------- 请求：Chat Completions → Anthropic ----------------
def _to_anthropic(body: dict) -> dict:
    system, msgs = [], []

    def push(role, blocks):
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"].extend(blocks)
        else:
            msgs.append({"role": role, "content": list(blocks)})

    for m in body.get("messages") or []:
        role, c = m.get("role"), m.get("content")
        if role in ("system", "developer"):
            t = _text_of(c)
            if t:
                system.append(t)
            continue
        if role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                           "content": _text_of(c) or ""}])
            continue
        blocks = []
        if isinstance(c, str):
            if c:
                blocks.append({"type": "text", "text": c})
        else:
            for p in c or []:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text" and p.get("text"):
                    blocks.append({"type": "text", "text": p["text"]})
                elif p.get("type") == "image_url":
                    u = _image_url(p)
                    if u.startswith("data:"):
                        mt, data = _split_data_url(u)
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
                    elif u:
                        blocks.append({"type": "image", "source": {"type": "url", "url": u}})
        if role == "assistant":
            tb = [x for x in (m.get("thinking_blocks") or []) if isinstance(x, dict)]
            if tb:
                blocks = tb + blocks
            for tc in m.get("tool_calls") or []:
                f = tc.get("function") or {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or _new_id("toolu_"), "name": f.get("name", ""),
                               "input": _args_obj(f.get("arguments"))})
            if blocks:
                push("assistant", blocks)
        else:
            push("user", blocks or [{"type": "text", "text": " "}])
    if msgs and msgs[0]["role"] != "user":   # Anthropic 要求第一条是 user
        msgs.insert(0, {"role": "user", "content": [{"type": "text", "text": "（继续）"}]})
    out = {"model": body.get("model"), "messages": msgs,
           "max_tokens": int(body.get("max_tokens") or body.get("max_completion_tokens") or DEFAULT_MAX_TOKENS)}
    if system:
        out["system"] = "\n\n".join(system)
    eff = _effort_of(body)
    if eff != "default":
        ae = _A_EFFORT[eff]
        out["output_config"] = {"effort": ae}
        if not (body.get("max_tokens") or body.get("max_completion_tokens")):
            out["max_tokens"] = max(out["max_tokens"], _A_MIN_TOKENS.get(ae, out["max_tokens"]))
    elif body.get("temperature") is not None:     # Anthropic 只接受 0~1；开了思考等级就不传温度（思考时不允许改温度）
        out["temperature"] = max(0.0, min(1.0, float(body["temperature"])))
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("stop"):
        out["stop_sequences"] = [body["stop"]] if isinstance(body["stop"], str) else list(body["stop"])
    if body.get("stream"):
        out["stream"] = True
    tools = body.get("tools") or []
    choice = body.get("tool_choice")
    if tools and choice != "none":
        out["tools"] = [{"name": (t.get("function") or {}).get("name", ""),
                         "description": (t.get("function") or {}).get("description", ""),
                         "input_schema": (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}}
                        for t in tools if t.get("type", "function") == "function"]
        if choice == "required":
            out["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict):
            out["tool_choice"] = {"type": "tool", "name": (choice.get("function") or {}).get("name", "")}
    return out


# ---------------- 请求：Chat Completions → Responses ----------------
def _to_responses(body: dict) -> dict:
    instr, items = [], []
    for m in body.get("messages") or []:
        role, c = m.get("role"), m.get("content")
        if role in ("system", "developer"):
            t = _text_of(c)
            if t:
                instr.append(t)
            continue
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""), "output": _text_of(c)})
            continue
        if role == "assistant":
            t = _text_of(c)
            if t:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": t}]})
            for tc in m.get("tool_calls") or []:
                f = tc.get("function") or {}
                args = f.get("arguments")
                items.append({"type": "function_call", "call_id": tc.get("id") or _new_id("call_"),
                              "name": f.get("name", ""),
                              "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)})
            continue
        parts = []
        if isinstance(c, str):
            parts.append({"type": "input_text", "text": c})
        else:
            for p in c or []:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append({"type": "input_text", "text": p.get("text", "")})
                elif p.get("type") == "image_url" and _image_url(p):
                    parts.append({"type": "input_image", "image_url": _image_url(p)})
        items.append({"role": "user", "content": parts or [{"type": "input_text", "text": " "}]})
    out = {"model": body.get("model"), "input": items, "store": False}
    if instr:
        out["instructions"] = "\n\n".join(instr)
    mt = body.get("max_tokens") or body.get("max_completion_tokens")
    if mt:
        out["max_output_tokens"] = int(mt)
    eff = _effort_of(body)
    if eff != "default":
        out["reasoning"] = {"effort": eff}
    for k in ("temperature", "top_p"):
        if body.get(k) is not None and eff in ("default", "none"):   # 推理模型思考时不接受温度
            out[k] = body[k]
    if body.get("stream"):
        out["stream"] = True
    tools = body.get("tools") or []
    if tools:
        out["tools"] = [{"type": "function", "name": (t.get("function") or {}).get("name", ""),
                         "description": (t.get("function") or {}).get("description", ""),
                         "parameters": (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}}
                        for t in tools if t.get("type", "function") == "function"]
        ch = body.get("tool_choice")
        if ch in ("auto", "required", "none"):
            out["tool_choice"] = ch
        elif isinstance(ch, dict):
            out["tool_choice"] = {"type": "function", "name": (ch.get("function") or {}).get("name", "")}
    return out


def build(protocol: str, base: str, key: str, body: dict) -> tuple[str, dict, dict]:
    p = norm(protocol)
    if p == "anthropic":
        payload = _to_anthropic(body)
    elif p == "responses":
        payload = _to_responses(body)
    else:
        payload = body   # 不需要改时原样转发（同一个对象）
        eff = _effort_of(body)
        raw = body.get("reasoning_effort")
        if (raw is not None and (eff != "default" or str(raw).strip().lower() == "default" or isinstance(raw, dict))) \
                or any(isinstance(m, dict) and "thinking_blocks" in m for m in body.get("messages") or []):
            payload = dict(body)
        if any("thinking_blocks" in m for m in body.get("messages") or [] if isinstance(m, dict)):
            payload["messages"] = [{k: v for k, v in m.items() if k != "thinking_blocks"} for m in body["messages"]]
        eff = _effort_of(body)
        raw = body.get("reasoning_effort")
        if raw is not None and (eff != "default" or str(raw).strip().lower() == "default" or isinstance(raw, dict)):
            payload.pop("reasoning_effort", None)   # 认得的档位统一成字符串；不认得的原样透传（代理场景）
        if eff != "default":
            payload["reasoning_effort"] = eff
            if eff != "none":
                payload.pop("temperature", None)   # GPT-5 类推理模型思考时不接受温度
                payload.pop("top_p", None)
    return endpoint(p, base), headers(p, key), payload


# ---------------- 回应：→ chat.completion ----------------
_A_STOP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
           "pause_turn": "stop", "refusal": "content_filter"}


def parse(protocol: str, data: dict, model: str = "") -> dict:
    p = norm(protocol)
    if p == "openai":
        return data
    text, reasoning, calls, finish, usage, tblocks = [], [], [], "stop", {}, []
    if p == "anthropic":
        for b in data.get("content") or []:
            t = b.get("type")
            if t == "text":
                text.append(b.get("text", ""))
            elif t == "thinking":
                reasoning.append(b.get("thinking", ""))
                tblocks.append(b)
            elif t == "redacted_thinking":
                tblocks.append(b)
            elif t == "tool_use":
                calls.append({"id": b.get("id"), "type": "function",
                              "function": {"name": b.get("name", ""),
                                           "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
        finish = _A_STOP.get(data.get("stop_reason") or "end_turn", "stop")
        u = data.get("usage") or {}
        usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)}
    else:  # responses
        for it in data.get("output") or []:
            t = it.get("type")
            if t == "message":
                for c in it.get("content") or []:
                    if c.get("type") in ("output_text", "text"):
                        text.append(c.get("text", ""))
                    elif c.get("type") == "refusal":
                        text.append(c.get("refusal", ""))
            elif t == "function_call":
                calls.append({"id": it.get("call_id") or it.get("id"), "type": "function",
                              "function": {"name": it.get("name", ""), "arguments": it.get("arguments") or "{}"}})
            elif t == "reasoning":
                for s in it.get("summary") or []:
                    reasoning.append(s.get("text", ""))
        if not text and isinstance(data.get("output_text"), str):
            text.append(data["output_text"])
        if calls:
            finish = "tool_calls"
        elif data.get("status") == "incomplete":
            finish = "length" if (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens" else "stop"
        u = data.get("usage") or {}
        usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)}
    usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    msg: dict = {"role": "assistant", "content": "".join(text) if (text or not calls) else None}
    if calls:
        msg["tool_calls"] = calls
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tblocks and calls:   # 思考 + 工具调用：下一轮要把带签名的思考块原样传回（Anthropic 要求）
        msg["thinking_blocks"] = tblocks
    return {"id": data.get("id") or _new_id(), "object": "chat.completion", "created": int(time.time()),
            "model": data.get("model") or model, "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": usage}


def error_message(protocol: str, data) -> str:
    """把上游错误体里的说明取出来。"""
    try:
        if isinstance(data, (bytes, str)):
            data = json.loads(data)
        e = data.get("error") if isinstance(data, dict) else None
        if isinstance(e, dict):
            return e.get("message") or json.dumps(e, ensure_ascii=False)
        if isinstance(e, str):
            return e
        return (data or {}).get("message", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


# ---------------- 流式：上游 SSE → chat.completion.chunk ----------------
class StreamTranslator:
    """feed(原始字节) -> 若干 SSE 文本块（'data: {...}\\n\\n'）；结束时 finish()。
    同时累积回复文本到 .text，供写入归档。openai 协议原样透传。"""

    def __init__(self, protocol: str, model: str = ""):
        self.p = norm(protocol)
        self.model = model
        self.id = _new_id()
        self.created = int(time.time())
        self.buf = b""
        self.text_parts: list[str] = []
        self.usage: dict = {}
        self.finish_reason: str | None = None
        self.tools: dict = {}          # 上游块序号 -> chat 的 tool_calls 序号
        self.started = False
        self.done = False

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    # 生成一个 chunk
    def _chunk(self, delta: dict | None = None, finish: str | None = None, usage: dict | None = None) -> str:
        ch = {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
              "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
        if usage is not None:
            ch["usage"] = usage
        return "data: " + json.dumps(ch, ensure_ascii=False) + "\n\n"

    def _role(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        return [self._chunk({"role": "assistant", "content": ""})]

    def feed(self, data: bytes) -> list[str]:
        if self.p == "openai":
            self.buf += data
            self._scan_openai()
            return [data.decode("utf-8", "replace")]
        self.buf += data
        out: list[str] = []
        while b"\n\n" in self.buf or b"\r\n\r\n" in self.buf:
            i = self.buf.find(b"\n\n")
            j = self.buf.find(b"\r\n\r\n")
            if j >= 0 and (i < 0 or j < i):
                block, self.buf = self.buf[:j], self.buf[j + 4:]
            else:
                block, self.buf = self.buf[:i], self.buf[i + 2:]
            out += self._block(block.decode("utf-8", "replace"))
        return out

    def _scan_openai(self):
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            s = line.decode("utf-8", "replace").strip()
            if not s.startswith("data:"):
                continue
            s = s[5:].strip()
            if s == "[DONE]":
                continue
            try:
                d = json.loads(s)
                for c in d.get("choices") or []:
                    t = (c.get("delta") or {}).get("content")
                    if isinstance(t, str):
                        self.text_parts.append(t)
            except Exception:
                pass

    def _block(self, block: str) -> list[str]:
        data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
        if not data or data == "[DONE]":
            return []
        try:
            obj = json.loads(data)
        except Exception:
            return []
        return self.event(obj)

    def event(self, obj: dict) -> list[str]:
        """处理一个已解析的上游事件（宇宙之眼也直接用这个）。"""
        return self._anthropic(obj) if self.p == "anthropic" else self._responses(obj)

    def _text(self, t: str) -> list[str]:
        if not t:
            return []
        self.text_parts.append(t)
        return self._role() + [self._chunk({"content": t})]

    def _anthropic(self, o: dict) -> list[str]:
        t = o.get("type")
        if t == "message_start":
            m = o.get("message") or {}
            self.model = m.get("model") or self.model
            self.usage["prompt_tokens"] = (m.get("usage") or {}).get("input_tokens", 0)
            return self._role()
        if t == "content_block_start":
            b = o.get("content_block") or {}
            if b.get("type") == "tool_use":
                n = len(self.tools)
                self.tools[o.get("index", n)] = n
                return self._role() + [self._chunk({"tool_calls": [{"index": n, "id": b.get("id"), "type": "function",
                                                                   "function": {"name": b.get("name", ""), "arguments": ""}}]})]
            if b.get("type") == "text" and b.get("text"):
                return self._text(b["text"])
            return []
        if t == "content_block_delta":
            d = o.get("delta") or {}
            if d.get("type") == "text_delta":
                return self._text(d.get("text", ""))
            if d.get("type") == "thinking_delta" and d.get("thinking"):
                return self._role() + [self._chunk({"reasoning_content": d["thinking"]})]
            if d.get("type") == "input_json_delta":
                n = self.tools.get(o.get("index"))
                if n is not None and d.get("partial_json"):
                    return [self._chunk({"tool_calls": [{"index": n, "function": {"arguments": d["partial_json"]}}]})]
            return []
        if t == "message_delta":
            self.finish_reason = _A_STOP.get((o.get("delta") or {}).get("stop_reason") or "", self.finish_reason)
            u = o.get("usage") or {}
            if "output_tokens" in u:
                self.usage["completion_tokens"] = u["output_tokens"]
            return []
        if t == "message_stop":
            return self.finish()
        if t == "error":
            return self._error((o.get("error") or {}).get("message", "上游出错"))
        return []

    def _responses(self, o: dict) -> list[str]:
        t = o.get("type", "")
        if t == "response.created":
            self.model = (o.get("response") or {}).get("model") or self.model
            return self._role()
        if t == "response.output_text.delta":
            return self._text(o.get("delta", ""))
        if t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta") and o.get("delta"):
            return self._role() + [self._chunk({"reasoning_content": o["delta"]})]
        if t == "response.output_item.added":
            it = o.get("item") or {}
            if it.get("type") == "function_call":
                n = len(self.tools)
                self.tools[o.get("output_index", n)] = n
                return self._role() + [self._chunk({"tool_calls": [{"index": n, "id": it.get("call_id") or it.get("id"),
                                                                   "type": "function",
                                                                   "function": {"name": it.get("name", ""), "arguments": ""}}]})]
            return []
        if t == "response.function_call_arguments.delta":
            n = self.tools.get(o.get("output_index"))
            if n is not None and o.get("delta"):
                return [self._chunk({"tool_calls": [{"index": n, "function": {"arguments": o["delta"]}}]})]
            return []
        if t in ("response.completed", "response.incomplete"):
            r = o.get("response") or {}
            u = r.get("usage") or {}
            self.usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)}
            if self.tools:
                self.finish_reason = "tool_calls"
            elif t == "response.incomplete":
                self.finish_reason = "length"
            return self.finish()
        if t in ("response.failed", "error"):
            e = (o.get("response") or {}).get("error") or o.get("error") or o
            return self._error(e.get("message", "上游出错") if isinstance(e, dict) else str(e))
        return []

    def _error(self, msg: str) -> list[str]:
        if self.done:
            return []
        self.done = True
        err = {"error": {"message": msg, "type": "upstream_error"}}
        return ["data: " + json.dumps(err, ensure_ascii=False) + "\n\n", "data: [DONE]\n\n"]

    def finish(self) -> list[str]:
        if self.done or self.p == "openai":
            return []
        self.done = True
        u = dict(self.usage)
        u["total_tokens"] = u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
        return self._role() + [self._chunk({}, self.finish_reason or "stop", u), "data: [DONE]\n\n"]


# ---------------- 同步调用（后台整理 / 检测） ----------------
def _effort_rejected(r) -> bool:
    if r.status_code not in (400, 422):
        return False
    t = r.text.lower()
    return any(k in t for k in _EFFORT_ERR)


def complete(protocol: str, base: str, key: str, body: dict, timeout: float = 120) -> dict:
    """同步调用。设了思考等级但模型 / 服务商不认（400 且报错里提到相关参数）时，自动去掉思考等级重试一次，
    返回里带 `effort_dropped: true`，调用方可以提示主人。"""
    body = dict(body, stream=False)
    url, h, payload = build(protocol, base, key, body)
    r = httpx.post(url, json=payload, headers=h, timeout=timeout, follow_redirects=True)
    dropped = False
    if _effort_of(body) != "default" and _effort_rejected(r):
        body = {k: v for k, v in body.items() if k != "reasoning_effort"}
        url, h, payload = build(protocol, base, key, body)
        r = httpx.post(url, json=payload, headers=h, timeout=timeout, follow_redirects=True)
        dropped = True
    r.raise_for_status()
    out = parse(protocol, r.json(), body.get("model", ""))
    if dropped:
        out["effort_dropped"] = True
    return out


def reply_text(data: dict) -> str:
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return msg.get("content") or ""
