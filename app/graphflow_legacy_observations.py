"""Read original GraphFlow observation files, never execute the recorded command."""
import hashlib
import os
import re
import stat
from pathlib import Path

from . import config
from .graphflow import GraphFlowError


ENV = "STARWISH_GRAPHFLOW_LEGACY_OBSERVATIONS"
HANDLE = re.compile(r"gfo:([0-9a-f]{12})")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def read(workspace: Path, digest: str, limit: int) -> list[str]:
    if os.environ.get(ENV) != "1":
        raise GraphFlowError("未授权 Agent 读取旧 GraphFlow 日志。", 403)
    if not HANDLE.fullmatch("gfo:" + digest):
        raise GraphFlowError("旧日志句柄格式无效。")
    directory = workspace / ".graphflow" / "observations"
    path = directory / f"{digest}.log"
    try:
        if path.is_relative_to(config.SOUL_DIR.resolve()) or path.resolve() != path:
            raise GraphFlowError("旧日志不在可读取范围内。", 403)
        for parent in (directory.parent, directory):
            info = parent.lstat()
            if _linked(info) or not stat.S_ISDIR(info.st_mode):
                raise GraphFlowError("旧日志目录不在可读取范围内。", 403)
        info = path.lstat()
        if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise GraphFlowError("旧日志仅支持普通文件。", 403)
        if info.st_size > limit:
            raise GraphFlowError("旧日志超过可读取限制。", 413)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not os.path.samestat(info, opened):
                raise GraphFlowError("旧日志文件已变化。", 409)
            data = stream.read(limit + 1)
    except FileNotFoundError as exc:
        raise GraphFlowError("当前项目没有该旧日志。", 404) from exc
    except (OSError, RuntimeError) as exc:
        raise GraphFlowError("旧日志暂不可读。", 503) from exc
    if len(data) > limit:
        raise GraphFlowError("旧日志超过可读取限制。", 413)
    if not hashlib.sha256(data).hexdigest().startswith(digest):
        raise GraphFlowError("旧日志内容与句柄不符。", 409)
    try:
        return re.split(r"\r?\n", data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise GraphFlowError("旧日志不是 UTF-8 文本。", 422) from exc
