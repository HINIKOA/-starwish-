"""Read aggregate flywheel health from an opt-in GraphFlow 3.9.2 journal."""
import json
import os
import re
import stat
import time
from pathlib import Path

from . import config
from .graphflow import GraphFlowError


ENV = "STARWISH_GRAPHFLOW_LEGACY_JOURNAL"
TOPIC_ENV = "STARWISH_GRAPHFLOW_LEGACY_TOPIC_RESUME"
MAX_BYTES = 1024 * 1024
TTL_DAYS = 30
_DAY_MS = 86_400_000
_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _invalid_constant(value):
    raise ValueError("Invalid JSON constant")


def topic_resume_enabled() -> bool:
    return os.environ.get(TOPIC_ENV) == "1"


def valid_topic(value: str) -> bool:
    return _TOPIC.fullmatch(value) is not None


def _read(workspace: Path) -> tuple[str, bytes]:
    path = workspace / ".graphflow" / "session-journal.jsonl"
    try:
        if path.resolve() != path or path.is_relative_to(config.SOUL_DIR.resolve()):
            return "unavailable", b""
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return "unavailable", b""
        if info.st_size > MAX_BYTES:
            return "too_large", b""
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return "missing", b""
    except (OSError, RuntimeError):
        return "unavailable", b""
    if len(data) > MAX_BYTES:
        return "too_large", b""
    return "ready", data.removeprefix(b"\xef\xbb\xbf")


def _events(data: bytes):
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant)
        except (ValueError, RecursionError):
            event = None
        yield event if isinstance(event, dict) else None


def require_topic(workspace: Path, topic: str) -> None:
    if not topic_resume_enabled():
        raise GraphFlowError("未启用旧话题续读。", 503)
    if not valid_topic(topic):
        raise GraphFlowError("旧话题编号格式不正确。")
    status, data = _read(workspace)
    if status == "too_large":
        raise GraphFlowError("旧话题日志超过可读取限制。", 413)
    if status not in {"ready", "missing"}:
        raise GraphFlowError("旧话题日志未启用或暂不可读。", 503)
    for event in _events(data):
        if not event:
            continue
        keys = ("topicId", "turnId", "episodeId")
        if event.get("kind") == "pending-episode":
            keys += ("id",)
        if any(event.get(key) == topic for key in keys):
            return
    raise GraphFlowError("当前授权工作区内没有该话题。", 404)


def inspect(workspace: Path) -> dict:
    if os.environ.get(ENV) != "1":
        return {"status": "disabled"}
    status, data = _read(workspace)
    if status != "ready":
        return {"status": status}
    total = errors = pending = expired = unknown_age = 0
    oldest = None
    now = time.time() * 1000
    cutoff = now - TTL_DAYS * _DAY_MS
    for event in _events(data):
        total += 1
        if event is None:
            errors += 1
            continue
        if event.get("kind") != "pending-episode":
            continue
        pending += 1
        created = event.get("createdAt")
        if type(created) not in (int, float) or not -8.64e15 <= created <= 8.64e15:
            unknown_age += 1
            continue
        expired += created < cutoff
        oldest = created if oldest is None else min(oldest, created)
    return {"status": "ready", "total_lines": total, "parse_errors": errors, "pending": pending,
            "over_ttl": expired, "unknown_age": unknown_age, "ttl_days": TTL_DAYS,
            "oldest_pending_age_days": None if oldest is None else round((now - oldest) / _DAY_MS, 2)}
