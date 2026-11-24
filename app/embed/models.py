"""本地向量模型的下载与依赖安装。模型放在程序目录 models/embed/<档位>/，不进灵魂包、不跟 WebDAV 备份走。"""
from __future__ import annotations

import importlib
import os
import stat
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


class ModelBusyError(RuntimeError):
    pass


def models_root() -> Path:
    return Path(os.environ.get("STARWISH_MODELS_DIR") or (config.APP_DIR / "models")) / "embed"


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


def _model_files(kind: str, *, partial: bool = False) -> list[Path]:
    d = model_dir(kind)
    return [d / (f + (".part" if partial else "")) for f in LOCAL[kind]["files"]]


def _regular_file(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("模型目录中存在非普通文件，未清除。")
    return info


def local_status(kind: str) -> dict:
    m = LOCAL[kind]
    d = model_dir(kind)

    def info(p):
        try:
            return _regular_file(p)
        except (OSError, ValueError):
            return None

    files = {p.name: data.st_size for p in _model_files(kind) if (data := info(p)) is not None}
    parts = [data.st_size for p in _model_files(kind, partial=True) if (data := info(p)) is not None]
    need = sum(s for f, (_, s) in m["files"].items() if files.get(f, 0) <= 1000)
    return {"title": m["title"], "repo": m["repo"], "dim": m["dim"], "license": m["license"], "lang": m["lang"],
            "installed": need == 0, "bytes": sum(files.values()), "partial_bytes": sum(parts),
            "has_files": bool(files or parts), "need_mb": round(need / 1e6, 1), "dir": str(d),
            "job": jobs().get(f"model:{kind}")}


def _urls(rel: str, ecfg: dict, repo: str) -> list[str]:
    env = os.environ.get("STARWISH_EMBED_MODEL_BASE")
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
    total = sum(s for _, s in m["files"].values())
    _job(name, state="running", done=0, total=total, error="", url="")
    done = 0
    try:
        d.mkdir(parents=True, exist_ok=True)
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
        _job(name, state="done", log="运行库已就绪")
        return
    if getattr(sys, "frozen", False):
        _job(name, state="error", error="打包版不能在线装 Python 依赖")
        return
    cmd = os.environ.get("STARWISH_EMBED_PIP_CMD")
    args = cmd.split() + missing if cmd else [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]
    if ecfg.get("pip_index") and not cmd:
        args += ["-i", ecfg["pip_index"]]
    _job(name, state="running", phase="installing", log="正在安装：" + "、".join(missing), error="")
    try:
        p = subprocess.run(args, capture_output=True, timeout=900)
        if p.returncode != 0:
            _job(name, state="error", error=f"安装程序退出码 {p.returncode}。请检查网络和 Python 运行环境后重试。")
            log_event("embed_pip", ok=False, pkgs=missing)
            return
        _job(name, phase="verifying", log="安装命令已结束，正在验证运行库")
        importlib.invalidate_caches()
        unavailable = [mod for mod, dep in dep_status().items() if not dep["ok"]]
        log_event("embed_pip", ok=not unavailable, pkgs=missing)
        if unavailable:
            _job(name, state="error", error="安装命令已结束，但仍无法加载：" + "、".join(unavailable))
            return
        from .. import semantic
        semantic.reset()
        semantic.start_backfill()
        _job(name, state="done", log="运行库已安装并通过加载检查")
    except subprocess.TimeoutExpired:
        _job(name, state="error", error="安装超时，请检查网络后重试。")
    except Exception as e:  # noqa: BLE001
        _job(name, state="error", error=f"运行库安装或验证失败（{type(e).__name__}）。")


def install(kind: str, ecfg: dict, deps: bool = True, wait: bool = False) -> dict:
    threads = []
    need_deps = deps and not deps_ok()
    # Reserve jobs before starting workers so status never misses a submitted install.
    with _lock:
        if need_deps and not running("deps"):
            _job("deps", state="running", phase="queued", error="", log="正在准备安装运行库")
            threads.append(("deps", threading.Thread(target=_pip, args=(ecfg,), daemon=True)))
        name = f"model:{kind}"
        if kind in LOCAL and not installed(kind) and not running(name):
            _job(name, state="running", done=0, total=0, error="", url="")
            threads.append((name, threading.Thread(target=_download, args=(kind, ecfg), daemon=True)))
    for name, t in threads:
        try:
            t.start()
        except RuntimeError:
            _job(name, state="error", error="无法启动后台安装，请重试。")
    if wait:
        for _, t in threads:
            if t.ident is not None:
                t.join(1800)
    return jobs()


def uninstall(kind: str) -> int:
    if kind == "small":
        raise ValueError("内置模型是常驻默认，不能删除")
    from .. import semantic

    name = f"model:{kind}"
    n = 0
    with _lock:
        if running(name):
            raise ModelBusyError("BGE-M3 正在下载，请等任务结束再清除。")
        if model_dir(kind).resolve() != models_root().resolve() / kind:
            raise ValueError("模型目录指向了其他位置，未清除。")
        paths = _model_files(kind) + _model_files(kind, partial=True)
        files = [(p, info.st_size) for p in paths if (info := _regular_file(p)) is not None]
        if files:
            semantic.reset()
        for p, size in files:
            try:
                p.unlink()
            except FileNotFoundError:
                continue
            except OSError as ex:
                raise ModelBusyError("部分模型文件可能正在使用，请停止相关任务后重试清除。") from ex
            n += size
        _jobs.pop(name, None)
    log_event("embed_model_remove", kind=kind, bytes=n)
    return n
