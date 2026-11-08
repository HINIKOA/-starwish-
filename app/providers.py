"""模型服务：服务商（地址 + Key）→ 一键同步模型列表 → 默认模型（对话 / 整理 / 识图 / 向量）。

只填 Key，地址用预设；同步模型后从下拉里选，不用手打模型名。每个服务商和每个模型都能「检测」。
配置里：
  providers: [{id, name, preset, protocol, base_url, api_key, enabled, models: [{id, kind}]}]
  defaults:  {chat, maintain, vision, embed}，值为 "服务商id::模型id"
保存时由 config.apply_providers() 把默认模型映射回 llm / eye / embed 旧字段，其他模块无需改动。
"""
from __future__ import annotations

import re
import time

import httpx

# 预设服务商（地址取各家官方文档的 OpenAI 兼容地址）
PRESETS = [
    {"preset": "deepseek", "name": "深度求索 DeepSeek", "protocol": "openai", "base_url": "https://api.deepseek.com/v1",
     "key_url": "https://platform.deepseek.com/api_keys"},
    {"preset": "dashscope", "name": "阿里云百炼（千问）", "protocol": "openai",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key_url": "https://bailian.console.aliyun.com/"},
    {"preset": "qianwen_maas", "name": "千问 MaaS（Omni 视频）", "protocol": "openai",
     "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1", "key_url": ""},
    {"preset": "zhipu", "name": "智谱开放平台", "protocol": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "key_url": "https://open.bigmodel.cn/usercenter/apikeys"},
    {"preset": "moonshot", "name": "月之暗面 Kimi", "protocol": "openai", "base_url": "https://api.moonshot.cn/v1",
     "key_url": "https://platform.moonshot.cn/console/api-keys"},
    {"preset": "siliconflow", "name": "硅基流动", "protocol": "openai", "base_url": "https://api.siliconflow.cn/v1",
     "key_url": "https://cloud.siliconflow.cn/account/ak"},
    {"preset": "volcengine", "name": "火山方舟（豆包）", "protocol": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "key_url": "https://console.volcengine.com/ark"},
    {"preset": "openai", "name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1",
     "key_url": "https://platform.openai.com/api-keys"},
    {"preset": "anthropic", "name": "Anthropic Claude", "protocol": "anthropic", "base_url": "https://api.anthropic.com",
     "key_url": "https://console.anthropic.com/settings/keys"},
    {"preset": "gemini", "name": "Google Gemini", "protocol": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_url": "https://aistudio.google.com/apikey"},
    {"preset": "openrouter", "name": "OpenRouter", "protocol": "openai", "base_url": "https://openrouter.ai/api/v1",
     "key_url": "https://openrouter.ai/keys"},
    {"preset": "ollama", "name": "Ollama（本机）", "protocol": "openai", "base_url": "http://127.0.0.1:11434/v1", "key_url": ""},
    {"preset": "custom", "name": "自定义（OpenAI 兼容）", "protocol": "openai", "base_url": "", "key_url": ""},
]
PRESET_MAP = {p["preset"]: p for p in PRESETS}

_EMBED = re.compile(r"embed|bge|m3e|text-embedding|gte-|e5-", re.I)
_VISION = re.compile(r"vl|vision|omni|4o|gpt-4\.1|gpt-5|gemini|claude|glm-4v|glm-4\.\dv|qvq|kimi-k2\.5|kimi-latest|"
                     r"llava|pixtral|doubao.*(vision|seed-1\.6|1-5-thinking-vision)|step-1v|internvl|minicpm-v", re.I)
_SKIP = re.compile(r"tts|whisper|asr|audio|rerank|dall-e|image|wanx|flux|stable-diffusion|cogview|cogvideo|sora|"
                   r"moderation|paraformer|sambert|cosyvoice", re.I)


def guess_kind(model_id: str) -> str:
    m = model_id or ""
    if _EMBED.search(m):
        return "embedding"
    if _SKIP.search(m):
        return "other"
    if _VISION.search(m):
        return "vision"
    return "chat"


def norm_base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/completions", "/embeddings", "/models", "/messages"):
        if u.endswith(tail):
            u = u[: -len(tail)]
    return u


def _headers(protocol: str, key: str) -> dict:
    if protocol == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"} if key else {}


def _explain(e: Exception, base: str) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        body = e.response.text[:200]
        hint = {401: "API Key 不对或已失效", 403: "这个 Key 没有权限（或账户欠费 / 未实名）",
                404: "地址不对：大多数服务商地址要以 /v1 结尾", 429: "请求太频繁或额度用完",
                400: "请求被拒绝：模型名可能不对"}.get(code, "服务商返回错误")
        return f"{hint}（HTTP {code}）{(' · ' + body) if body else ''}"
    if isinstance(e, httpx.ConnectError):
        return f"连不上 {base}：检查地址、网络或代理"
    if isinstance(e, httpx.TimeoutException):
        return "连接超时：网络慢或地址不可达"
    return f"{type(e).__name__}: {e}"


def list_models(base_url: str, api_key: str, protocol: str = "openai", timeout: float = 20) -> dict:
    """拉取上游模型列表。地址少了 /v1 时自动补上再试一次。"""
    base = norm_base(base_url)
    if not base:
        raise ValueError("请先填写 API 地址")
    tried = []
    cands = [base]
    if protocol == "anthropic":
        cands = [base + "/v1"] if not base.endswith("/v1") else [base]
    elif not re.search(r"/v\d+[a-z]*(/openai)?$|/api/v\d|compatible-mode/v1|/paas/v4", base):
        cands.append(base + "/v1")
    last = None
    for b in cands:
        tried.append(b)
        try:
            r = httpx.get(b + "/models", headers=_headers(protocol, api_key), timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            data = r.json()
            items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
            out = []
            for it in items or []:
                mid = it.get("id") or it.get("name") if isinstance(it, dict) else str(it)
                if not mid:
                    continue
                mid = mid.split("/", 1)[1] if mid.startswith("models/") else mid
                out.append({"id": mid, "kind": guess_kind(mid)})
            out.sort(key=lambda m: ({"chat": 0, "vision": 1, "embedding": 2}.get(m["kind"], 3), m["id"]))
            res = {"ok": True, "models": out, "base_url": b}
            if b != base:
                res["fixed_base_url"] = b
            return res
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(_explain(last, tried[-1]))


def check(base_url: str, api_key: str, protocol: str = "openai", model: str = "", kind: str = "",
          timeout: float = 30) -> dict:
    """检测：没给模型 → 只验证 Key（拉模型列表）；给了模型 → 真的发一句话（向量模型发一条 embeddings）。"""
    base = norm_base(base_url)
    if not model:
        r = list_models(base, api_key, protocol, timeout=timeout)
        return {"ok": True, "msg": f"连接正常，Key 有效，可用模型 {len(r['models'])} 个",
                "fixed_base_url": r.get("fixed_base_url", "")}
    kind = kind or guess_kind(model)
    t0 = time.time()
    try:
        if kind == "embedding":
            r = httpx.post(base + "/embeddings", headers=_headers(protocol, api_key),
                           json={"model": model, "input": ["星愿记忆连通性检测"]}, timeout=timeout)
            r.raise_for_status()
            dim = len(r.json()["data"][0]["embedding"])
            return {"ok": True, "ms": int((time.time() - t0) * 1000), "msg": f"向量模型可用：{dim} 维"}
        if protocol == "anthropic":
            url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
            r = httpx.post(url, headers=_headers(protocol, api_key), timeout=timeout,
                           json={"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "回复：好"}]})
            r.raise_for_status()
            text = "".join(c.get("text", "") for c in r.json().get("content", []))
        else:
            r = httpx.post(base + "/chat/completions", headers=_headers(protocol, api_key), timeout=timeout,
                           json={"model": model, "max_tokens": 16, "stream": False,
                                 "messages": [{"role": "user", "content": "回复：好"}]})
            r.raise_for_status()
            msg = (r.json().get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content") or msg.get("reasoning_content") or ""
        ms = int((time.time() - t0) * 1000)
        return {"ok": True, "ms": ms, "reply": (text or "").strip()[:40], "msg": f"模型可用，{ms} 毫秒"}
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_explain(e, base)) from None


def split_ref(ref: str) -> tuple[str, str]:
    if not ref or "::" not in ref:
        return "", ""
    a, b = ref.split("::", 1)
    return a, b


def find(cfg: dict, pid: str) -> dict | None:
    for p in cfg.get("providers") or []:
        if p.get("id") == pid:
            return p
    return None


def resolve(cfg: dict, role: str) -> dict | None:
    """默认模型 → {base_url, api_key, protocol, model}；服务商被关闭或不存在时返回 None。"""
    pid, model = split_ref((cfg.get("defaults") or {}).get(role, ""))
    p = find(cfg, pid)
    if not p or not p.get("enabled", True) or not model:
        return None
    return {"base_url": norm_base(p.get("base_url", "")), "api_key": p.get("api_key", ""),
            "protocol": p.get("protocol", "openai"), "model": model, "provider": p.get("name", pid)}
