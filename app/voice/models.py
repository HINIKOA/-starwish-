"""宇宙之声的插件安装：依赖（sherpa-onnx + numpy）与两个模型，一键下载到程序目录 models/voice/。

模型不进灵魂包（soul/），所以不会跟着 WebDAV 备份走；换电脑时重新下载一次即可。
国内网络可以在设置里填「下载加速前缀」（例如 https://ghfast.top/）和 pip 镜像（例如清华源）。
"""
from __future__ import annotations

import hashlib
import importlib
import io
import os
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import httpx

from .. import config
from ..db import log_event

RELEASE_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

MODELS = {
    "embedding": {
        "title": "CAM++ 中英声纹模型（3D-Speaker，Apache-2.0）",
        "file": "campplus_zh_en_advanced.onnx",
        "path": "speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
        "size": 28281164,
        "license": "Apache-2.0",
    },
    "segmentation": {
        "title": "pyannote 分割模型 3.0 int8（MIT）",
        "file": "pyannote_segmentation_3_0_int8.onnx",
        "path": "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
        "member": "model.int8.onnx",
        "size": 6958444,       # 压缩包大小；解出来的 int8 模型约 1.5MB
        "license": "MIT",
    },
}
DEPS = {"numpy": "numpy", "sherpa_onnx": "sherpa-onnx"}
DEPS_SIZE_MB = 32          # Windows：sherpa-onnx-core ≈16.7MB + sherpa-onnx ≈2.2MB + numpy ≈13MB

_lock = threading.RLock()
_jobs: dict[str, dict] = {}


def models_dir() -> Path:
    d = Path(os.environ.get("SOULSTAR_MODELS_DIR") or (config.APP_DIR / "models")) / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path(kind: str) -> Path:
    return models_dir() / MODELS[kind]["file"]


def dep_status() -> dict:
    out = {}
    for mod, pkg in DEPS.items():
        try:
            m = importlib.import_module(mod)
            out[mod] = {"ok": True, "version": getattr(m, "__version__", "?"), "pip": pkg}
        except Exception as e:  # noqa: BLE001
            out[mod] = {"ok": False, "error": type(e).__name__, "pip": pkg}
    return out


def model_status() -> dict:
    out = {}
    for k, m in MODELS.items():
        p = model_path(k)
        out[k] = {"title": m["title"], "license": m["license"], "installed": p.exists() and p.stat().st_size > 1000,
                  "bytes": p.stat().st_size if p.exists() else 0, "download_bytes": m["size"]}
    return out


def ready(backend: str = "sherpa") -> bool:
    if backend == "fake":
        return dep_status()["numpy"]["ok"]
    ds, ms = dep_status(), model_status()
    return all(d["ok"] for d in ds.values()) and all(m["installed"] for m in ms.values())


def status(backend: str = "sherpa") -> dict:
    ds, ms = dep_status(), model_status()
    need = sum(m["download_bytes"] for m in ms.values() if not m["installed"]) / 1e6
    need += 0 if all(d["ok"] for d in ds.values()) else DEPS_SIZE_MB
    return {"backend": backend, "ready": ready(backend), "deps": ds, "models": ms, "models_dir": str(models_dir()),
            "need_mb": round(need, 1), "frozen": bool(getattr(sys, "frozen", False)), "jobs": jobs()}


def jobs() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _jobs.items()}


def _job(name: str, **kw) -> dict:
    with _lock:
        j = _jobs.setdefault(name, {})
        j.update(kw, t=time.time())
        return dict(j)


def _url(path: str, vcfg: dict) -> str:
    base = (os.environ.get("SOULSTAR_VOICE_MODEL_BASE") or vcfg.get("model_base_url") or RELEASE_BASE).rstrip("/")
    mirror = (vcfg.get("download_mirror") or "").strip()
    url = f"{base}/{path}"
    if mirror and base.startswith("https://github.com"):
        url = mirror.rstrip("/") + "/" + url
    return url


def _download(kind: str, vcfg: dict) -> None:
    m = MODELS[kind]
    name = f"model:{kind}"
    dest = model_path(kind)
    url = _url(m["path"], vcfg)
    _job(name, state="running", done=0, total=m["size"], url=url, error="")
    try:
        buf = io.BytesIO()
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            total = int(r.headers.get("content-length") or m["size"])
            _job(name, total=total)
            for chunk in r.iter_bytes(1 << 16):
                buf.write(chunk)
                _job(name, done=buf.tell())
        data = buf.getvalue()
        if m.get("member"):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tf:
                mem = next((x for x in tf.getmembers() if x.name.endswith("/" + m["member"]) or x.name == m["member"]), None)
                if not mem:
                    raise RuntimeError(f"压缩包里没有 {m['member']}")
                data = tf.extractfile(mem).read()
        if len(data) < 1000:
            raise RuntimeError("下载的文件太小，可能被拦截了")
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        _job(name, state="done", done=len(data), sha256=hashlib.sha256(data).hexdigest()[:16])
        log_event("voice_model_download", kind=kind, bytes=len(data))
    except Exception as e:  # noqa: BLE001
        _job(name, state="error", error=str(e)[:200])
        log_event("voice_model_download_failed", kind=kind, error=str(e)[:200])


def _pip(vcfg: dict) -> None:
    name = "deps"
    missing = [d["pip"] for d in dep_status().values() if not d["ok"]]
    if not missing:
        _job(name, state="done", log="依赖已经装好了")
        return
    if getattr(sys, "frozen", False):
        _job(name, state="error", error="打包版不能在线装 Python 依赖，请用源码版或下载带插件的版本")
        return
    cmd = os.environ.get("SOULSTAR_VOICE_PIP_CMD")
    args = cmd.split() + missing if cmd else [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]
    if vcfg.get("pip_index") and not cmd:
        args += ["-i", vcfg["pip_index"]]
    _job(name, state="running", log="正在安装：" + " ".join(missing), error="")
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=900)
        tail = (p.stdout + p.stderr)[-600:]
        importlib.invalidate_caches()
        if p.returncode != 0:
            _job(name, state="error", error=f"pip 退出码 {p.returncode}", log=tail)
        else:
            _job(name, state="done", log=tail)
        log_event("voice_pip", ok=p.returncode == 0, pkgs=missing)
    except Exception as e:  # noqa: BLE001
        _job(name, state="error", error=str(e)[:200])


def install(vcfg: dict, deps: bool = True, models: bool = True, wait: bool = False) -> dict:
    """后台安装；返回当前任务状态。wait=True 时等它装完（测试用）。"""
    threads = []
    if deps and not all(d["ok"] for d in dep_status().values()) and (_jobs.get("deps", {}).get("state") != "running"):
        threads.append(threading.Thread(target=_pip, args=(vcfg,), daemon=True))
    if models:
        for k in MODELS:
            if not model_status()[k]["installed"] and _jobs.get(f"model:{k}", {}).get("state") != "running":
                threads.append(threading.Thread(target=_download, args=(k, vcfg), daemon=True))
    for t in threads:
        t.start()
    if wait:
        for t in threads:
            t.join(600)
    return jobs()


def uninstall_models() -> int:
    n = 0
    for k in MODELS:
        p = model_path(k)
        if p.exists():
            p.unlink()
            n += 1
    with _lock:
        for k in list(_jobs):
            if k.startswith("model:"):
                del _jobs[k]
    log_event("voice_model_remove", count=n)
    return n
