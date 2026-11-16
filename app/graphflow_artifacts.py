"""Portable workspace manifests; never install externally supplied AST data."""
from __future__ import annotations

import base64
import binascii
import gzip
import io
import json
import subprocess
import zlib

from . import graphflow
from .graphflow import GraphFlowError

FORMAT = "starwish-index-manifest-v1"
MAX_WIRE = 320_000
MAX_PAYLOAD = 2_000_000


def _decode(payload: str) -> dict:
    if not isinstance(payload, str) or not payload or len(payload) > MAX_WIRE:
        raise GraphFlowError("请提供大小合规的星愿 gzip/base64 索引清单。")
    try:
        data = base64.b64decode(payload, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(MAX_PAYLOAD + 1)
        if len(raw) > MAX_PAYLOAD:
            raise GraphFlowError("索引清单解压后超出限制。", 413)
        manifest = json.loads(raw)
    except GraphFlowError:
        raise
    except (ValueError, OSError, EOFError, RecursionError, binascii.Error, zlib.error) as exc:
        raise GraphFlowError("索引清单不是有效的 gzip/base64 JSON。") from exc
    if (not isinstance(manifest, dict) or set(manifest) != {"format", "engine", "files"}
            or manifest["format"] != FORMAT or manifest["engine"] != graphflow.VERSION
            or not isinstance(manifest["files"], dict) or len(manifest["files"]) > graphflow.MAX_FILES):
        raise GraphFlowError("索引清单格式或引擎版本不受支持。")
    return manifest


def exchange(mode: str, artifact: str = "", rootDir: str = "", inputPath: str = "", outputPath: str = "",
             compression: str = "gzip") -> dict:
    if mode not in ("export", "import") or inputPath != "" or outputPath != "" or compression != "gzip":
        raise GraphFlowError("只支持 gzip 内联清单，不接受输入或输出文件路径。")
    if mode == "export" and artifact != "":
        raise GraphFlowError("导出不接受已有清单。")
    workspace = graphflow.checked_workspace(rootDir)
    supplied = _decode(artifact) if mode == "import" else None
    node, entry, git = graphflow._engine()
    if not graphflow._gate.acquire(blocking=False):
        raise GraphFlowError("已有索引操作运行中，请稍后再试。", 429)
    try:
        snapshot, partial, _ = graphflow._sync_snapshot(workspace, git)
        if partial:
            raise GraphFlowError("当前快照超出容量，不能交换不完整清单。", 413)
        if not snapshot.files:
            raise GraphFlowError("没有可索引的已跟踪源码。", 404)
        manifest = {"format": FORMAT, "engine": graphflow.VERSION,
                    "files": {name: digest.hex() for name, digest in snapshot.files.items()}}
        result = {"mode": mode, "format": FORMAT, "file_count": len(snapshot.files), "workspace_written": False}
        if supplied is None:
            raw = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            payload = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
            if len(raw) > MAX_PAYLOAD or len(payload) > MAX_WIRE:
                raise GraphFlowError("索引清单超出传输限制。", 413)
            return {**result, "encoding": "gzip+base64", "artifact": payload}
        if supplied != manifest:
            raise GraphFlowError("清单与当前源码不一致；未预热索引。", 409)
        files = [name for name in snapshot.files if name not in snapshot.warmed]
        warmed = graphflow._call_bridge(snapshot, node, entry, warmFiles=files) if files else []
        if not isinstance(warmed, list) or any(not isinstance(name, str) or name not in snapshot.files for name in warmed):
            raise GraphFlowError("引擎返回了无效的索引结果。", 503)
        snapshot.warmed.update({name: snapshot.files[name] for name in warmed})
        return {**result, "verified": True, "index_scope": "process", "warmed": len(warmed),
                "already_warm": len(snapshot.warmed) - len(warmed), "unparsed": len(files) - len(warmed)}
    except subprocess.TimeoutExpired as exc:
        raise GraphFlowError("索引清单处理超时。", 504) from exc
    except (OSError, UnicodeError) as exc:
        raise GraphFlowError("无法处理索引清单。", 503) from exc
    finally:
        graphflow._gate.release()
