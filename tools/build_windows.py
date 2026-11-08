"""打出 Windows 免安装版：dist/星愿记忆/

  星愿记忆.exe        无命令窗口启动器（图标 = assets/starwish.ico）
  runtime/            内置 Python 3.12（官方 embeddable）+ 全部依赖，已预装，无需联网
  app/ web/ ...       程序本体
  models/embed/small  内置向量模型 bge-small-zh int8
  models/voice/       宇宙之声声纹模型（预先下载）

用法（可在 Linux 上交叉构建，需要 x86_64-w64-mingw32-gcc / windres）：
  python tools/build_windows.py [--cache build/cache] [--out dist] [--no-voice-models]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_VER = "3.12.10"
PY_URLS = [f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip",
           f"https://registry.npmmirror.com/-/binary/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"]
# 只在 Windows 上才需要、pip 跨平台下载时不会自动带上的依赖
WIN_EXTRA = ["pythonnet", "colorama", "pywin32>=311"]
EXTRA = ["pywebview>=5", "sherpa-onnx>=1.12"]
SDIST_ONLY = ["jieba", "proxy_tools"]          # 纯 Python、只有源码包：本机打成 wheel
APP_NAME = "星愿记忆"
COPY = ["app", "web", "assets", "docs", "desktop.py", "run.py", "mcp_server.py", "requirements.txt",
        "README.md", "CHANGELOG.md", "LICENSE-NOTICE.md", "LICENSE-XINGXI.txt", "项目说明.md", "使用说明书.md"]


def sh(*a, **kw):
    print("+", " ".join(map(str, a)))
    subprocess.run(list(map(str, a)), check=True, **kw)


def fetch(urls, dest: Path):
    if dest.exists():
        return
    for u in urls:
        try:
            print("下载", u)
            urllib.request.urlretrieve(u, dest)
            return
        except Exception as e:  # noqa: BLE001
            print("  失败：", e)
    raise SystemExit(f"下载失败：{dest.name}")


def wheels(cache: Path) -> Path:
    wd = cache / "wheels"
    wd.mkdir(parents=True, exist_ok=True)
    if any(wd.glob("fastapi-*.whl")) and any(wd.glob("pywebview-*.whl")):
        return wd
    reqs = [l.strip() for l in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]
    reqs = [r for r in reqs if not any(r.lower().startswith(s) for s in SDIST_ONLY)]
    pipdl = [sys.executable, "-m", "pip", "download", "-q", "-d", wd, "--platform", "win_amd64",
             "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312", "--only-binary=:all:",
             "--find-links", wd]
    sh(sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", wd, *SDIST_ONLY)
    sh(*pipdl, *reqs, *EXTRA, *WIN_EXTRA)
    return wd


def install_wheels(wd: Path, site: Path):
    site.mkdir(parents=True, exist_ok=True)
    for w in sorted(wd.glob("*.whl")):
        with zipfile.ZipFile(w) as z:
            for n in z.namelist():
                parts = n.split("/")
                if parts[0].endswith(".data") and len(parts) > 2:
                    if parts[1] in ("purelib", "platlib"):
                        target = site / "/".join(parts[2:])
                    else:  # scripts / data / headers：桌面版用不到
                        continue
                else:
                    target = site / n
                if n.endswith("/"):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    # 清理测试与缓存，减小体积
    for p in list(site.rglob("__pycache__")) + [d for d in site.rglob("tests") if d.is_dir() and "numpy" not in str(d)]:
        shutil.rmtree(p, ignore_errors=True)
    # 桌面版用不到的大件：hf_xet（huggingface 加速下载）、pythonwin IDE 与帮助文档
    for n in ("hf_xet", "pythonwin", "PyWin32.chm"):
        p = site / n
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def runtime(cache: Path, out: Path):
    pyzip = cache / f"python-{PY_VER}-embed-amd64.zip"
    fetch(PY_URLS, pyzip)
    rt = out / "runtime"
    with zipfile.ZipFile(pyzip) as z:
        z.extractall(rt)
    tag = "".join(PY_VER.split(".")[:2])
    (rt / f"python{tag}._pth").write_text(
        f"python{tag}.zip\n.\nLib\\site-packages\n..\nimport site\n", encoding="utf-8")
    install_wheels(wheels(cache), rt / "Lib" / "site-packages")


def launcher(out: Path):
    src = ROOT / "tools" / "launcher"
    res = out / "_launcher.res"
    sh("x86_64-w64-mingw32-windres", "-c", "65001", src / "launcher.rc", "-O", "coff", "-o", res, cwd=src)
    sh("x86_64-w64-mingw32-gcc", "-O2", "-s", "-municode", "-mwindows", "-static",
       "-o", out / f"{APP_NAME}.exe", src / "launcher.c", res)
    res.unlink()


def copy_app(out: Path):
    for name in COPY:
        s = ROOT / name
        if not s.exists():
            continue
        d = out / name
        if s.is_dir():
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(s, d)
    small = ROOT / "models" / "embed" / "small"
    if not (small / "model.onnx").exists():
        raise SystemExit("缺少内置向量模型 models/embed/small/model.onnx")
    shutil.copytree(small, out / "models" / "embed" / "small")
    (out / "MCP接入.bat").write_bytes(
        "@echo off\r\nchcp 65001 >nul\r\nREM 供 Claude Desktop / Cursor 等以 MCP stdio 方式调用\r\n"
        'cd /d "%~dp0"\r\n"runtime\\python.exe" mcp_server.py\r\n'.encode("utf-8"))


def voice_models(out: Path):
    os.environ["STARWISH_MODELS_DIR"] = str(out / "models")
    sys.path.insert(0, str(ROOT))
    from app import config
    from app.voice import models as vm

    cfg = config.load_config()
    r = vm.install(cfg.get("voice", {}), deps=False, models=True, wait=True)
    print("声纹模型：", r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "build" / "cache"))
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--no-voice-models", action="store_true")
    a = ap.parse_args()
    cache, out = Path(a.cache), Path(a.out) / APP_NAME
    cache.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    copy_app(out)
    runtime(cache, out)
    launcher(out)
    if not a.no_voice_models:
        voice_models(out)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"完成：{out}  共 {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
