"""Mine replayable read bundles from an explicitly enabled legacy trajectory log."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections import Counter
from pathlib import Path

from . import config, graphflow, graphflow_history
from .graphflow import GraphFlowError

ENV = "STARWISH_GRAPHFLOW_LEGACY_TRAJECTORIES"
ID = re.compile(r"trace_[0-9a-f]{16}")
MAX_BYTES = 1024 * 1024
MAX_EVENTS = 500
MAX_BUNDLES = 12
MAX_TARGETS = 6


def _read(workspace: Path) -> tuple[str, list[bytes]]:
    if os.environ.get(ENV) != "1":
        return "disabled", []
    path = workspace / ".graphflow" / "trajectories.jsonl"
    try:
        if (path.resolve(strict=True) != path or path.is_relative_to(config.SOUL_DIR)
                or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_nlink != 1):
            return "unavailable", []
        with path.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return "missing", []
    except (OSError, RuntimeError):
        return "unavailable", []
    if len(data) > MAX_BYTES:
        return "too_large", []
    return "ready", data.removeprefix(b"\xef\xbb\xbf").splitlines()[-MAX_EVENTS:]


def _event(raw: bytes) -> tuple[str, tuple[str, ...], str] | None:
    try:
        row = json.loads(raw)
        if not isinstance(row, dict) or row.get("kind") != "read" or row.get("ok") is not True:
            return None
        items, session = row.get("items"), row.get("sessionId")
        if (not isinstance(items, list) or not 1 <= len(items) <= MAX_TARGETS
                or not isinstance(session, str) or not 1 <= len(session) <= 128):
            return None
        targets = tuple(sorted({graphflow.validate_read(target=item)[1] for item in items}))
        for target in targets:
            target.encode("utf-8")
        timestamp = row.get("ts")
        return session, targets, timestamp if isinstance(timestamp, str) and len(timestamp) <= 64 else ""
    except (ValueError, RecursionError):
        return None


def _counts(events: list[bytes]) -> Counter:
    counts = Counter()
    previous = None
    seen = set()
    for raw in events:
        event = _event(raw)
        if event is None or (event[2] and event in seen):
            previous = None
            continue
        if event[2]:
            seen.add(event)
        session, targets, _ = event
        if len(targets) > 1:
            counts[targets] += 1
            previous = None
        elif previous and previous[0] == session and previous[1] != targets:
            counts[tuple(sorted((*previous[1], *targets)))] += 1
            previous = None
        else:
            previous = (session, targets)
    return counts


def mine(workspace: Path, *, budget: int = 5000) -> dict:
    status, events = _read(workspace)
    result = {"status": status, "events_scanned": len(events), "candidates": [], "more": False, "persisted": False}
    groups = [(targets, count) for targets, count in _counts(events).items() if count >= 2]
    if not groups:
        return result
    git = shutil.which("git")
    if not git:
        raise GraphFlowError("轨迹分析需要 Git。", 503)
    tracked = graphflow_history._tracked_paths(workspace, git)
    groups.sort(key=lambda row: (-row[1], row[0]))
    for targets, count in groups:
        if any(os.fsencode(graphflow.validate_read(target=target)[2]) not in tracked for target in targets):
            continue
        if len(result["candidates"]) >= MAX_BUNDLES:
            result["more"] = True
            break
        digest = hashlib.sha256(json.dumps(targets, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        result["candidates"].append({"id": f"trace_{digest[:16]}", "targets": list(targets), "occurrences": count})
        if len(json.dumps(result, ensure_ascii=False)) > budget:
            result["candidates"].pop()
            result["more"] = True
            break
    return result


def specs(workspace: Path) -> dict:
    return {entry["id"]: {"title": f"轨迹代码包 · {entry['id']}",
                           "steps": [{"op": "read_batch", "targets": entry["targets"]}]}
            for entry in mine(workspace, budget=42000)["candidates"]}
