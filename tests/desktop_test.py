"""星愿记忆 v0.8.0 测试：改名、软件图标、桌面版入口（关窗即停服务）、Windows 免安装构建、设置窗口与模型服务。

用法：python tests/desktop_test.py
模型服务用本地模拟的上游（OpenAI 兼容 + Anthropic），不联网。
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="starwish-desktop-"))
os.environ["STARWISH_SOUL_DIR"] = str(TMP / "soul")
os.environ["STARWISH_NO_EMBED_BOOT"] = "1"

from smoke_test import check, RESULTS, free_port  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402

KEY = "sk-test-1234567890"
CALLS = []


def mock_upstream():
    up = FastAPI()

    def auth(a):
        if a not in (f"Bearer {KEY}", "Bearer sk-qw-0987654321"):
            raise HTTPException(401, "invalid api key")

    @up.get("/v1/models")
    def models(authorization: str = Header("")):
        auth(authorization)
        return {"data": [{"id": x} for x in ("deepseek-chat", "qwen-vl-max", "text-embedding-v4", "cosyvoice-v2", "glm-4.5")]}

    @up.post("/v1/chat/completions")
    async def chat(req: Request, authorization: str = Header("")):
        auth(authorization)
        b = await req.json()
        CALLS.append(("chat", b["model"]))
        if b["model"] == "bad-model":
            raise HTTPException(400, "model not found")
        return {"choices": [{"message": {"role": "assistant", "content": "好"}}]}

    @up.post("/v1/embeddings")
    async def emb(req: Request, authorization: str = Header("")):
        auth(authorization)
        b = await req.json()
        return {"data": [{"embedding": [0.1] * 32} for _ in b["input"]]}

    @up.get("/v1/v1/models")   # 防止「多补一次 /v1」
    def never():
        raise HTTPException(500, "double v1")

    @up.post("/v1/messages")
    async def anth(req: Request, x_api_key: str = Header("")):
        if x_api_key != KEY:
            raise HTTPException(401, "bad key")
        return {"content": [{"type": "text", "text": "好"}]}

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


def main():
    UP = mock_upstream()
    from fastapi.testclient import TestClient

    from app import config, providers
    from app.server import app

    c = TestClient(app)

    print("== 1. 改名为「星愿记忆」")
    hits = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(x in p.parts for x in (".git", "models", "build", "dist", "soul", "__pycache__", "vendor")) \
                and p.suffix in (".py", ".js", ".html", ".css", ".md", ".txt", ".bat", ".sh", ".json", ".c", ".rc") and p.name not in ("desktop_test.py", "CHANGELOG.md"):
            t = p.read_text("utf-8", errors="ignore")
            if re.search("星魂|SoulStar|soulstar|SOULSTAR", t.replace("原名「星魂」", "")):
                hits.append(str(p.relative_to(ROOT)))
    check("代码与文档里不再有「星魂 / SoulStar」", not hits, ",".join(hits[:5]))
    idx = c.get("/").text
    check("首页标题是「星愿记忆」", "<title>星愿记忆" in idx and "星愿记忆 <small>StarWish" in idx)
    check("health 返回 app=starwish", c.get("/api/health").json()["app"] == "starwish")
    check("MCP 服务名改为 starwish", 'MCPServer("starwish"' in (ROOT / "mcp_server.py").read_text("utf-8"))

    print("== 2. 软件图标")
    check("assets/starwish.ico 含 16~256 多尺寸", (ROOT / "assets" / "starwish.ico").read_bytes()[4] >= 6)
    fav = c.get("/favicon.ico")
    check("/favicon.ico 可访问", fav.status_code == 200 and len(fav.content) > 1000)
    check("各页面都挂了图标与 manifest", all('rel="icon"' in (ROOT / "web" / f).read_text("utf-8") and "manifest.json" in (ROOT / "web" / f).read_text("utf-8")
                                       for f in ("index.html", "core.html", "skills.html", "eye.html")))
    check("manifest 与 PNG 图标可访问", c.get("/static/manifest.json").json()["name"] == "星愿记忆" and c.get("/static/icons/icon-192.png").status_code == 200)
    check("顶栏显示软件图标", 'class="app-icon"' in idx)

    print("== 3. 桌面版：无命令窗口、关窗即停服务")
    import desktop

    check("desktop.pick_port：端口被占时自动换下一个", (lambda s: (s.bind(("127.0.0.1", 0)), s.listen(), desktop.pick_port("127.0.0.1", s.getsockname()[1]), s.close())[2])(socket.socket())[0] != 0)
    port = free_port()
    svc = desktop.Service("127.0.0.1", port)
    up = svc.start(port, timeout=40)
    check("Service 后台线程启动本地服务", up)
    check("已有星愿在跑时识别出来（不再起第二份）", desktop.pick_port("127.0.0.1", port) == (port, True))
    svc.stop()
    for _ in range(30):
        if not desktop._alive(port) and desktop._port_free("127.0.0.1", port):
            break
        time.sleep(0.2)
    check("关闭后服务停止、端口释放", not desktop._alive(port), f"thread alive={svc.thread.is_alive()}")
    # 浏览器兜底模式：页面心跳停了就退出
    from app import server as srv
    r = c.post("/api/ping")
    check("/api/ping 心跳可用", r.status_code == 200 and srv.LAST_PING > 0)
    env = {**os.environ, "STARWISH_SOUL_DIR": str(TMP / "soul2"), "STARWISH_DESKTOP_MODE": "browser", "BROWSER": "true",
           "STARWISH_DESKTOP_LOG": "1", "STARWISH_NO_EMBED_BOOT": "1"}
    code = "import desktop,time\ndesktop._browser_with_heartbeat=lambda url: time.sleep(3)\ndesktop.main()"
    t0 = time.time()
    p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, timeout=90, capture_output=True, text=True)
    check("desktop.main：窗口结束后整个进程退出（不留后台服务）", p.returncode == 0 and time.time() - t0 < 80, p.stderr[-300:])
    check("无控制台时日志写入 soul/logs/desktop.log", (TMP / "soul2" / "logs" / "desktop.log").exists())
    lc = (ROOT / "tools" / "launcher" / "launcher.c").read_text("utf-8")
    check("启动器用 pythonw + CREATE_NO_WINDOW（不弹命令窗口）", "pythonw.exe" in lc and "CREATE_NO_WINDOW" in lc and "wWinMain" in lc)
    check("启动器带图标资源", 'ICON "../../assets/starwish.ico"' in (ROOT / "tools" / "launcher" / "launcher.rc").read_text("utf-8"))
    check("不再提供 start.bat", not (ROOT / "start.bat").exists())

    print("== 4. Windows 免安装版（依赖预装）")
    dist = ROOT / "dist" / "星愿记忆"
    if (dist / "星愿记忆.exe").exists():
        exe = (dist / "星愿记忆.exe").read_bytes()
        check("星愿记忆.exe 是 Windows GUI 程序（不带控制台）", exe[:2] == b"MZ" and exe[exe[0x3C] + 0x5C] == 2)
        site = dist / "runtime" / "Lib" / "site-packages"
        need = ["fastapi", "uvicorn", "jieba", "onnxruntime", "tokenizers", "numpy", "webview", "clr_loader", "pythonnet", "sherpa_onnx", "cryptography", "mcp", "win32"]
        miss = [n for n in need if not (site / n).exists()]
        check("runtime 已预装全部依赖（含桌面窗口与声纹插件）", not miss, ",".join(miss))
        check("runtime 带 pythonw.exe 与 ._pth（含 import site）", (dist / "runtime" / "pythonw.exe").exists() and "import site" in (dist / "runtime" / "python312._pth").read_text())
        check("内置向量模型与声纹模型已放进软件", (dist / "models" / "embed" / "small" / "model.onnx").exists() and len(list((dist / "models" / "voice").glob("*.onnx"))) >= 2)
        check("附带 MCP接入.bat（走内置 runtime）", "runtime\\python.exe" in (dist / "MCP接入.bat").read_text("utf-8"))
    else:
        print("  （未构建 dist，跳过；运行 python tools/build_windows.py 后再测）")

    print("== 5/6. 设置窗口与模型服务")
    js = (ROOT / "web" / "settings.js").read_text("utf-8")
    app_js = (ROOT / "web" / "app.js").read_text("utf-8")
    check("设置是整页视图（v0.11.0），点「设置」开、← 返回关", "toggleSettings" in app_js and "settingsOpen" in js and 'className = "setview' in js and "← 返回" in js)
    check("Esc 返回", '"Escape"' in js)
    check("左侧分组导航：基础 / 工具 / 偏好，含模型服务 / 默认模型 / 本地模型 / MCP / 对话监控 / 宇宙之眼 / 外观 / 数据 / 核心记忆 / 关于",
          all(k in js for k in ("基础", "工具", "偏好", "模型服务", "默认模型", "本地模型", "MCP", "对话监控", "宇宙之眼", "外观", "数据", "安全", "核心记忆", "关于")))
    check("服务商页：搜索、添加服务商、Key 显示/隐藏、检测、同步模型、手动添加",
          all(k in js for k in ("搜索模型平台", "添加服务商", "pvEye", ">检测<", "同步模型", "手动添加")))
    pres = c.get("/api/providers/presets").json()
    check("预设服务商 ≥ 10 家，地址已填好", len(pres) >= 10 and all(p["base_url"] for p in pres if p["preset"] != "custom"))

    # 拉取模型：地址少了 /v1 自动补上
    r = c.post("/api/providers/models", json={"base_url": UP, "api_key": KEY}).json()
    check("同步模型：地址少了 /v1 时自动补上", r.get("fixed_base_url") == UP + "/v1" and len(r["models"]) >= 4)
    kinds = {m["id"]: m["kind"] for m in r["models"]}
    check("自动识别模型类型（对话 / 识图 / 向量）", kinds["deepseek-chat"] == "chat" and kinds["qwen-vl-max"] == "vision" and kinds["text-embedding-v4"] == "embedding")
    check("语音合成等非对话模型标为「其他」", kinds["cosyvoice-v2"] == "other")
    bad = c.post("/api/providers/check", json={"base_url": UP + "/v1", "api_key": "sk-wrong"})
    check("检测：Key 错误时说清楚原因", bad.status_code == 400 and "API Key 不对" in bad.json()["detail"])
    bad2 = c.post("/api/providers/check", json={"base_url": "http://127.0.0.1:1/v1", "api_key": KEY})
    check("检测：地址不通时提示检查地址 / 网络", bad2.status_code == 400 and "连不上" in bad2.json()["detail"])
    ok = c.post("/api/providers/check", json={"base_url": UP + "/v1", "api_key": KEY}).json()
    check("检测：只验证 Key 时报告可用模型数", ok["ok"] and "可用模型" in ok["msg"])
    ok2 = c.post("/api/providers/check", json={"base_url": UP + "/v1", "api_key": KEY, "model": "deepseek-chat"}).json()
    check("检测单个对话模型：真的发一句话", ok2["ok"] and ok2["reply"] == "好" and ("chat", "deepseek-chat") in CALLS)
    ok3 = c.post("/api/providers/check", json={"base_url": UP + "/v1", "api_key": KEY, "model": "text-embedding-v4"}).json()
    check("检测向量模型：走 /embeddings 并报维度", ok3["ok"] and "32 维" in ok3["msg"])
    bad3 = c.post("/api/providers/check", json={"base_url": UP + "/v1", "api_key": KEY, "model": "bad-model"})
    check("检测：模型名不对时提示", bad3.status_code == 400 and "模型名" in bad3.json()["detail"])
    ok4 = c.post("/api/providers/check", json={"base_url": UP, "api_key": KEY, "protocol": "anthropic", "model": "claude-x"}).json()
    check("Anthropic 协议检测", ok4["ok"])

    # 保存服务商 + 默认模型 → 映射到各模块
    prov = [{"id": "ds", "name": "深度求索", "preset": "deepseek", "protocol": "openai", "base_url": UP + "/v1", "api_key": KEY, "enabled": True,
             "models": [{"id": "deepseek-chat", "kind": "chat"}, {"id": "text-embedding-v4", "kind": "embedding"}]},
            {"id": "qw", "name": "百炼", "preset": "dashscope", "protocol": "openai", "base_url": UP + "/v1/", "api_key": "sk-qw-0987654321", "enabled": True,
             "models": [{"id": "qwen-vl-max", "kind": "vision"}]}]
    s = c.put("/api/settings", json={"providers": prov, "defaults": {"chat": "ds::deepseek-chat", "maintain": "qw::qwen-vl-max",
                                                                     "vision": "qw::qwen-vl-max", "embed": "ds::text-embedding-v4"}}).json()
    check("读取设置时服务商 Key 被掩码", s["providers"][0]["api_key"].startswith("****") and s["providers"][0]["api_key_set"])
    cfg = config.load_config()
    check("默认对话模型 → 代理与整理用的 llm", cfg["llm"]["base_url"] == UP + "/v1" and cfg["llm"]["api_key"] == KEY and cfg["llm"]["model"] == "deepseek-chat")
    check("整理模型在另一家服务商 → 独立地址与 Key", cfg["llm"]["maintenance_model"] == "qwen-vl-max" and cfg["llm"]["maint_base_url"] == UP + "/v1" and cfg["llm"]["maint_api_key"] == "sk-qw-0987654321")
    check("识图模型 → 宇宙之眼帧模式", cfg["eye"]["frame_model"] == "qwen-vl-max" and cfg["eye"]["frame_api_key"] == "sk-qw-0987654321")
    check("向量模型 → 向量接口档", cfg["embed"]["api_model"] == "text-embedding-v4" and cfg["embed"]["api_key"] == KEY)
    # 掩码原样回传：Key 不丢
    s2 = c.put("/api/settings", json={"providers": s["providers"], "defaults": s["defaults"]}).json()
    check("掩码 Key 原样回传时保留原 Key", config.load_config()["providers"][1]["api_key"] == "sk-qw-0987654321")
    check("用服务商 id 拉模型时自动用已保存的 Key", c.post("/api/providers/models", json={"id": "ds"}).status_code == 200)
    check("检测「默认模型」", c.post("/api/providers/check", json={"role": "chat"}).json()["ok"])
    from app import maintenance
    CALLS.clear()
    out = maintenance.chat([{"role": "user", "content": "hi"}])
    check("整理调用走整理模型", out == "好" and CALLS[-1] == ("chat", "qwen-vl-max"))
    # 关闭服务商 → 默认模型不再生效
    s2["providers"][1]["enabled"] = False
    c.put("/api/settings", json={"providers": s2["providers"]})
    check("关闭的服务商不参与默认模型", providers.resolve(config.load_config(), "vision") is None)
    # 机密加密也覆盖服务商 Key
    c.post("/api/secrets/encrypt", json={"passphrase": "pw123"})
    raw = (TMP / "soul" / "config.json").read_text("utf-8")
    check("开启主密码后服务商 Key 加密落盘", KEY not in raw and '"enc:' in raw)
    check("解锁状态下照常可用", config.load_config()["providers"][0]["api_key"] == KEY)
    config.set_master_passphrase(None)
    check("未解锁时服务商 Key 为空，不泄露密文", config.load_config()["providers"][0]["api_key"] == "")
    config.set_master_passphrase("pw123")
    c.post("/api/secrets/decrypt")
    # 旧版只有 llm 的：界面里显示为一个服务商
    (TMP / "soul" / "config.json").write_text('{"llm":{"base_url":"%s/v1","api_key":"%s","model":"deepseek-chat"}}' % (UP, KEY), "utf-8")
    m = c.get("/api/settings").json()
    check("旧设置自动显示成「上游大模型（旧设置）」服务商", m["providers"] and m["providers"][0]["id"] == "legacy" and m["defaults"]["chat"] == "legacy::deepseek-chat")
    c.put("/api/settings", json={"providers": m["providers"], "defaults": m["defaults"]})
    check("保存后旧 Key 不丢", config.load_config()["providers"][0]["api_key"] == KEY and config.load_config()["llm"]["api_key"] == KEY)

    # ---------- 单文件版 星愿记忆-windows-amd64.exe ----------
    import shutil as _sh, zipfile as _zf, importlib.util as _iu
    spec = _iu.spec_from_file_location("bw", ROOT / "tools" / "build_windows.py")
    bw = _iu.module_from_spec(spec); spec.loader.exec_module(bw)
    tr = bw.make_trailer(123, 456, "0.9.0-abcdef1234")
    check("尾部固定 64 字节且以魔数开头", len(tr) == 64 and tr[:8] == bw.MAGIC)
    check("单文件名为 星愿记忆-windows-amd64.exe", bw.ONEFILE_NAME == "星愿记忆-windows-amd64.exe")
    src = (ROOT / "tools" / "launcher" / "onefile.c").read_text("utf-8")
    check("启动器：已解压（.ok）则直接启动，不再解压", ".ok" in src and "GetFileAttributesW(okf) == INVALID_FILE_ATTRIBUTES" in src)
    check("启动器：先解到 .tmp 再改名，半途中断不留半成品", ".tmp" in src and "MoveFileW(tmp, target)" in src)
    check("启动器：记忆放 exe 旁 soul 文件夹", "STARWISH_SOUL_DIR" in src and "\\\\soul" in src)
    check("启动器：无命令窗口、等待子进程（关窗即退出）", "CREATE_NO_WINDOW" in src and "WaitForSingleObject" in src)
    check("启动器：--mcp 继承标准输入输出", "--mcp" in src and "STARTF_USESTDHANDLES" in src)
    check("启动器：清理旧版本缓存", "clean_old" in src)
    if _sh.which("x86_64-w64-mingw32-gcc"):
        fake = TMP / "fake"
        (fake / "runtime").mkdir(parents=True)
        (fake / "desktop.py").write_text("print(1)", "utf-8")
        (fake / "runtime" / "pythonw.exe").write_bytes(b"x" * 1000)
        (fake / "星愿记忆.exe").write_bytes(b"old")
        (fake / "MCP接入.bat").write_text("x", "utf-8")
        (fake / "中文 目录").mkdir()
        (fake / "中文 目录" / "说明.txt").write_text("你好", "utf-8")
        exe = bw.onefile(fake, TMP / bw.ONEFILE_NAME, "9.9.9")
        info = bw.read_trailer(exe)
        head = exe.read_bytes()[:2]
        check("单文件 exe 以 MZ 开头（Windows 可执行）", head == b"MZ")
        check("尾部标签 = 版本-哈希", info["tag"].startswith("9.9.9-") and len(info["tag"]) == 16)
        check("负载位置与大小自洽", info["offset"] + info["size"] + 64 == info["total"])
        import io
        z = _zf.ZipFile(io.BytesIO(exe.read_bytes()[info["offset"]:info["offset"] + info["size"]]))
        names = set(z.namelist())
        check("负载含运行环境与程序，中文路径无损", {"desktop.py", "runtime/pythonw.exe", "中文 目录/说明.txt"} <= names
              and z.read("中文 目录/说明.txt").decode() == "你好")
        check("负载不含旧启动器与 bat", "星愿记忆.exe" not in names and "MCP接入.bat" not in names)
        again = bw.onefile(fake, TMP / "again.exe", "9.9.9")
        check("内容不变则标签不变（第二次不会重解压）", bw.read_trailer(again)["tag"] == info["tag"])
        (fake / "desktop.py").write_text("print(2)", "utf-8")
        check("内容一变标签就变（升级会重新解压）", bw.read_trailer(bw.onefile(fake, TMP / "v2.exe", "9.9.9"))["tag"] != info["tag"])
        mz = TMP / "payload_check"
        if _sh.which("gcc"):
            ld = ROOT / "tools" / "launcher"
            subprocess.run(["gcc", "-O2", "-w", "-I", str(ld / "miniz"), "-o", str(mz), str(ld / "payload_check.c"),
                            str(ld / "miniz" / "miniz.c")], check=False)
        if mz.exists():
            r = subprocess.run([str(mz), str(exe)], capture_output=True, text=True)
            check("miniz（与启动器同一解压库）能完整读出负载", r.returncode == 0 and "bad=0" in r.stdout, r.stdout + r.stderr)
    h = c.get("/api/health").json()
    check("健康接口给出 MCP 接入方式", "mcp" in h and h["mcp"]["command"])
    os.environ["STARWISH_EXE"] = r"D:\星愿记忆-windows-amd64.exe"
    h = c.get("/api/health").json()
    check("单文件版 MCP = exe --mcp", h["mcp"] == {"command": r"D:\星愿记忆-windows-amd64.exe", "args": ["--mcp"]})
    os.environ.pop("STARWISH_EXE", None)

    ok = sum(1 for _, g, _ in RESULTS if g)
    for nme, g, dt in RESULTS:
        if not g:
            print("  ✘", nme, dt)
    print(f"\n桌面版与设置结果：{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
