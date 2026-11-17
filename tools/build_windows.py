"""打出 Windows 免安装版：dist/星愿记忆/

  星愿记忆.exe        无命令窗口启动器（图标 = assets/starwish.ico）
  runtime/            内置 Python 3.12（官方 embeddable）+ 全部依赖，已预装，无需联网
  app/ web/ ...       程序本体
  models/embed/small  内置向量模型 bge-small-zh int8
  models/voice/       宇宙之声声纹模型（预先下载）

单文件版：dist/星愿记忆-windows-amd64.exe（--onefile，默认同时生成）
  = tools/launcher/onefile.c 启动器 + 上面整个文件夹的 zip + 64 字节尾部。
  首次运行解压到 %LOCALAPPDATA%\StarWish\runtime\<版本-哈希>\，以后直接启动；
  记忆数据在 exe 旁边的 soul\ 文件夹。

用法（可在 Linux 上交叉构建，需要 x86_64-w64-mingw32-gcc / windres）：
  python tools/build_windows.py [--cache build/cache] [--out dist] [--no-voice-models]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
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
    runtime_pth(rt)
    install_wheels(wheels(cache), rt / "Lib" / "site-packages")


def runtime_pth(rt: Path):
    tag = "".join(PY_VER.split(".")[:2])
    (rt / f"python{tag}._pth").write_text(
        f"python{tag}.zip\n.\nLib\\site-packages\n..\nimport site\n", encoding="utf-8")


def _plain_runtime_path(path: Path, directory: bool = False) -> bool:
    info = path.lstat()
    type_check = stat.S_ISDIR if directory else stat.S_ISREG
    return type_check(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def runtime_inputs(directory: Path) -> tuple[Path, tuple[Path, ...]]:
    source = directory.resolve()
    tag = "".join(PY_VER.split(".")[:2])
    site = source / "Lib" / "site-packages"
    required = {"python.exe", "pythonw.exe", f"python{tag}.dll", f"python{tag}.zip", "LICENSE.txt"}
    try:
        core = tuple(sorted(p for p in source.iterdir()
                            if p.name in required or p.suffix.lower() in {".dll", ".pyd"}))
        if not required.issubset(p.name for p in core) or not all(_plain_runtime_path(p) for p in core):
            raise ValueError
        if not all(_plain_runtime_path(p, directory=True) for p in (site.parent, site)):
            raise ValueError
        for root, dirs, files in os.walk(site, onerror=_raise_runtime_walk_error):
            if any(not _plain_runtime_path(Path(root) / name, directory=True) for name in dirs):
                raise ValueError
            if any(not _plain_runtime_path(Path(root) / name) for name in files):
                raise ValueError
        probe = (
            "import platform, sys, pathlib; "
            f"assert platform.python_version() == {PY_VER!r} and platform.machine().upper() == 'AMD64' "
            "and sys.platform == 'win32' and sys.maxsize > 2**32; "
            "import fastapi, mcp, uvicorn, sqlite3, webview, onnxruntime, sherpa_onnx; "
            "site = pathlib.Path(sys.argv[1]); "
            "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(site) "
            "for m in (fastapi, mcp, uvicorn, webview, onnxruntime, sherpa_onnx))"
        )
        subprocess.run([str(source / "python.exe"), "-I", "-B", "-c", probe, str(site)],
                       cwd=source, check=True, timeout=30, capture_output=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"--runtime-dir requires a complete, trusted Windows x64 Python {PY_VER} runtime.") from exc
    return site, core


def _raise_runtime_walk_error(error: OSError) -> None:
    raise error


def copy_runtime(out: Path, inputs: tuple[Path, tuple[Path, ...]]):
    site, core = inputs
    destination = out / "runtime"
    destination.mkdir()
    for path in core:
        shutil.copy2(path, destination / path.name)
    shutil.copytree(site, destination / "Lib" / "site-packages",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    runtime_pth(destination)


def launcher(out: Path, *, cc="x86_64-w64-mingw32-gcc", windres="x86_64-w64-mingw32-windres"):
    src = ROOT / "tools" / "launcher"
    res = out / "_launcher.res"
    sh(windres, "-c", "65001", src / "launcher.rc", "-O", "coff", "-o", res, cwd=src)
    sh(cc, "-O2", "-s", "-municode", "-mwindows", "-static",
       "-o", out / f"{APP_NAME}.exe", src / "launcher.c", res)
    res.unlink()


ONEFILE_NAME = f"{APP_NAME}-windows-amd64.exe"
MAGIC = b"SWPKG01\0"


def make_trailer(offset: int, size: int, tag: str) -> bytes:
    import struct
    t = tag.encode("utf-8")[:39]
    return MAGIC + struct.pack("<QQ", offset, size) + t + b"\0" * (40 - len(t))


def read_trailer(exe: Path) -> dict:
    """测试用：读回尾部。"""
    import struct
    with exe.open("rb") as stream:
        stream.seek(-64, os.SEEK_END)
        total = stream.tell() + 64
        tr = stream.read(64)
    assert tr[:8] == MAGIC, "魔数不对"
    off, size = struct.unpack("<QQ", tr[8:24])
    return {"offset": off, "size": size, "tag": tr[24:].split(b"\0")[0].decode(), "total": total}


def release_source(commit: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit):
        raise SystemExit("--release-commit requires a full lowercase Git commit hash.")
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=15).strip()
        changes = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"],
                                          cwd=ROOT, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit("Cannot verify the release checkout.") from exc
    if head != commit or changes.strip():
        raise SystemExit("--release-commit requires a clean checkout at the specified commit.")
    return head


def write_release_manifest(exe: Path, version: str, source_commit: str | None = None) -> Path:
    trailer = read_trailer(exe)
    offset, size = trailer["offset"], trailer["size"]
    if offset < 2 or size == 0 or offset + size + 64 != trailer["total"]:
        raise ValueError("Invalid Windows artifact ranges.")
    artifact, payload = hashlib.sha256(), hashlib.sha256()
    with exe.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            start = stream.tell() - len(chunk)
            left, right = max(0, offset - start), min(len(chunk), offset + size - start)
            if left < right:
                payload.update(chunk[left:right])
            artifact.update(chunk)
    digest = payload.hexdigest()
    if trailer["tag"] != f"{version}-{digest[:10]}":
        raise ValueError("Windows artifact payload and version tag do not match.")
    info = {"artifact": exe.name, "version": version, "platform": "windows-amd64",
            "bytes": trailer["total"], "sha256": artifact.hexdigest(), "payload_sha256": digest,
            "cache_tag": trailer["tag"], "source_commit": source_commit}
    dest = exe.with_suffix(".json")
    temp = dest.with_suffix(".json.tmp")
    temp.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, dest)
    return dest


def onefile(folder: Path, dest: Path, version: str, *, cc="x86_64-w64-mingw32-gcc",
            windres="x86_64-w64-mingw32-windres") -> Path:
    import tempfile
    src = ROOT / "tools" / "launcher"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        res, stub = td / "onefile.res", td / "stub.exe"
        sh(windres, "-c", "65001", src / "onefile.rc", "-O", "coff", "-o", res, cwd=src)
        sh(cc, "-O2", "-s", "-municode", "-mwindows", "-static", "-o", stub,
           src / "onefile.c", src / "miniz" / "miniz.c", res, "-lcomctl32", "-lshell32", cwd=src)
        return pack_onefile(folder, dest, version, stub.read_bytes())


def pack_onefile(folder: Path, dest: Path, version: str, stub: bytes) -> Path:
    import tempfile
    if not stub.startswith(b"MZ"):
        raise ValueError("The onefile stub must be a Windows executable.")
    with tempfile.TemporaryDirectory() as td:
        payload = Path(td) / "payload.zip"
        skip = {f"{APP_NAME}.exe", "MCP接入.bat"}   # 单文件版用 exe 本身启动、用 exe --mcp 接入
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for f in sorted(folder.rglob("*")):
                rel = f.relative_to(folder).as_posix()
                if rel in skip or not f.is_file():
                    continue
                z.write(f, rel)
        digest = hashlib.sha256()
        with payload.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        h = digest.hexdigest()[:10]
        tag = f"{version}-{h}"
        with open(dest, "wb") as o:
            o.write(stub)
            with open(payload, "rb") as p:
                shutil.copyfileobj(p, o, 1 << 20)
            o.write(make_trailer(len(stub), payload.stat().st_size, tag))
    print(f"单文件版：{dest}  {dest.stat().st_size / 1e6:.1f} MB  标签 {tag}")
    return dest


def embedding_assets(models_dir: Path | None = None) -> tuple[Path, Path]:
    root = models_dir or Path(os.environ.get("STARWISH_MODELS_DIR") or (ROOT / "models"))
    small = root.resolve() / "embed" / "small"
    files = (small / "model.onnx", small / "tokenizer.json")
    if any(not path.is_file() or not path.stat().st_size for path in files):
        raise SystemExit("缺少内置向量模型：请用 --models-dir 指向含 embed/small/model.onnx 和 tokenizer.json 的模型目录。")
    return files


def copy_app(out: Path, *, models_dir: Path | None = None):
    files = embedding_assets(models_dir)
    for name in COPY:
        s = ROOT / name
        if not s.exists():
            continue
        d = out / name
        if s.is_dir():
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(s, d)
    copy_graphflow_tools(out)
    strip_dev_text(out)
    small = out / "models" / "embed" / "small"
    small.mkdir(parents=True)
    for path in files:
        shutil.copy2(path, small / path.name)
    (out / "MCP接入.bat").write_bytes(
        "@echo off\r\nchcp 65001 >nul\r\nREM 供 Claude Desktop / Cursor 等以 MCP stdio 方式调用\r\n"
        'cd /d "%~dp0"\r\n"runtime\\python.exe" mcp_server.py\r\n'.encode("utf-8"))


def copy_graphflow_tools(out: Path):
    tools = out / "tools"
    lock = tools / "graphflow_runtime"
    lock.mkdir(parents=True)
    for name in ("graphflow_engine.py", "graphflow_codex.py", "graphflow_codex_probe.py"):
        shutil.copy2(ROOT / "tools" / name, tools / name)
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(ROOT / "tools" / "graphflow_runtime" / name, lock / name)


def voice_assets(directory: Path) -> tuple[Path, ...]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.voice.models import MODELS

    source = directory.resolve()
    files = tuple(source / spec["file"] for spec in MODELS.values())
    if any(path.is_symlink() or not path.is_file() or path.stat().st_size <= 1000 for path in files):
        raise SystemExit("--voice-models-dir requires both local, non-empty StarWish voice ONNX files.")
    return files


def voice_models(out: Path, files: tuple[Path, ...] = ()):
    if files:
        destination = out / "models" / "voice"
        destination.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, destination / path.name)
        return
    os.environ["STARWISH_MODELS_DIR"] = str(out / "models")
    sys.path.insert(0, str(ROOT))
    from app import config
    from app.voice import models as vm

    cfg = config.load_config()
    r = vm.install(cfg.get("voice", {}), deps=False, models=True, wait=True)
    print("声纹模型：", r)


TEST_WORDS = re.compile(r"测试|测过|实测|自测|tests?/|_test\\.py|\\d+/\\d+ 通过|模拟上游|模拟源|模拟接口|内存替身|实机|验证|沙箱")


def _drop_sections(text: str, heads: tuple) -> str:
    out, skip = [], False
    for ln in text.splitlines(keepends=True):
        if ln.startswith("## "):
            skip = any(h in ln for h in heads)
        if not skip:
            out.append(ln)
    return "".join(out)


def strip_dev_text(out: Path) -> None:
    """发布包只给用户用：去掉开发用的测试说明、测试结果、设计稿。源码仓库里的文档不动。"""
    shutil.rmtree(out / "docs" / "superpowers", ignore_errors=True)
    for name in ("README.md", "项目说明.md", "使用说明书.md", "CHANGELOG.md"):
        f = out / name
        if not f.exists():
            continue
        t = _drop_sections(f.read_text("utf-8"), ("测试", "自测", "尚未实测"))
        keep, in_code = [], False
        for ln in t.splitlines(keepends=True):
            if ln.lstrip().startswith("```"):
                in_code = not in_code
            if TEST_WORDS.search(ln):
                continue
            keep.append(ln)
        t = re.sub(r"```(?:bash)?\n\s*```\n?", "", "".join(keep))
        t = re.sub(r"\n{3,}", "\n\n", t)
        f.write_text(t, "utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "build" / "cache"))
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--models-dir", type=Path, help="模型目录，缺省使用 STARWISH_MODELS_DIR 或源码下的 models")
    ap.add_argument("--runtime-dir", type=Path, help="复用可信的本机嵌入式 Python runtime，不下载 wheels")
    voice = ap.add_mutually_exclusive_group()
    voice.add_argument("--no-voice-models", action="store_true")
    voice.add_argument("--voice-models-dir", type=Path, help="使用本机已有的两个声纹 ONNX 模型，不下载")
    ap.add_argument("--no-onefile", action="store_true", help="只生成文件夹版")
    ap.add_argument("--release-commit", help="仅允许在对应的干净 Git 检出中标记发布来源")
    ap.add_argument("--cc", default="x86_64-w64-mingw32-gcc", help="Windows x64 MinGW-compatible C compiler")
    ap.add_argument("--windres", default="x86_64-w64-mingw32-windres", help="Windows x64 resource compiler")
    ap.add_argument("--with-graphflow", action="store_true", help="随包内置固定 GraphFlow 引擎（需要 Windows x64 Node.js/npm）")
    ap.add_argument("--graphflow-offline", action="store_true", help="仅从本机 npm 缓存准备随包引擎")
    a = ap.parse_args()
    if a.graphflow_offline and not a.with_graphflow:
        ap.error("--graphflow-offline requires --with-graphflow")
    if a.release_commit is not None and a.no_onefile:
        ap.error("--release-commit requires the single-file artifact")
    source_commit = release_source(a.release_commit) if a.release_commit is not None else None
    cache, out = Path(a.cache), Path(a.out) / APP_NAME
    files = embedding_assets(a.models_dir)
    voice_files = voice_assets(a.voice_models_dir) if a.voice_models_dir is not None else ()
    if any(path.resolve().is_relative_to(out.resolve()) for path in (*files, *voice_files)):
        ap.error("model inputs must be outside the build output directory")
    runtime_files = None
    if a.runtime_dir is not None:
        source, output = a.runtime_dir.resolve(), out.resolve()
        if source.is_relative_to(output) or output.is_relative_to(source):
            ap.error("--runtime-dir must not overlap the build output directory")
        runtime_files = runtime_inputs(source)
    cache.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    copy_app(out, models_dir=a.models_dir)
    if a.with_graphflow:
        if __package__:
            from .graphflow_engine import bundle_windows
        else:
            from graphflow_engine import bundle_windows

        print("GraphFlow:", bundle_windows(out.resolve(), offline=a.graphflow_offline))
    if runtime_files is None:
        runtime(cache, out)
    else:
        copy_runtime(out, runtime_files)
    launcher(out, cc=a.cc, windres=a.windres)
    if not a.no_voice_models:
        voice_models(out, voice_files)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"完成：{out}  共 {size / 1e6:.1f} MB")
    if not a.no_onefile:
        sys.path.insert(0, str(ROOT))
        from app import __version__
        exe = onefile(out, out.parent / ONEFILE_NAME, __version__, cc=a.cc, windres=a.windres)
        if source_commit is not None:
            release_source(source_commit)
        print("校验清单：", write_release_manifest(exe, __version__, source_commit))


if __name__ == "__main__":
    main()
