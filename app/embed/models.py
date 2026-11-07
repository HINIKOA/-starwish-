"""本地向量模型的下载与依赖安装。模型放在程序目录 models/embed/<档位>/，不进灵魂包、不跟 WebDAV 备份走。"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from .. import config
from ..db import log_event

# 两个本地档位：都用 Xenova 导出的 ONNX（输出 last_hidden_state，取 CLS 再归一化）
LOCAL = {
    "small": {
        "title": "bge-small-zh-v1.5 int8（内置 · 常驻默认）",
        "repo": "Xenova/bge-small-zh-v1.5",
        "files": {"model.onnx": ("onnx/model_int8.onnx", 23903394), "tokenizer.json": ("tokenizer.json", 439125)},
        "dim": 512, "max_len": 512, "license": "MIT", "lang": "中文为主",
        "min_sim": 0.44,
    },
    "m3": {
        "title": "BGE-M3 int8（可选 · 一键部署）",
        "repo": "Xenova/bge-m3",
        "files": {"model.onnx": ("onnx/model_quantized.onnx", 570 * 1024 * 1024), "tokenizer.json": ("tokenizer.json", 17_100_000)},
        "dim": 1024, "max_len": 1024, "license": "MIT", "lang": "中英等 100+ 语言，长文本更强",
        "min_sim": 0.45,
    },
}
DEPS = {"numpy": "numpy", "onnxruntime": "onnxruntime", "tokenizers": "tokenizers"}
DEPS_SIZE_MB = 31  # Windows：onnxruntime ≈14.9MB + tokenizers ≈2.9MB + numpy ≈13MB

_lock = threading.RLock()
_jobs: dict[str, dict] = {}


def models_root() -> Path:
    return Path(os.environ.get("SOULSTAR_MODELS_DIR") or (config.APP_DIR / "models")) / "embed"


def model_dir(kind: str) -> Path:
    return models_root() / kind


def installed(kind: str) -> bool:
    d = model_dir(kind)
    return all((d / f).exists() and (d / f).stat().st_size > 1000 for f in LOCAL[kind]["files"])


def dep_status() -> dict:
    out = {}
    for mod, pkg in DEPS.items():
        try:
            m = importlib.import_module(mod)
            out[mod] = {"ok": True, "version": getattr(m, "__version__", "?"), "pip": pkg}
        except Exception as e:  # noqa: BLE001
            out[mod] = {"ok": False, "error": type(e).__name__, "pip": pkg}
    return out


def deps_ok() -> bool:
    return all(d["ok"] for d in dep_status().values())


def jobs() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _jobs.items()}


def _job(name: str, **kw) -> dict:
    with _lock:
        j = _jobs.setdefault(name, {})
        j.update(kw, t=time.time())
        return dict(j)


def running(name: str) -> bool:
    return _jobs.get(name, {}).get("state") == "running"


def local_status(kind: str) -> dict:
    m = LOCAL[kind]
    d = model_dir(kind)
    size = sum((d / f).stat().st_size for f in m["files"] if (d / f).exists())
    need = sum(s for f, (_, s) in m["files"].items() if not ((d / f).exists() and (d / f).stat().st_size > 1000))
    return {"title": m["title"], "repo": m["repo"], "dim": m["dim"], "license": m["license"], "lang": m["lang"],
            "installed": installed(kind), "bytes": size, "need_mb": round(need / 1e6, 1), "dir": str(d),
            "job": jobs().get(f"model:{kind}")}


def _urls(rel: str, ecfg: dict, repo: str) -> list[str]:
    env = os.environ.get("SOULSTAR_EMBED_MODEL_BASE")
    if env:
        return [f"{env.rstrip('/')}/{repo}/resolve/main/{rel}"]
    bases = [ecfg.get("hf_base") or "https://huggingface.co"]
    if ecfg.get("hf_mirror"):
        bases.append(ecfg["hf_mirror"])
    if ecfg.get("prefer_mirror") and len(bases) > 1:
        bases.reverse()
    return [f"{b.rstrip('/')}/{repo}/resolve/main/{rel}" for b in bases]


def _fetch(url: str, dest: Path, name: str, done_before: int, total: int) -> int:
    tmp = dest.with_suffix(dest.suffix + ".part")
    n = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30, read=180)) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        if "text/html" in (r.headers.get("content-type") or ""):
            raise RuntimeError("拿到的是网页不是模型（可能被拦截了）")
        with open(tmp, "wb") as f:  # 边下边写，大模型不占内存
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                n += len(chunk)
                _job(name, done=done_before + n, total=total)
    if n < 1000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载的文件太小")
    tmp.replace(dest)
    return n


def _download(kind: str, ecfg: dict) -> None:
    m = LOCAL[kind]
    name = f"model:{kind}"
    d = model_dir(kind)
    d.mkdir(parents=True, exist_ok=True)
    total = sum(s for _, s in m["files"].values())
    _job(name, state="running", done=0, total=total, error="", url="")
    done = 0
    try:
        for fname, (rel, size) in m["files"].items():
            dest = d / fname
            if dest.exists() and dest.stat().st_size > 1000:
                done += dest.stat().st_size
                continue
            errs = []
            for url in _urls(rel, ecfg, m["repo"]):
                _job(name, url=url)
                try:
                    done += _fetch(url, dest, name, done, total)
                    break
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{url.split('/')[2]}: {e}")
            else:
                raise RuntimeError("；".join(errs))
        _job(name, state="done", done=total)
        log_event("embed_model_download", kind=kind)
        from .. import semantic
        semantic.reset()
        semantic.start_backfill()
    except Exception as e:  # noqa: BLE001
        _job(name, state="error", error=str(e)[:300])
        log_event("embed_model_download_failed", kind=kind, error=str(e)[:200])


def _pip(ecfg: dict) -> None:
    name = "deps"
    missing = [d["pip"] for d in dep_status().values() if not d["ok"]]
    if not missing:
        _job(name, state="done", log="依赖已经装好了")
        return
    if getattr(sys, "frozen", False):
        _job(name, state="error", error="打包版不能在线装 Python 依赖")
        return
    cmd = os.environ.get("SOULSTAR_EMBED_PIP_CMD")
    args = cmd.split() + missing if cmd else [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]
    if ecfg.get("pip_index") and not cmd:
        args += ["-i", ecfg["pip_index"]]
    _job(name, state="running", log="正在安装：" + " ".join(missing), error="")
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=900)
        importlib.invalidate_caches()
        tail = (p.stdout + p.stderr)[-600:]
        _job(name, state="done" if p.returncode == 0 else "error", log=tail, error="" if p.returncode == 0 else f"pip 退出码 {p.returncode}")
        log_event("embed_pip", ok=p.returncode == 0, pkgs=missing)
        if p.returncode == 0:
            from .. import semantic
            semantic.reset()
            semantic.start_backfill()
    except Exception as e:  # noqa: BLE001
        _job(name, state="error", error=str(e)[:200])


def install(kind: str, ecfg: dict, deps: bool = True, wait: bool = False) -> dict:
    threads = []
    if deps and not deps_ok() and not running("deps"):
        threads.append(threading.Thread(target=_pip, args=(ecfg,), daemon=True))
    if kind in LOCAL and not installed(kind) and not running(f"model:{kind}"):
        threads.append(threading.Thread(target=_download, args=(kind, ecfg), daemon=True))
    for t in threads:
        t.start()
    if wait:
        for t in threads:
            t.join(1800)
    return jobs()


def uninstall(kind: str) -> int:
    if kind == "small":
        raise ValueError("内置模型是常驻默认，不能删除")
    n = 0
    d = model_dir(kind)
    for f in LOCAL[kind]["files"]:
        p = d / f
        if p.exists():
            n += p.stat().st_size
            p.unlink()
    from .. import semantic
    semantic.reset()
    log_event("embed_model_remove", kind=kind, bytes=n)
    return n
