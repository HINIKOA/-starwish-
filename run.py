"""星愿 StarWish 启动器：python run.py [--host 127.0.0.1] [--port 7860] [--no-browser]"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config  # noqa: E402


def main():
    cfg = config.load_config()
    ap = argparse.ArgumentParser(description="星愿 StarWish 本地记忆库")
    ap.add_argument("--host", default=cfg["server"]["host"])
    ap.add_argument("--port", type=int, default=int(cfg["server"]["port"]))
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '::') else a.host}:{a.port}/"
    print(f"星愿 StarWish 启动中… 灵魂包目录：{config.SOUL_DIR}")
    print(f"面板地址：{url}    OpenAI 兼容地址：{url}v1")
    if cfg["server"].get("open_browser", True) and not a.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()
    import uvicorn

    from app.server import app

    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
