"""`星愿记忆.exe --notify <客户端>`：各客户端「本轮结束」钩子调用的小命令。

- 钩子 JSON：Claude Code / Qoder / WorkBuddy / CodeBuddy / dsh 从标准输入给；Codex 和 OpenCode 插件放在最后一个参数里。
- POST 到正在运行的星愿（端口取 state.json 的 run_port，再试配置里的端口）。
- 星愿没开：追加到 soul/watch/inbox.jsonl，下次启动补上（轮询本来也会补读，这里只是更快）。
- 无论如何 0 退出、几秒内结束，绝不阻塞客户端。
只用标准库，不加载服务端依赖。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request

MAX_FIELD = 20000


def _read_stdin(timeout: float = 2.0) -> dict:
    s = sys.stdin
    if s is None:
        return {}
    try:
        if s.isatty():
            return {}
    except Exception:
        return {}
    box: dict = {}

    def rd():
        try:
            box["d"] = (s.buffer if hasattr(s, "buffer") else s).read()
        except Exception:
            box["d"] = b""
    t = threading.Thread(target=rd, daemon=True)
    t.start()
    t.join(timeout)
    raw = box.get("d") or b""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _trim(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > MAX_FIELD:
            v = v[-MAX_FIELD:]
        if k in ("background_tasks", "session_crons", "tool_input", "tool_response"):
            continue
        out[k] = v
    return out


def _ports() -> list[int]:
    from .. import config   # 只有标准库依赖
    ports = []
    try:
        p = int(config.load_state().get("run_port") or 0)
        if p:
            ports.append(p)
    except Exception:
        pass
    try:
        p = int((config.load_raw_config().get("server") or {}).get("port") or 7860)
        if p not in ports:
            ports.append(p)
    except Exception:
        ports.append(7860)
    return ports


def send(client: str, payload: dict, timeout: float = 2.0) -> str:
    body = json.dumps({"client": client, "payload": payload}, ensure_ascii=False).encode("utf-8")
    for port in _ports():
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/watch/notify", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机，不走系统代理
            with opener.open(req, timeout=timeout) as r:
                if r.status == 200:
                    return "sent"
        except Exception:
            continue
    from .. import config
    d = config.soul_path("watch")
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "inbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"client": client, "payload": payload, "t": time.time()}, ensure_ascii=False) + "\n")
    return "inbox"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        i = argv.index("--notify")
        rest = argv[i + 1:]
        client = rest[0] if rest and not rest[0].startswith("{") else "claude"
        js = [a for a in rest if a.strip().startswith("{")]
        payload = {}
        if js:
            try:
                payload = json.loads(js[-1])
            except Exception:
                payload = {}
        else:
            payload = _read_stdin()
        send(client, _trim(payload if isinstance(payload, dict) else {}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
