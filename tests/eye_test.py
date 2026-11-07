"""星魂 SoulStar · 宇宙之眼测试。

用法：python tests/eye_test.py
在临时灵魂包里启动真实服务器 + 模拟上游（OpenAI 兼容 / Anthropic 兼容 / 转写），验证：
页面与按键、引擎解析与千问预留接口、能力预检、流式问答、图片不进历史、主动吐槽限速、预算、
视频格式自动退回多图、直接出语音、Anthropic 协议、转写、密钥不外泄、仅主人、结束沉淀成星星。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = ",".join(x for x in [os.environ.get(_k, ""), "127.0.0.1", "localhost"] if x)

import httpx  # noqa: E402
from smoke_test import check, free_port, wait_up, RESULTS  # noqa: E402

JPG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()


def start_mock(port):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    import uvicorn

    app = FastAPI()
    st = app.state
    st.bodies, st.reject_video, st.audio, st.anth = [], False, False, []

    def has_image(content):
        return isinstance(content, list) and any(p.get("type") in ("image_url", "video", "image") for p in content)

    @app.get("/v1/models")
    def models():
        return {"data": [{"id": "mock"}]}

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        st.bodies.append(body)
        last = body["messages"][-1]["content"]
        if st.reject_video and isinstance(last, list) and any(p.get("type") == "video" for p in last):
            return JSONResponse({"error": {"message": "video not supported"}}, status_code=400)
        sysmsg = body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else ""
        if "图像检测器" in sysmsg:
            reply = "红色" if body["model"] != "blind-model" else "我不太确定呢"
        elif not body.get("stream"):
            reply = "我和主人一起看了测试电影，主人在飞船起飞时很激动。"
            return {"choices": [{"message": {"role": "assistant", "content": reply}}]}
        elif "主动开口" in sysmsg:
            reply = "哇，这个镜头切得真漂亮！"
        else:
            reply = "看到了，他们正在飞船里。" if has_image(last) else "没看到图。"

        async def gen():
            for i in range(0, len(reply), 4):
                d = {"content": reply[i:i + 4]}
                if st.audio and i == 0:
                    d["audio"] = {"data": base64.b64encode(b"\x00\x01" * 8).decode()}
                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": d}]}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 20}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/anthropic/v1/messages")
    async def anth(req: Request):
        body = await req.json()
        st.anth.append({"body": body, "key": req.headers.get("x-api-key"), "ver": req.headers.get("anthropic-version")})
        reply = "（Anthropic）画面里有一艘飞船。"

        async def gen():
            yield "event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 700}}}) + "\n\n"
            for i in range(0, len(reply), 5):
                yield "event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 0,
                                                                          "delta": {"type": "text_delta", "text": reply[i:i + 5]}}, ensure_ascii=False) + "\n\n"
            yield "event: message_delta\ndata: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": 15}}) + "\n\n"
            yield "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/audio/transcriptions")
    async def stt(req: Request):
        form = await req.form()
        st.stt_model = form.get("model")
        return {"text": "我们必须回去"}

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    return app


def sse(c, body, headers=None):
    events, ctype = [], ""
    with c.stream("POST", "/api/eye/ask", json=body, headers=headers or {}) as r:
        ctype = r.headers.get("content-type", "")
        if "event-stream" not in ctype:
            r.read()
            return r.status_code, r.json(), []
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return 200, None, events


def main():
    tmp = Path(tempfile.mkdtemp(prefix="soulstar-eye-"))
    soul = tmp / "soul"
    env = {**os.environ, "SOULSTAR_SOUL_DIR": str(soul), "PYTHONIOENCODING": "utf-8"}
    up, port = free_port(), free_port()
    mock = start_mock(up)
    srv = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--no-browser", "--port", str(port)],
                           env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    B, U = f"http://127.0.0.1:{port}", f"http://127.0.0.1:{up}"
    c = httpx.Client(base_url=B, timeout=60)
    try:
        print("== 宇宙之眼 · 启动")
        check("服务器启动", wait_up(B + "/api/health"))
        check("模拟上游启动", wait_up(U + "/v1/models"))
        from app import __version__
        check("版本号 ≥ 0.4.0 且与服务一致", tuple(map(int, __version__.split("."))) >= (0, 4, 0) and c.get("/api/health").json()["version"] == __version__, __version__)

        print("== 页面与按键")
        pg = c.get("/eye")
        check("/eye 页面可打开", pg.status_code == 200 and "宇宙之眼" in pg.text and "eye.js" in pg.text)
        check("星图顶栏有「👁 宇宙之眼」按键", 'data-panel="eye"' in c.get("/").text and '"/eye"' in c.get("/static/app.js").text)
        js = c.get("/static/eye.js").text
        check("前端：屏幕 / 摄像头采集 + 变化检测 + 拼分镜图", all(k in js for k in ("getDisplayMedia", "getUserMedia", "sampleOnce", "composeGrid")))
        check("前端：语音打断与回声守卫、红色正在看标记", all(k in js for k in ("stopSpeaking", "回声守卫", "liveBadge")))

        print("== 引擎解析（含千问预留接口）")
        s0 = c.get("/api/eye/status").json()
        q = s0["engines"]["qwen_omni"]
        check("千问默认 OpenAI 兼容地址", q["base_url"] == "https://maas.qianwenaiapi.com/compatible-mode/v1", q["base_url"])
        check("千问默认模型 qwen3.8-omni-flash，未填 Key 时未就绪", q["model"] == "qwen3.8-omni-flash" and not q["ready"])
        check("千问 Anthropic 兼容地址已预置", s0["settings"]["qwen_base_url_anthropic"] == "https://maas.qianwenaiapi.com/apps/anthropic")
        check("通用帧模式：未配置时未就绪", not s0["engines"]["frame"]["ready"])
        c.put("/api/settings", json={"llm": {"base_url": U + "/v1", "api_key": "sk-mock-llm-123456", "model": "mock-vl"}})
        s1 = c.get("/api/eye/status").json()
        check("通用帧模式：留空时沿用 LLM 维护的地址 / Key / 模型", s1["engines"]["frame"]["ready"] and s1["engines"]["frame"]["model"] == "mock-vl")
        check("状态接口不返回明文 Key", "sk-mock-llm" not in json.dumps(s1, ensure_ascii=False))

        print("== 能力预检")
        pf = c.post("/api/eye/preflight", json={"engine": "frame"}).json()
        check("能看图的模型预检通过", pf["ok"] and "红" in pf["reply"], str(pf))
        probe = mock.state.bodies[-1]["messages"][-1]["content"]
        check("预检发的是一张图片", any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/png") for p in probe))
        c.put("/api/settings", json={"eye": {"frame_base_url": U + "/v1", "frame_api_key": "sk-blind-000000", "frame_model": "blind-model"}})
        pf2 = c.post("/api/eye/preflight", json={"engine": "frame"}).json()
        check("不会看图的模型预检失败并说明原因", (not pf2["ok"]) and "不支持读图" in pf2["reason"], str(pf2))
        check("预检结果缓存在状态里", len(c.get("/api/eye/status").json()["preflight"]) >= 2)
        c.put("/api/settings", json={"eye": {"frame_base_url": "", "frame_api_key": "", "frame_model": ""}})

        print("== 观看会话与流式问答")
        c.put("/api/core", json={"section": "owner", "content": "主人叫九天", "confirm": True})
        S = c.post("/api/eye/session/start", json={"title": "测试电影", "source": "screen"}).json()
        sid = S["id"]
        check("开始观看", S["title"] == "测试电影" and S["engine"] == "frame")
        c.post(f"/api/eye/session/{sid}/note", json={"kind": "subtitle", "text": "我们必须回去"})
        code, js_, ev = sse(c, {"session": sid, "text": "他们在干嘛？", "frames": [JPG], "mode": "user"})
        kinds = [e["type"] for e in ev]
        text = "".join(e.get("text", "") for e in ev if e["type"] == "delta")
        check("流式返回 start → delta → done", kinds[0] == "start" and "delta" in kinds and kinds[-1] == "done", str(kinds))
        check("回答内容正确", text == "看到了，他们正在飞船里。", text)
        done = ev[-1]
        check("done 带首字耗时与用量", done["tokens"] == 920 and done["first_ms"] is not None and done["today"]["requests"] >= 1, str(done))
        b1 = mock.state.bodies[-1]
        sysm = b1["messages"][0]["content"]
        check("系统提示带核心记忆 + 陪看规则", "九天" in sysm and "宇宙之眼" in sysm and "测试电影" in sysm)
        um = b1["messages"][-1]["content"]
        check("这一轮带上了图片和最近台词", sum(p["type"] == "image_url" for p in um) == 1 and "我们必须回去" in um[-1]["text"])
        check("请求是流式且要求返回用量", b1["stream"] and b1["stream_options"]["include_usage"])
        sse(c, {"session": sid, "text": "那现在呢？", "frames": [JPG, JPG], "mode": "user"})
        b2 = mock.state.bodies[-1]
        hist = b2["messages"][1:-1]
        check("第二轮带上了上一轮的文字对话", [m["role"] for m in hist] == ["user", "assistant"] and hist[0]["content"] == "他们在干嘛？")
        check("图片永不进历史（只在当次请求里）", all(isinstance(m["content"], str) for m in hist) and
              sum(p["type"] == "image_url" for p in b2["messages"][-1]["content"]) == 2)

        print("== 主动吐槽限速与预算")
        code, d, _ = sse(c, {"session": sid, "frames": [JPG], "mode": "auto"})
        check("刚说完话时主动吐槽被限速跳过", code == 200 and d["skipped"] and d["wait"] > 0, str(d))
        c.put("/api/settings", json={"eye": {"pace": "off"}})
        code, d, _ = sse(c, {"session": sid, "frames": [JPG], "mode": "auto"})
        check("节奏设为「不主动说话」时不吐槽", d and d["skipped"])
        c.put("/api/settings", json={"eye": {"pace": "normal"}})
        from app.eye import session as es
        os.environ["SOULSTAR_SOUL_DIR"] = str(soul)
        check("限速规则：上一句越长等越久", es.PACE_GAP["calm"] > es.PACE_GAP["normal"] > es.PACE_GAP["chatty"])
        used = c.get("/api/eye/status").json()["usage"]["requests"]
        c.put("/api/settings", json={"eye": {"daily_max_requests": used}})
        code, d, _ = sse(c, {"session": sid, "text": "还在吗", "frames": [JPG]})
        check("每日请求上限生效（429）", code == 429 and "用完" in d["detail"], str(d))
        c.put("/api/settings", json={"eye": {"daily_max_requests": 0}})
        code, d, _ = sse(c, {"session": sid, "text": "", "frames": [JPG], "mode": "user"})
        check("空问题被拒绝", code == 400)

        print("== 千问 Omni 视频接口（OpenAI 兼容）")
        c.put("/api/settings", json={"eye": {"engine": "qwen_omni", "qwen_api_key": "sk-qwen-abcdef123456",
                                             "qwen_base_url_openai": U + "/v1"}})
        st2 = c.get("/api/eye/status").json()
        check("填上 Key 后千问引擎就绪", st2["engines"]["qwen_omni"]["ready"] and st2["engine"] == "qwen_omni")
        check("千问 Key 不外泄", "sk-qwen-abcdef" not in json.dumps(st2) and "sk-qwen-abcdef" not in c.get("/api/settings").text)
        S2 = c.post("/api/eye/session/start", json={"title": "千问测试", "source": "camera"}).json()
        sse(c, {"session": S2["id"], "text": "看看这段", "frames": [JPG] * 5})
        bq = mock.state.bodies[-1]
        vparts = [p for p in bq["messages"][-1]["content"] if p["type"] == "video"]
        check("≥4 帧时按「视频」传给千问", bq["model"] == "qwen3.8-omni-flash" and len(vparts) == 1 and len(vparts[0]["video"]) == 5)
        sse(c, {"session": S2["id"], "text": "两帧呢", "frames": [JPG] * 2})
        check("不足 4 帧时按多图传", sum(p["type"] == "image_url" for p in mock.state.bodies[-1]["messages"][-1]["content"]) == 2)
        mock.state.reject_video = True
        n0 = len(mock.state.bodies)
        _, _, evq = sse(c, {"session": S2["id"], "text": "再看看", "frames": [JPG] * 4})
        mock.state.reject_video = False
        retry = mock.state.bodies[-1]["messages"][-1]["content"]
        check("上游不认视频格式时自动退回多图", len(mock.state.bodies) - n0 == 2 and sum(p["type"] == "image_url" for p in retry) == 4
              and any(e["type"] == "info" for e in evq) and evq[-1]["type"] == "done")
        c.put("/api/settings", json={"eye": {"qwen_audio_out": True, "qwen_voice": "Cherry"}})
        mock.state.audio = True
        _, _, eva = sse(c, {"session": S2["id"], "text": "说句话", "frames": [JPG]})
        mock.state.audio = False
        ba = mock.state.bodies[-1]
        check("直接出语音：请求带 modalities 与音色", ba.get("modalities") == ["text", "audio"] and ba["audio"]["voice"] == "Cherry")
        check("直接出语音：音频分片转发给浏览器", any(e["type"] == "audio" for e in eva))
        c.put("/api/settings", json={"eye": {"qwen_audio_out": False}})

        print("== 千问 Omni（Anthropic 兼容）")
        c.put("/api/settings", json={"eye": {"qwen_protocol": "anthropic", "qwen_base_url_anthropic": U + "/anthropic"}})
        _, _, evn = sse(c, {"session": S2["id"], "text": "Anthropic 协议看得到吗", "frames": [JPG, JPG]})
        an = mock.state.anth[-1]
        ab = an["body"]
        imgs = [p for p in ab["messages"][-1]["content"] if p["type"] == "image"]
        check("Anthropic 协议：请求 /v1/messages，带 x-api-key 与版本头", an["key"] == "sk-qwen-abcdef123456" and an["ver"] == "2023-06-01")
        check("Anthropic 协议：图片按 base64 块传、system 单独放", len(imgs) == 2 and imgs[0]["source"]["type"] == "base64"
              and isinstance(ab["system"], str) and "宇宙之眼" in ab["system"])
        roles = [m["role"] for m in ab["messages"]]
        check("Anthropic 协议：历史 user / assistant 交替", roles[0] == "user" and all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), str(roles))
        check("Anthropic 协议：流式回答与用量", "".join(e.get("text", "") for e in evn if e["type"] == "delta") == "（Anthropic）画面里有一艘飞船。"
              and evn[-1]["tokens"] == 715)
        c.put("/api/settings", json={"eye": {"engine": "frame", "qwen_protocol": "openai"}})

        print("== 引擎单元")
        from app.eye import engine as eg
        r = {"protocol": "anthropic", "base_url": "https://x.example/v1", "api_key": "k", "model": "m"}
        url, _, body = eg.build_request(r, "sys", [{"role": "assistant", "content": "先说"}, {"role": "user", "content": "a"},
                                                   {"role": "user", "content": "b"}], "问", [JPG])
        check("Anthropic 地址以 /v1 结尾时不重复拼接", url == "https://x.example/v1/messages")
        check("Anthropic 历史：去掉开头 assistant、合并相邻同角色", [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
              and body["messages"][0]["content"] == "a\nb")
        check("测试图是合法 PNG", base64.b64decode(eg.PROBE.split(",")[1])[:8] == b"\x89PNG\r\n\x1a\n")

        print("== 转写（电影声音）")
        c.put("/api/settings", json={"eye": {"stt_mode": "api", "stt_model": "sensevoice"}})
        tr = c.post("/api/eye/transcribe", files={"file": ("a.webm", b"fakeaudio", "audio/webm")}, data={"session": sid, "kind": "subtitle"})
        check("转写服务返回台词", tr.status_code == 200 and tr.json()["text"] == "我们必须回去" and mock.state.stt_model == "sensevoice", tr.text[:200])

        print("== 画面不落盘 / 可选保存")
        check("默认不保存画面", not (soul / "eye" / "frames").exists())
        c.put("/api/settings", json={"eye": {"save_frames": True}})
        sse(c, {"session": sid, "text": "存一下", "frames": [JPG]})
        check("打开「保存画面」后才落盘", len(list((soul / "eye" / "frames" / sid).glob("*.jpg"))) == 1)
        c.put("/api/settings", json={"eye": {"save_frames": False}})
        diary = (soul / "eye" / "sessions" / f"{sid}.jsonl").read_text("utf-8")
        check("文字日记里没有图片数据", "base64" not in diary and "/9j/" not in diary and "他们在干嘛" in diary)

        print("== 结束：沉淀成星星")
        E = c.post(f"/api/eye/session/{sid}/end", json={}).json()
        star = E.get("star") or {}
        check("结束后生成一颗星（LLM 写回忆）", star.get("id") and "宇宙之眼" in star.get("tags", []) and "飞船" in c.get(f"/api/stars/{star['id']}").json()["content"], str(E)[:300])
        check("会话标记结束并关联星星", E["ended_at"] and E["star_id"] == star.get("id"))
        code, d, _ = sse(c, {"session": sid, "text": "还在吗", "frames": [JPG]})
        check("结束后不能再问", code == 400)
        E2 = c.post(f"/api/eye/session/{S2['id']}/end", json={"make_star": False}).json()
        check("可以选择不存星星", E2["ended_at"] and not E2.get("star"))
        lst = c.get("/api/eye/sessions").json()
        check("观看记录列表", len(lst) >= 2 and lst[0]["count"]["user"] >= 1)
        check("找不到的会话 404", c.get("/api/eye/session/deadbeef00").status_code == 404)
        ev_log = c.get("/api/events?limit=300").json()
        check("事件日志记录 eye_start / eye_end", any(e.get("type") == "eye_start" for e in ev_log) and any(e.get("type") == "eye_end" for e in ev_log))

        print("== 仅主人")
        c.put("/api/settings", json={"core": {"owner_token": "owner-secret-xyz"}})
        check("设了主人令牌后，不带令牌访问被拒（403）", c.get("/api/eye/status").status_code == 403)
        check("带主人令牌可以访问", c.get("/api/eye/status", headers={"x-soulstar-owner": "owner-secret-xyz"}).status_code == 200)
        c.put("/api/settings", json={"core": {"owner_token": ""}}, headers={"x-soulstar-owner": "owner-secret-xyz"})
        c.put("/api/settings", json={"eye": {"enabled": False}})
        check("设置里关闭宇宙之眼后接口拒绝", c.get("/api/eye/status").status_code == 403)
        c.put("/api/settings", json={"eye": {"enabled": True}})
    finally:
        srv.terminate()
        try:
            srv.wait(5)
        except Exception:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\n宇宙之眼结果：{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
