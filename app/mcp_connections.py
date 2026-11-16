"""Local STDIO session presence shared by the MCP processes and the panel."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from . import config, graphflow

HEARTBEAT = 3
TTL = 12


def launch_config(*, include_project: bool = False) -> dict:
    root = config.APP_DIR
    exe = os.environ.get("STARWISH_EXE")
    script = str(root / "mcp_server.py")
    uv = shutil.which("uv")
    if exe:
        command, args = exe, ["--mcp"]
        root = Path(exe).parent
    elif uv and "builds-v0" in Path(sys.prefix).parts:
        command = uv
        args = ["run", "--no-project", "--with-requirements", str(root / "requirements.txt"),
                "--python", sys._base_executable, "python", script]
    else:
        command, args = sys.executable, [script]
    env = {"STARWISH_SOUL_DIR": str(config.SOUL_DIR)}
    if include_project:
        workspace = graphflow.checked_workspace()
        engine = graphflow.status()
        if not engine["available"]:
            raise graphflow.GraphFlowError(engine["reason"], 503)
        env["STARWISH_GRAPHFLOW_WORKSPACE"] = str(workspace)
        if os.environ.get("STARWISH_GRAPHFLOW_PREFIX"):
            env["STARWISH_GRAPHFLOW_PREFIX"] = str(graphflow.engine_prefix().resolve())
    return {"name": "starwish", "type": "stdio", "command": command, "args": args,
            "cwd": str(root), "env": env}


class Presence:
    def __init__(self):
        self.path = config.soul_path("mcp-connections", f"{uuid4().hex}.json")
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.initializing = False
        self.info = {"name": "MCP client", "version": "", "state": "pending", "since": time.time()}
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def _write(self):
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.info, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # Status tracking must not prevent use of the MCP tools.

    def _heartbeat(self):
        while not self.stop.wait(HEARTBEAT):
            with self.lock:
                self._write()

    def __enter__(self):
        self._write()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        for path in (self.path, self.path.with_suffix(".tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def observe(self, method, params=None):
        if method not in {"initialize", "notifications/initialized"}:
            return
        with self.lock:
            if method == "initialize":
                self.initializing = True
                client = params.get("clientInfo") if isinstance(params, dict) else None
                if isinstance(client, dict):
                    name = client.get("title") or client.get("name")
                    self.info["name"] = name.strip()[:120] if isinstance(name, str) and name.strip() else "MCP client"
                    version = client.get("version")
                    self.info["version"] = version.strip()[:48] if isinstance(version, str) else ""
            elif self.initializing:
                self.info["state"] = "connected"
            else:
                return
            self._write()


def status() -> dict:
    now = time.time()
    sessions = []
    for path in config.soul_path("mcp-connections").glob("*.json"):
        try:
            age = now - path.stat().st_mtime
            if age > 86400:
                path.unlink(missing_ok=True)
            if age > TTL:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["state"] in {"pending", "connected"}:
                sessions.append({k: data[k] for k in ("name", "version", "state", "since")})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    sessions.sort(key=lambda item: (item["state"] != "connected", item["since"]))
    connected = sum(item["state"] == "connected" for item in sessions)
    pending = len(sessions) - connected
    state = "partial" if pending else "connected" if connected else "disconnected"
    return {"state": state, "connected": connected, "pending": pending, "sessions": sessions}
