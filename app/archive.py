"""原始对话归档：soul/archive/ 下只追加，不修改、不删除。"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config
from .db import log_event

_lock = threading.Lock()


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI 多模态 parts
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text"):
                out.append(p.get("text", ""))
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    return "" if content is None else str(content)


def append_messages(messages: list[dict], session: str = "", source: str = "api", day: str | None = None) -> str:
    """把一组消息追加到 archive/YYYY-MM-DD.jsonl，返回相对路径。"""
    config.ensure_dirs()
    day = day or datetime.now().strftime("%Y-%m-%d")
    path = config.ARCHIVE_DIR / f"{day}.jsonl"
    now = time.time()
    lines = []
    for m in messages:
        txt = _content_text(m.get("content"))
        if not txt.strip():
            continue
        lines.append(json.dumps({"t": m.get("t", now), "session": session, "source": source,
                                 "role": m.get("role", "user"), "content": txt}, ensure_ascii=False))
    if not lines:
        return ""
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    rel = path.relative_to(config.SOUL_DIR).as_posix()
    log_event("archive_append", file=rel, n=len(lines), session=session, source=source)
    return rel


def append_raw_file(rel_path: str, text: str) -> str:
    """导入的原始文本（如星汐 原始对话/*.txt）存到 archive/imported/...；已存在且内容相同则跳过，不同则追加。"""
    p = (config.ARCHIVE_DIR / "imported" / rel_path).resolve()
    if config.ARCHIVE_DIR.resolve() not in p.parents:
        raise ValueError("非法路径")
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if p.exists():
            old = p.read_text("utf-8", errors="replace")
            if text in old:
                return p.relative_to(config.SOUL_DIR).as_posix()
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n" + text)
        else:
            p.write_text(text, "utf-8")
    rel = p.relative_to(config.SOUL_DIR).as_posix()
    log_event("archive_import", file=rel, chars=len(text))
    return rel


def iter_jsonl_files() -> list[Path]:
    if not config.ARCHIVE_DIR.exists():
        return []
    return sorted(p for p in config.ARCHIVE_DIR.rglob("*.jsonl") if p.is_file())


def list_days() -> list[str]:
    return sorted(p.stem for p in config.ARCHIVE_DIR.glob("*.jsonl"))
