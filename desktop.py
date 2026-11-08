"""星愿记忆 桌面版入口：由「星愿记忆.exe」以 pythonw（无命令窗口）启动。

- 后台线程跑本地服务（uvicorn），主线程打开桌面窗口（pywebview + 系统自带的 Edge WebView2）。
- 关闭窗口 = 关闭软件：服务随之停止，进程退出，不留后台。
- 没有 WebView2 时退回 Edge / Chrome 的「应用模式」窗口，关掉它同样会停止服务；
  两者都没有时用默认浏览器打开，并靠页面心跳判断：页面全部关闭 60 秒后自动退出。
- 已有一个星愿在运行时，不再起第二份服务，只把窗口再打开一次。
- `--notify <客户端>`（v0.11.0）：各 AI 客户端「本轮结束」钩子调用；把提醒交给正在运行的星愿（没开就记进 inbox），
  立刻 0 退出，不开窗口、不启动服务。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app import __version__, config  # noqa: E402

TITLE = "星愿记忆"
ICON = HERE / "assets" / "starwish.ico"
LOG_DIR = config.SOUL_DIR / "logs"


def _quiet_stdio():
    """pythonw 下没有控制台，stdout/stderr 是 None；改写到日志文件，避免 uvicorn 打印时报错。"""
    if sys.stdout is None or sys.stderr is None or os.environ.get("STARWISH_DESKTOP_LOG"):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        f = open(LOG_DIR / "desktop.log", "a", encoding="utf-8", buffering=1)
        f.write(f"\n==== {time.strftime('%Y-%m-%dT%H:%M:%S%z')} 星愿记忆 v{__version__} 启动 ====\n")
        sys.stdout = sys.stderr = f


def _alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return json.loads(r.read().decode()).get("app") == "starwish"
    except Exception:
        return False


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, want: int) -> tuple[int, bool]:
    """返回 (端口, 是否已有星愿在跑)。想要的端口被别的程序占了就往后找。"""
    if _alive(want):
        return want, True
    for p in range(want, want + 30):
        if _port_free(host, p):
            return p, False
    raise RuntimeError("找不到可用端口")


class Service:
    def __init__(self, host: str, port: int):
        import uvicorn

        from app.server import app

        self.server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                                    log_config=None))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="starwish-server")

    def start(self, port: int, timeout: float = 30) -> bool:
        self.thread.start()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if _alive(port):
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.2)
        return False

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def _webview_window(url: str) -> bool:
    """用 pywebview 打开桌面窗口，阻塞到窗口关闭。成功返回 True。"""
    try:
        import webview  # pywebview
    except Exception as e:  # noqa: BLE001
        print("pywebview 不可用：", e)
        return False
    try:
        store = config.SOUL_DIR / ".webview"
        store.mkdir(parents=True, exist_ok=True)
        webview.create_window(TITLE, url, width=1360, height=860, min_size=(960, 640),
                              background_color="#05060f", text_select=True)
        kw = dict(private_mode=False, storage_path=str(store))
        if ICON.exists():
            kw["icon"] = str(ICON)
        try:
            webview.start(gui="edgechromium" if os.name == "nt" else None, **kw)
        except TypeError:  # 旧版 pywebview 没有 icon 参数
            kw.pop("icon", None)
            webview.start(gui="edgechromium" if os.name == "nt" else None, **kw)
        return True
    except Exception as e:  # noqa: BLE001
        print("WebView 窗口打开失败：", e)
        return False


def _browser_candidates() -> list[str]:
    c = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                c += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    for n in ("msedge", "microsoft-edge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(n)
        if p:
            c.append(p)
    return [p for p in c if p and os.path.exists(p)]


def _app_mode_window(url: str) -> bool:
    """Edge / Chrome 应用模式：独立窗口、无地址栏。用独立用户目录，进程会一直等到窗口关闭。"""
    for exe in _browser_candidates():
        prof = config.SOUL_DIR / ".appwindow"
        prof.mkdir(parents=True, exist_ok=True)
        try:
            t0 = time.time()
            p = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}", "--no-first-run",
                                  "--window-size=1360,860"])
            p.wait()
            if time.time() - t0 > 3:  # 太快退出说明被已有实例接管了，不能用来判断关窗
                return True
        except Exception as e:  # noqa: BLE001
            print("应用模式窗口失败：", exe, e)
    return False


def _browser_with_heartbeat(url: str):
    import webbrowser

    from app import server as srv

    webbrowser.open(url)
    t0 = time.time()
    while True:
        time.sleep(5)
        last = getattr(srv, "LAST_PING", 0.0)
        idle_ref = last or t0
        if time.time() - idle_ref > (60 if last else 180):
            return


def main():
    _quiet_stdio()
    cfg = config.load_config()
    host = cfg["server"].get("host", "127.0.0.1")
    port, running = pick_port(host, int(cfg["server"].get("port", 7860)))
    url = f"http://127.0.0.1:{port}/"
    svc = None
    if not running:
        svc = Service(host, port)
        if not svc.start(port):
            _fatal("本地服务没有启动成功，详情见 soul/logs/desktop.log")
            return
        config.update_state(run_port=port, run_pid=os.getpid())   # 钩子 --notify 按这个端口找服务
    try:
        if os.environ.get("STARWISH_DESKTOP_MODE") == "browser":
            _browser_with_heartbeat(url)
        elif not _webview_window(url) and not _app_mode_window(url):
            _browser_with_heartbeat(url)
    finally:
        if svc:
            svc.stop()
            try:
                config.update_state(run_port=0, run_pid=0)
            except Exception:  # noqa: BLE001
                pass
    os._exit(0)


def _fatal(msg: str):
    print(msg)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, TITLE, 0x10)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    if "--notify" in sys.argv:          # 钩子调用：读 JSON、交给服务、立刻退出
        from app.watch import notify
        sys.exit(notify.main(sys.argv[1:]))
    main()
