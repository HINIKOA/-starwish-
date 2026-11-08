"""偏移量：每个对话文件读到哪了（soul/watch/offsets.json，原子写）。下次启动从这里接着读，关着时聊的也不丢。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .. import config

_lock = threading.RLock()


def watch_dir() -> Path:
    d = config.soul_path("watch")
    d.mkdir(parents=True, exist_ok=True)
    return d


def offsets_path() -> Path:
    return watch_dir() / "offsets.json"


def inbox_path() -> Path:
    return watch_dir() / "inbox.jsonl"


class State:
    """{"files": {路径: {c: 客户端, o: 偏移, s: 大小, m: mtime, meta: {...}, p: 有未完成的一轮, f: 失败次数}},
        "clients": {客户端: {"init": 初始化时间}}, "dbs": {数据库路径: {...}}}"""

    def __init__(self):
        self.data = {"files": {}, "clients": {}, "dbs": {}}
        self.load()

    def load(self):
        with _lock:
            p = offsets_path()
            if p.exists():
                try:
                    d = json.loads(p.read_text("utf-8"))
                    if isinstance(d, dict):
                        self.data.update({k: d.get(k) or {} for k in ("files", "clients", "dbs")})
                except Exception:
                    pass

    def save(self):
        with _lock:
            p = offsets_path()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), "utf-8")
            os.replace(tmp, p)

    @property
    def files(self) -> dict:
        return self.data["files"]

    @property
    def clients(self) -> dict:
        return self.data["clients"]

    @property
    def dbs(self) -> dict:
        return self.data["dbs"]
