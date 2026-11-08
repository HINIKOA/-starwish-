"""星愿 StarWish · 宇宙之声测试。

用法：python tests/voice_test.py
单元部分在进程内跑（音高、男女、标记解析、登记簿匹配 / 学习 / 重新聚类 / 投票确认 / 声纹库）；
接口部分启动真实服务器（假声纹后端：纯音频率即身份）+ 模拟上游（转写 / 视觉模型 / LLM / 模型下载）。
如果本机已装 sherpa-onnx 且有真实模型（环境变量 STARWISH_REAL_VOICE_DIR 指向的目录，含 campplus.onnx / model.int8.onnx / four.wav），再用真实四人对话录音跑一遍分人。
"""
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = ",".join(x for x in [os.environ.get(_k, ""), "127.0.0.1", "localhost"] if x)

TMP = Path(tempfile.mkdtemp(prefix="starwish-voice-"))
os.environ["STARWISH_SOUL_DIR"] = str(TMP / "unit-soul")
os.environ["STARWISH_MODELS_DIR"] = str(TMP / "unit-models")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
from smoke_test import check, free_port, wait_up, RESULTS  # noqa: E402

SR = 16000
JPG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ=="


def tone(freq, sec, amp=0.3):
    t = np.arange(int(SR * sec)) / SR
    return (amp * (np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(4 * np.pi * freq * t) + 0.2 * np.sin(6 * np.pi * freq * t))).astype(np.float32)


def silence(sec):
    return np.zeros(int(SR * sec), dtype=np.float32)


def pcm(*parts):
    x = np.concatenate(parts)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def dom_freq(x):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec[f < 50] = 0
    return float(f[int(np.argmax(spec))])


# ======================= 单元 =======================
def unit():
    from app.voice import audio, naming, registry as rg
    from app.voice import backend as be, models as md

    print("== 音频：音高与男女")
    f_m, f_f = audio.estimate_f0(tone(120, 1.5)), audio.estimate_f0(tone(230, 1.5))
    check("120Hz 估出约 120Hz", f_m and abs(f_m - 120) < 8, f_m)
    check("230Hz 估出约 230Hz", f_f and abs(f_f - 230) < 12, f_f)
    check("低音判男、高音判女", audio.gender_of(f_m) == "男" and audio.gender_of(f_f) == "女")
    check("静音没有音高", audio.estimate_f0(silence(1)) is None)
    w = audio.float_to_wav(tone(200, 0.5))
    x2, sr2 = audio.wav_to_float(w)
    check("WAV 封装可往返", audio.wav_header_ok(w) and sr2 == SR and len(x2) == SR // 2)
    check("重采样 48k→16k 长度正确", len(audio.resample(np.zeros(48000, np.float32), 48000)) == 16000)

    print("== 台词与画面线索解析")
    check("自我介绍：我叫库珀", naming.self_intro("你好，我叫库珀，是驾驶员") == "库珀")
    check("自我介绍：不把「我叫什么」当名字", naming.self_intro("你知道我叫什么吗") is None)
    clean, tags = naming.parse_tags("他俩在吵架。\n【认人】男1=库珀（胸牌）；女1=布兰德")
    check("【认人】标记解析出两个人", [(t["label"], t["name"]) for t in tags] == [("男1", "库珀"), ("女1", "布兰德")] and tags[0]["note"] == "胸牌", str(tags))
    check("标记从回答里去掉", clean == "他俩在吵架。")
    tf = naming.TagFilter()
    shown = "".join(tf.feed(d) for d in ["他俩在", "吵架。\n【", "认", "人】男1=库珀"]) + tf.flush()
    check("流式时标记被藏起来（跨分片也行）", shown == "他俩在吵架。\n", repr(shown))
    tf2 = naming.TagFilter()
    shown2 = "".join(tf2.feed(d) for d in ["价格【", "便宜】"]) + tf2.flush()
    check("普通方括号不受影响", shown2 == "价格【便宜】", shown2)
    ids = naming.parse_identify('好的：[{"speaker":"男1","name":"库珀","evidence":"被喊","confidence":0.9},{"speaker":"女1","name":"某人","confidence":0.2}]')
    check("LLM 认人结果解析（过滤低置信）", len(ids) == 1 and ids[0]["name"] == "库珀")
    check("LLM 乱答时不报错", naming.parse_identify("我不知道") == [])

    print("== 片名 → 系列")
    check("「星际穿越 第2集」与「星际穿越」同系列", rg.series_key("星际穿越 第2集") == rg.series_key("星际穿越") == "星际穿越")
    check("S01E03 / EP5 也能去掉", rg.series_key("Friends S01E03") == "friends" and rg.series_key("繁花 EP5") == "繁花")

    print("== 登记簿：匹配、学习、重新聚类")
    fb = be.FakeBackend()
    v = {"match_threshold": 0.5, "min_learn_sec": 1.5, "merge_threshold": 0.65, "auto_accept_votes": 2}
    os.environ["STARWISH_SOUL_DIR"] = str(TMP / "unit-soul")
    reg = rg.Registry.load("aaaaaa000001", "星际穿越 第1集")
    a, _, n1 = reg.match(fb.embed(tone(120, 2)), 2.0, 120.0, v)
    b, _, n2 = reg.match(fb.embed(tone(230, 2)), 2.0, 230.0, v)
    a2, s2, n3 = reg.match(fb.embed(tone(122, 2)), 2.0, 122.0, v)
    c, _, _ = reg.match(fb.embed(tone(330, 2)), 2.0, 110.0, v)
    check("新说话人按男女编号：男1、女1", n1 and n2 and a["label"] == "男1" and b["label"] == "女1")
    check("同一个声音认成同一个人", (not n3) and a2["id"] == a["id"] and s2 > 0.9, s2)
    check("第三个人（低音）编号男2", c["label"] == "男2")
    n_before = a["n"]
    reg.match(fb.embed(tone(121, 0.6)), 0.6, None, v)
    check("短句不更新声纹（只累计时长）", a["n"] == n_before and a["segments"] == 3)
    d, _, nd = reg.match(fb.embed(tone(148, 2)), 2.0, 148.0, {**v, "match_threshold": 0.7})
    check("阈值高时被拆成新的人", nd and d["label"] == "男3")
    merges = reg.recluster({"merge_threshold": 0.55})
    check("重新聚类把拆开的男3并回男1", any(m["drop_label"] == "男3" and m["keep_label"] == "男1" for m in merges), str(merges))
    check("合并后按旧编号还能找到现在的人", reg.resolve(d["id"])["id"] == a["id"] and reg.resolve("男3")["id"] == a["id"])
    reg.add_timeline(a["id"], time.time() - 6, 3)
    reg.add_timeline(b["id"], time.time() - 2, 1.5)
    rec = reg.recent_speakers(30)
    check("最近说话的人按时间排列", [s["label"] for s, _, _ in rec] == ["男1", "女1"] and rec[0][1] >= 5)

    print("== 名字：三路线索投票")
    ch = reg.propose("男1", "库珀", "self_intro", "我叫库珀", v)
    check("一路线索：先记为「可能是」", ch["status"] == "proposed" and reg.display(a).startswith("男1（可能是库珀"))
    ch = reg.propose("男1", "库珀", "visual", "胸牌", v)
    check("第二路线索也说库珀 → 自动确认", ch["status"] == "confirmed" and reg.display(a) == "库珀")
    ch = reg.propose("女1", "库珀", "dialogue", "", v)
    check("名字已被别人确认时不抢，只记一票", ch["status"] == "proposed" and reg.resolve("女1")["name_status"] != "confirmed")
    reg.reject("女1", "库珀")
    check("否决错误提名", reg.resolve("女1")["name"] == "" and reg.best_proposal(reg.resolve("女1")) is None)
    ch = reg.propose("女1", "库珀", "visual", "", v)
    check("被否决过的名字不会再自动确认", ch["status"] == "rejected_before")
    ch = reg.propose("男1", "约瑟夫", "dialogue", "", v)
    check("已确认的名字默认不被自动线索改掉", ch["status"] == "kept" and reg.display(a) == "库珀")
    reg.set_name("女1", "布兰德")
    check("主人直接指定名字", reg.display("女1") == "布兰德")
    check("主人不能被合并", _raises(lambda: reg.merge("男1", "owner")))
    check("说话人表给模型看", "男1=库珀" in reg.roster_text() and "女1=布兰德" in reg.roster_text())
    reg.save()

    print("== 声纹库：按系列记住")
    saved = reg.save_to_library()
    check("结束时把确认了名字的人存进声纹库", saved == 2)
    reg2 = rg.Registry.load("aaaaaa000002", "星际穿越 第2集")
    s, score, new = reg2.match(fb.embed(tone(120, 2)), 2.0, 120.0, v)
    check("下一集一开口就认出库珀", (not new) and reg2.display(s) == "库珀" and s.get("from_library"), reg2.display(s))
    reg3 = rg.Registry.load("aaaaaa000003", "另一部电影")
    s3, _, new3 = reg3.match(fb.embed(tone(120, 2)), 2.0, 120.0, v)
    check("别的片子不会套用", new3 and reg3.display(s3) == "男1")
    rg.set_owner(fb.embed(tone(400, 4)), 4)
    so, _, _ = reg3.match(fb.embed(tone(401, 2)), 2.0, 400.0, v)
    check("录过主人声音后认出主人", so["id"] == "owner" and reg3.display(so) == "主人")
    rg.clear_owner()
    check("声纹库公开视图不带向量", "centroid" not in json.dumps(rg.library_public()))

    print("== 插件安装状态")
    st = md.status()
    check("未下载时提示需要的大小（不含已装依赖约 35MB 模型）", (not st["ready"]) and st["need_mb"] >= 34, st["need_mb"])
    check("模型放在程序目录 models/voice（测试时为临时目录）", st["models_dir"].endswith("voice") and not st["models_dir"].startswith(os.environ["STARWISH_SOUL_DIR"]))
    os.environ.pop("STARWISH_VOICE_BACKEND", None)
    check("没装好时真实后端给出明确提示", _raises(lambda: be.get({"backend": "sherpa"}), be.BackendError))
    check("假后端只要 numpy", md.ready("fake"))


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


# ======================= 模拟上游 =======================
def start_mock(port):
    from fastapi import FastAPI, Request
    from fastapi.responses import Response, StreamingResponse
    import uvicorn

    app = FastAPI()
    st = app.state
    st.bodies, st.stt_calls, st.identify = [], 0, 0
    emb = b"ONNX" + b"\0" * 5000
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:bz2") as tf:
        data = b"SEG" + b"\0" * 3000
        info = tarfile.TarInfo("sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    seg = tbuf.getvalue()

    @app.get("/v1/models")
    def models():
        return {"data": [{"id": "mock"}]}

    @app.get("/rel/speaker-recongition-models/{name}")
    def m1(name: str):
        return Response(emb, media_type="application/octet-stream")

    @app.get("/rel/speaker-segmentation-models/{name}")
    def m2(name: str):
        return Response(seg, media_type="application/octet-stream")

    @app.post("/v1/audio/transcriptions")
    async def stt(req: Request):
        form = await req.form()
        raw = await form["file"].read()
        with wave.open(io.BytesIO(raw)) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        st.stt_calls += 1
        f = dom_freq(x)
        if f < 170:
            return {"text": "我叫库珀，我来开飞船" if st.stt_calls < 3 else "走吧"}
        return {"text": "库珀，你必须回去"}

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        st.bodies.append(body)
        if not body.get("stream"):
            txt = body["messages"][-1]["content"]
            if "推断每个编号的名字" in txt:
                st.identify += 1
                return {"choices": [{"message": {"role": "assistant", "content":
                        '[{"speaker":"女1","name":"布兰德","evidence":"男1 回答她时叫她布兰德","confidence":0.9}]'}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "我们一起看了星际穿越，认识了库珀。"}}]}
        sysmsg = body["messages"][0]["content"]
        last = body["messages"][-1]["content"]
        utext = last[-1]["text"] if isinstance(last, list) else last
        reply = "看到了，两个人在争论。\n【认人】男1=库珀（胸牌写着）；女1=布兰德（字幕）" if ("宇宙之声" in sysmsg and "他们是谁" in utext) else "看到了。"

        async def gen():
            for i in range(0, len(reply), 3):
                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": reply[i:i + 3]}}]}, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    return app


def sse(c, body):
    ev = []
    with c.stream("POST", "/api/eye/ask", json=body) as r:
        if "event-stream" not in r.headers.get("content-type", ""):
            r.read()
            return r.status_code, r.json(), []
        for line in r.iter_lines():
            if line.startswith("data:"):
                ev.append(json.loads(line[5:]))
    return 200, None, ev


def post_audio(c, sid, data, t0=None):
    q = f"?sr=16000&t0={t0 or time.time() - len(data) / 32000:.2f}"
    return c.post(f"/api/voice/session/{sid}/audio{q}", content=data, headers={"Content-Type": "application/octet-stream"})


# ======================= 接口 =======================
def api_tests():
    soul, mdir = TMP / "soul", TMP / "models"
    up, port = free_port(), free_port()
    mock = start_mock(up)
    U, B = f"http://127.0.0.1:{up}", f"http://127.0.0.1:{port}"
    env = {**os.environ, "STARWISH_SOUL_DIR": str(soul), "STARWISH_MODELS_DIR": str(mdir), "STARWISH_VOICE_BACKEND": "fake",
           "STARWISH_VOICE_MODEL_BASE": U + "/rel", "PYTHONIOENCODING": "utf-8"}
    srv = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--no-browser", "--port", str(port)],
                           env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    c = httpx.Client(base_url=B, timeout=60)
    try:
        print("== 宇宙之声 · 启动")
        check("服务器启动", wait_up(B + "/api/health"))
        check("模拟上游启动", wait_up(U + "/v1/models"))
        from app import __version__
        check("版本号 ≥ 0.5.0 且与服务一致", tuple(map(int, __version__.split("."))) >= (0, 5, 0) and c.get("/api/health").json()["version"] == __version__)

        print("== 页面与开关")
        pg = c.get("/eye").text
        js = c.get("/static/eye.js").text
        check("观看页有「🌌 宇宙之声」开关和片中人物面板", 'id="voiceBtn"' in pg and "宇宙之声" in pg and 'id="cast"' in pg and 'data-tab="voices"' in pg)
        check("前端：16kHz PCM 采集、分人面板、改名 / 确认 / 合并、录主人声音",
              all(k in js for k in ("pcmCapture", "renderCast", "data-act=\"merge\"", "owner/enroll", "toggleVoice", "renderInstall")))
        s0 = c.get("/api/voice/status").json()
        check("默认关闭", s0["enabled"] is False and s0["settings"]["auto_accept_votes"] == 2)
        check("状态里列出依赖与模型", "sherpa_onnx" in s0["deps"] and set(s0["models"]) == {"embedding", "segmentation"})

        print("== 一键下载插件（模拟发布页）")
        c.post("/api/voice/install", json={"deps": False, "models": True})
        for _ in range(50):
            sm = c.get("/api/voice/status").json()
            if all(m["installed"] for m in sm["models"].values()):
                break
            time.sleep(0.2)
        check("两个模型都下载好了（分割模型从压缩包里解出 int8）", all(m["installed"] for m in sm["models"].values())
              and (mdir / "voice" / "pyannote_segmentation_3_0_int8.onnx").read_bytes()[:3] == b"SEG", str(sm["jobs"]))
        check("模型不在灵魂包里（不进备份）", not list(soul.rglob("*.onnx")))
        check("卸载模型释放空间", c.delete("/api/voice/models").json()["removed"] == 2 and not list((mdir / "voice").glob("*.onnx")))

        print("== 开始观看 + 分人 + 逐句转写")
        c.put("/api/settings", json={"llm": {"base_url": U + "/v1", "api_key": "sk-mock-123456", "model": "mock-vl"},
                                     "eye": {"stt_mode": "api"}, "voice": {"enabled": True, "auto_identify_every": 0}})
        S = c.post("/api/eye/session/start", json={"title": "星际穿越 第1集", "source": "tab"}).json()
        sid = S["id"]
        r = post_audio(c, sid, pcm(tone(120, 2.0), silence(0.7), tone(230, 2.0), silence(0.7), tone(121, 1.6), silence(0.3)))
        d = r.json()
        check("一段音频分出三句话、两个人", r.status_code == 200 and [l["label"] for l in d["lines"]] == ["男1", "女1", "男1"], r.text[:300])
        check("新面孔：男1、女1", d["new_speakers"] == ["男1", "女1"])
        check("逐句转写并标上说话人", d["lines"][0]["text"] == "我叫库珀，我来开飞船" and d["lines"][1]["text"] == "库珀，你必须回去")
        check("「我叫库珀」→ 男1 可能是库珀", any(n["label"] == "男1" and n["name"] == "库珀" and n["status"] == "proposed" for n in d["names"]), str(d["names"]))
        check("返回处理耗时与说话人表", d["proc_ms"] >= 0 and len(d["roster"]["speakers"]) == 2)
        diary = c.get(f"/api/eye/session/{sid}").json()["entries"]
        subs = [e for e in diary if e["kind"] == "subtitle"]
        check("画面日记里台词带说话人", len(subs) == 3 and all(e.get("speaker") for e in subs) and subs[0]["who"].startswith("男1"))
        check("静音段不产生台词", post_audio(c, sid, pcm(silence(3))).json()["lines"] == [])

        print("== 画面线索：提问时让视觉模型认人")
        code, _, ev = sse(c, {"session": sid, "text": "他们是谁？", "frames": [JPG]})
        shown = "".join(e.get("text", "") for e in ev if e["type"] == "delta")
        check("【认人】标记不显示、不朗读", "认人" not in shown and shown.strip() == "看到了，两个人在争论。", repr(shown))
        check("done 里的回答也是干净的", "认人" not in ev[-1]["text"])
        names_ev = [e for e in ev if e["type"] == "names"]
        check("回答里认出的人回传给页面", names_ev and any(x["label"] == "男1" for x in names_ev[0]["changes"]))
        sysm = mock.state.bodies[-1]["messages"][0]["content"]
        check("系统提示带说话人表和「最近说话的人」", "说话人表" in sysm and "男1" in sysm and "最近说话的人" in sysm, sysm[-300:])
        sp = c.get(f"/api/voice/session/{sid}/speakers").json()
        m1 = next(s for s in sp["speakers"] if s["label"] == "男1")
        f1 = next(s for s in sp["speakers"] if s["label"] == "女1")
        check("自我介绍 + 画面两路线索 → 男1 自动确认为库珀", m1["name"] == "库珀" and m1["name_status"] == "confirmed")
        check("只有画面一路 → 女1 暂记「可能是布兰德」", f1["name"] == "布兰德" and f1["name_status"] == "proposed")
        sse(c, {"session": sid, "text": "刚才谁说的？", "frames": [JPG]})
        um = mock.state.bodies[-1]["messages"][-1]["content"]
        utext = um[-1]["text"] if isinstance(um, list) else um
        check("最近台词里用上了名字", "台词·库珀" in utext, utext[:200])

        print("== 台词线索：让 AI 读台词认人")
        r = c.post(f"/api/voice/session/{sid}/identify").json()
        check("LLM 从称呼推断女1 = 布兰德，两路线索 → 确认", any(x["label"] == "女1" and x["status"] == "confirmed" for x in r["changes"]), str(r["changes"]))
        c.put("/api/settings", json={"voice": {"auto_identify_every": 2}})
        n0 = mock.state.identify
        post_audio(c, sid, pcm(tone(230, 1.8), silence(0.6), tone(120, 1.8), silence(0.4)))
        for _ in range(30):
            if mock.state.identify > n0:
                break
            time.sleep(0.1)
        check("台词攒够了自动认一次", mock.state.identify > n0)
        c.put("/api/settings", json={"voice": {"auto_identify_every": 0}})

        print("== 主人纠错：改名、确认、否决、合并、重新聚类")
        c.put("/api/settings", json={"voice": {"match_threshold": 0.7}})
        d = post_audio(c, sid, pcm(tone(148, 2.0), silence(0.5), tone(330, 2.0), silence(0.5))).json()
        labels = [l["label"] for l in d["lines"]]
        check("阈值调高后拆出男2（其实是库珀）和新人", "男2" in labels and len(set(labels)) == 2, str(labels))
        c.put("/api/settings", json={"voice": {"match_threshold": 0.5, "merge_threshold": 0.55}})
        rc = c.post(f"/api/voice/session/{sid}/recluster").json()
        check("重新聚类把男2并回库珀", any(m["drop_label"] == "男2" for m in rc["merges"]), str(rc["merges"]))
        c.put("/api/settings", json={"voice": {"merge_threshold": 0.65}})
        sp = c.get(f"/api/voice/session/{sid}/speakers").json()
        new = next(s for s in sp["speakers"] if s["label"] not in ("男1", "女1"))
        r = c.post(f"/api/voice/session/{sid}/speakers/{new['id']}", json={"action": "set", "name": "艾米莉亚"}).json()
        check("主人改名立即生效", any(s["id"] == new["id"] and s["display"] == "艾米莉亚" for s in r["speakers"]))
        r = c.post(f"/api/voice/session/{sid}/speakers/{new['id']}", json={"action": "reject", "name": "艾米莉亚"}).json()
        check("否决后名字清空", any(s["id"] == new["id"] and not s["name"] for s in r["speakers"]))
        r = c.post(f"/api/voice/session/{sid}/merge", json={"keep": f1["id"], "drop": new["id"]}).json()
        check("手动合并：两个人变一个", not any(s["id"] == new["id"] for s in r["speakers"]))
        check("确认没有提名时给出提示", c.post(f"/api/voice/session/{sid}/speakers/nobody", json={"action": "confirm"}).status_code == 400)
        check("和自己合并被拒绝", c.post(f"/api/voice/session/{sid}/merge", json={"keep": f1["id"], "drop": f1["id"]}).status_code == 400)

        print("== 主人的声音")
        e = c.post("/api/voice/owner/enroll?sr=16000", content=pcm(tone(400, 5)), headers={"Content-Type": "application/octet-stream"})
        check("录入主人声音", e.status_code == 200 and e.json()["enrolled"], e.text[:200])
        check("太短的录音被拒绝", c.post("/api/voice/owner/enroll", content=pcm(tone(400, 1))).status_code == 400)
        d = post_audio(c, sid, pcm(tone(400, 2.0), silence(0.5))).json()
        check("主人说话标为「主人」", d["lines"] and d["lines"][0]["display"] == "主人", str(d["lines"]))

        print("== 结束：存进声纹库，下一集直接认出")
        E = c.post(f"/api/eye/session/{sid}/end", json={}).json()
        check("结束时报告认识的人并存进声纹库", "库珀" in E["voice"]["cast"] and "布兰德" in E["voice"]["cast"] and E["voice"]["saved"] >= 2, str(E.get("voice")))
        check("星星里记下片中认识的人", E.get("star") and "库珀" in c.get(f"/api/stars/{E['star']['id']}").json()["content"])
        lib = c.get("/api/voice/library").json()
        check("声纹库按系列保存（第几集不影响）", lib["titles"] and lib["titles"][0]["key"] == "星际穿越" and lib["owner"]["enrolled"])
        check("声纹库接口不返回向量", "centroid" not in json.dumps(lib))
        check("结束后不能再送音频", post_audio(c, sid, pcm(tone(120, 2))).status_code == 400)
        S2 = c.post("/api/eye/session/start", json={"title": "星际穿越 第2集", "source": "tab"}).json()
        d = post_audio(c, S2["id"], pcm(tone(120, 2.0), silence(0.5))).json()
        check("第2集一开口：库珀", d["lines"] and d["lines"][0]["display"] == "库珀" and not d["new_speakers"], str(d["lines"]))
        check("删除声纹库里的一部", c.delete("/api/voice/library/星际穿越").status_code == 200 and not c.get("/api/voice/library").json()["titles"])
        c.delete("/api/voice/owner")
        check("删除主人声音", not c.get("/api/voice/library").json()["owner"]["enrolled"])
        check("文件里只有向量没有录音", not list(soul.rglob("*.wav")) and not list(soul.rglob("*.pcm")))

        print("== 关闭与权限")
        c.put("/api/settings", json={"voice": {"enabled": False}})
        sse(c, {"session": S2["id"], "text": "随便看看", "frames": [JPG]})
        check("关掉开关后页面停止送音频；已有登记簿的会话照常问答", mock.state.bodies[-1]["stream"])
        check("找不到的会话 404", post_audio(c, "deadbeef00", pcm(tone(120, 1))).status_code == 404)
        check("空音频 400", c.post(f"/api/voice/session/{S2['id']}/audio", content=b"").status_code == 400)
        c.put("/api/settings", json={"core": {"owner_token": "owner-xyz"}})
        check("非主人不能用宇宙之声（403）", c.get("/api/voice/status").status_code == 403)
        c.put("/api/settings", json={"core": {"owner_token": ""}}, headers={"x-starwish-owner": "owner-xyz"})
        ev_log = c.get("/api/events?limit=500").json()
        check("事件日志记录新说话人 / 认名字 / 合并", all(any(x.get("type") == t for x in ev_log) for t in ("voice_new_speaker", "voice_name", "voice_merge")))
    finally:
        srv.terminate()
        try:
            srv.wait(5)
        except Exception:
            srv.kill()


# ======================= 真实模型（可选） =======================
def real_models():
    ref = Path(os.environ.get("STARWISH_REAL_VOICE_DIR") or "/workspace/work/voice-real")
    emb, segtar, wav = ref / "campplus.onnx", ref / "model.int8.onnx", ref / "four.wav"
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        print("== 真实模型：未安装 sherpa-onnx，跳过")
        return
    if not (emb.exists() and segtar.exists() and wav.exists()):
        print("== 真实模型：本机没有模型文件，跳过")
        return
    print("== 真实模型：sherpa-onnx + CAM++，四人中文对话")
    from app.voice import models as md, backend as be, audio
    from app.voice.registry import Registry
    d = TMP / "real-models" / "voice"
    d.mkdir(parents=True)
    shutil.copy(emb, d / md.MODELS["embedding"]["file"])
    shutil.copy(segtar, d / md.MODELS["segmentation"]["file"])
    os.environ["STARWISH_MODELS_DIR"] = str(TMP / "real-models")
    os.environ.pop("STARWISH_VOICE_BACKEND", None)
    v = {"backend": "sherpa", "match_threshold": 0.5, "merge_threshold": 0.65, "min_learn_sec": 1.5}
    b = be.get(v)
    x, sr = audio.wav_to_float(wav.read_bytes())
    reg = Registry.load("bbbbbb000001", "四人对话", use_library=False)
    t0, proc = time.time(), 0.0
    for i in range(0, len(x), sr * 8):
        ch = x[i:i + sr * 8]
        a = time.time()
        res = b.process(ch, sr)
        proc += time.time() - a
        for loc, info in res["locals"].items():
            reg.match(info["emb"], info["seconds"], info["f0"], v, t=t0 + i / sr)
    reg.recluster(v)
    n = len(reg.active())
    dur = len(x) / sr
    check(f"按 8 秒一段实时分人：四人录音认出 {n} 人（3~5 算合格）", 3 <= n <= 5, [s["label"] for s in reg.active()])
    check(f"实时性：{dur:.0f} 秒音频处理 {proc:.1f} 秒（RTF {proc / dur:.2f}，需 < 1）", proc / dur < 1)


def main():
    try:
        unit()
        api_tests()
        real_models()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    ok = sum(1 for _, o, _ in RESULTS if o)
    for n, o, dt in RESULTS:
        if not o:
            print("  ✘", n, dt)
    print(f"\n宇宙之声结果：{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
