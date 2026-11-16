"""Read-only temporal coupling hints from a bounded Git history window."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from collections import Counter
from itertools import combinations
from pathlib import Path

from . import config, graphflow
from .graphflow import GraphFlowError

MAX_COMMITS = 120
MAX_COMMIT_FILES = 20
MAX_PAIRS = 12
MAX_GIT_BYTES = 4 * 1024 * 1024
_OID = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_GATE = threading.BoundedSemaphore(1)


def _read_git(workspace: Path, git: str, *args: str, missing_ok: bool = False) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
    try:
        with subprocess.Popen([git, "--no-pager", "-c", "core.fsmonitor=false", *args], cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            data = []

            def collect():
                try:
                    data.append(proc.stdout.read(MAX_GIT_BYTES + 1))
                except OSError:
                    pass

            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            reader.join(6)
            try:
                if reader.is_alive():
                    raise GraphFlowError("读取 Git 历史超时。", 504)
                if data and len(data[0]) > MAX_GIT_BYTES:
                    raise GraphFlowError("Git 路径清单超过历史分析容量。", 413)
                code = proc.wait(timeout=1)
                if code == 1 and missing_ok:
                    return b""
                if code or not data:
                    raise GraphFlowError("无法读取项目 Git 历史。", 503)
                return data[0]
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
                reader.join(1)
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("读取 Git 历史超时。", 504) from exc
    except OSError as exc:
        raise GraphFlowError("无法启动 Git 历史查询。", 503) from exc


def _tracked_paths(workspace: Path, git: str) -> set[bytes]:
    tracked = set()
    for row in _read_git(workspace, git, "ls-files", "--stage", "-z", "--").split(b"\0"):
        mode, _, raw_path = row.partition(b"\t")
        if not re.fullmatch(rb"100(?:644|755) [0-9a-f]+ 0", mode):
            continue
        path = os.fsdecode(raw_path)
        if any(0xD800 <= ord(char) <= 0xDFFF for char in path) or not graphflow._safe_source(path):
            continue
        file = workspace / path
        try:
            if file.resolve(strict=True) == file and not file.is_relative_to(config.SOUL_DIR) and file.is_file():
                tracked.add(raw_path)
        except (OSError, RuntimeError):
            continue
    return tracked


def _commits(raw: bytes):
    paths = None
    boundary = True
    for token in raw.split(b"\0"):
        if not token:
            boundary = True
            continue
        if boundary:
            if not _OID.fullmatch(token):
                raise GraphFlowError("Git 历史记录格式无效。", 503)
            if paths is not None:
                yield paths
            paths = []
            boundary = False
        else:
            paths.append(token if paths else token.removeprefix(b"\n"))
    if paths is not None:
        yield paths


def mine(workspace: Path, *, budget: int = 5000, target: str = "", allowed_files: set[str] | None = None) -> dict:
    git = shutil.which("git")
    if not git:
        raise GraphFlowError("历史分析需要 Git。", 503)
    if not _GATE.acquire(blocking=False):
        raise GraphFlowError("已有历史分析运行中，请稍后再试。", 429)
    try:
        head = _read_git(workspace, git, "rev-parse", "--verify", "--quiet", "HEAD", missing_ok=True).strip()
        raw = b""
        tracked = set()
        if head:
            if not _OID.fullmatch(head):
                raise GraphFlowError("Git 版本编号格式无效。", 503)
            tracked = _tracked_paths(workspace, git)
            raw = _read_git(workspace, git, "log", f"--max-count={MAX_COMMITS}", "--no-merges", "--no-color", "--no-show-signature",
                            "--format=format:%x00%H%x00", "--name-only", "-z", "--no-renames", "--no-ext-diff",
                            "--no-textconv", head.decode("ascii"), "--")
        scanned = skipped = 0
        file_counts, pair_counts = Counter(), Counter()
        for paths in _commits(raw):
            scanned += 1
            if len(paths) > MAX_COMMIT_FILES:
                skipped += 1
                continue
            eligible = {os.fsdecode(path) for path in paths if path in tracked}
            if allowed_files is not None:
                eligible.intersection_update(allowed_files)
            file_counts.update(eligible)
            pair_counts.update(pair for pair in combinations(sorted(eligible), 2) if not target or target in pair)
        rows = []
        for (a, b), count in pair_counts.items():
            confidence = round(count / min(file_counts[a], file_counts[b]), 2)
            if count >= 2 and confidence >= .5:
                rows.append({"files": [a, b], "co_commit_count": count, "confidence": confidence})
        rows.sort(key=lambda row: (-row["co_commit_count"], -row["confidence"], row["files"]))
        report = {"commits_scanned": scanned, "bulk_commits_skipped": skipped, "commit_limit": MAX_COMMITS,
                  "pairs": rows[:MAX_PAIRS], "more": len(rows) > MAX_PAIRS,
                  "analysis": "historical_correlation", "persisted": False}
        while report["pairs"] and len(json.dumps(report, ensure_ascii=False)) > budget:
            report["pairs"].pop()
            report["more"] = True
        return report
    finally:
        _GATE.release()
