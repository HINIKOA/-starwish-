"""Check or provision the pinned StarWish engine without registering an MCP server."""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import graphflow

RUNTIME_DIR = Path(__file__).resolve().with_name("graphflow_runtime")
PACKAGE = "@roarpeng/graphflow"


def locked_runtime(directory: Path = RUNTIME_DIR) -> tuple[bytes, bytes, int]:
    manifest = (directory / "package.json").read_bytes()
    lockfile = (directory / "package-lock.json").read_bytes()
    manifest_json = json.loads(manifest)
    lock = json.loads(lockfile)
    pinned = {PACKAGE: graphflow.VERSION}
    packages = lock.get("packages", {})
    if (manifest_json.get("dependencies") != pinned or lock.get("lockfileVersion") != 3
            or packages.get("", {}).get("dependencies") != pinned
            or packages.get("node_modules/" + PACKAGE, {}).get("version") != graphflow.VERSION):
        raise RuntimeError("Bundled npm lock does not match the pinned GraphFlow version.")
    for name, meta in packages.items():
        if not name:
            continue
        url = meta.get("resolved", "")
        algorithm, _, digest = meta.get("integrity", "").partition("-")
        if (not name.startswith("node_modules/") or meta.get("link")
                or "\\" in name or ".." in PurePosixPath(name).parts
                or not url.startswith("https://registry.npmjs.org/") or not url.endswith(".tgz")
                or algorithm != "sha512" or len(base64.b64decode(digest, validate=True)) != 64):
            raise RuntimeError(f"Unpinned npm lock entry: {name}")
    return manifest, lockfile, len(packages) - 1


def check(prefix: Path) -> dict:
    node, entry, _ = graphflow._engine(prefix)
    with tempfile.TemporaryDirectory(prefix="starwish-engine-check-") as directory:
        root = Path(directory)
        (root / "lib.ts").write_text("export function starwishSmoke() { return 42; }\n", encoding="utf-8")
        (root / "entry.ts").write_text(
            "import { starwishSmoke } from './lib';\nexport const value = starwishSmoke();\n", encoding="utf-8")
        snap = SimpleNamespace(root=root)
        text = graphflow._call_bridge(snap, node, entry, targets=["lib.ts#starwishSmoke"], includeCallers=True)
        data = graphflow._call_bridge(snap, node, entry, graph={
            "target": "lib.ts", "nodeLimit": 8, "edgeLimit": 12, "files": ["lib.ts", "entry.ts"], "cochange": [],
        })
        if ("return 42" not in text or not any(row.get("symbol") == "starwishSmoke" for row in data["nodes"])
                or not {"calls", "imports", "defines"} <= {row["kind"] for row in data["edges"]}):
            raise RuntimeError("Pinned engine failed the slice or dependency graph smoke check.")
    return {"provider": graphflow.PROVIDER, "slice": "passed", "dependency_graph": "passed"}


def npm_command() -> list[str]:
    if os.name == "nt":
        node = shutil.which("node")
        npm = shutil.which("npm.cmd")
        if node and npm:
            cli = Path(npm).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
            if cli.is_file():
                return [node, str(cli)]
    elif npm := shutil.which("npm"):
        return [npm]
    raise RuntimeError("npm was not found alongside the Node.js installation.")


def install(prefix: Path, *, offline: bool = False) -> dict:
    if os.path.lexists(prefix):
        return {**check(prefix), "install": "already_present"}
    manifest, lockfile, packages = locked_runtime()
    command = npm_command()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".starwish-engine-", dir=prefix.parent) as directory:
        stage = Path(directory) / "package"
        stage.mkdir()
        (stage / "package.json").write_bytes(manifest)
        (stage / "package-lock.json").write_bytes(lockfile)
        args = [*command, "ci", "--prefix", str(stage), "--ignore-scripts", "--include=optional",
                "--no-bin-links", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org"]
        if offline:
            args.append("--offline")
        subprocess.run(args, cwd=stage, check=True, timeout=120)
        if (stage / "package-lock.json").read_bytes() != lockfile:
            raise RuntimeError("npm modified the bundled lockfile; no engine was published.")
        report = check(stage)
        if os.path.lexists(prefix):
            raise RuntimeError("Engine destination appeared during installation; no files were replaced.")
        stage.rename(prefix)
    return {**report, "install": "installed", "locked_packages": packages}


def _bundled_notices(prefix: Path) -> list[dict]:
    _, raw, _ = locked_runtime(prefix)
    rows = []
    for path, entry in sorted(json.loads(raw)["packages"].items()):
        if not path:
            continue
        package = prefix / path
        if not package.exists() and entry.get("optional"):
            continue
        metadata = json.loads((package / "package.json").read_bytes())
        names = sorted(item.name for item in package.iterdir() if item.is_file()
                       and item.name.upper().startswith(("LICENSE", "LICENCE", "NOTICE", "COPYING")))
        license_name = metadata.get("license")
        if (metadata.get("version") != entry["version"] or not isinstance(license_name, str) or not license_name
                or not any(name.upper().startswith(("LICENSE", "LICENCE", "COPYING")) for name in names)):
            raise RuntimeError(f"Bundled dependency lacks expected version or license files: {path}")
        rows.append({"path": path, "name": metadata["name"], "version": entry["version"], "license": license_name,
                     "source": entry["resolved"], "notice_files": names})
    return rows


def bundle_windows(app_dir: Path, *, offline: bool = False) -> dict:
    node = shutil.which("node")
    if not node or subprocess.run([node, "-p", "process.platform + ':' + process.arch"], capture_output=True,
                                  check=True, text=True, timeout=10).stdout.strip() != "win32:x64":
        raise RuntimeError("Bundling the Windows engine requires Windows x64 Node.js and npm on the build host.")
    engines = app_dir / "engines"
    engines.mkdir(parents=True, exist_ok=True)
    destination = engines / f"graphflow-{graphflow.VERSION}"
    if os.path.lexists(destination):
        raise RuntimeError("Bundled engine destination already exists.")
    with tempfile.TemporaryDirectory(prefix=".starwish-runtime-", dir=engines) as directory:
        stage = Path(directory) / destination.name
        report = install(stage, offline=offline)
        notices = _bundled_notices(stage)
        (stage / "THIRD-PARTY.json").write_text(json.dumps(notices, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        if os.path.lexists(destination):
            raise RuntimeError("Bundled engine destination appeared during build.")
        stage.rename(destination)
    return {**report, "installed_packages": len(notices)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install", "bundle-windows"))
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--offline", action="store_true", help="Install only from the local npm cache.")
    args = parser.parse_args()
    if args.prefix is None:
        if args.action == "bundle-windows":
            parser.error("Provide --prefix to select the application staging directory.")
        args.prefix = graphflow.engine_prefix()
    if not args.prefix.is_absolute() or args.prefix.name == "" or args.prefix.is_symlink():
        parser.error("Use a dedicated absolute engine directory, not a symlink or volume root.")
    if args.offline and args.action == "check":
        parser.error("--offline is only needed for install or bundle-windows.")
    try:
        prefix = args.prefix.resolve()
        if prefix.is_relative_to(graphflow.config.SOUL_DIR.resolve()):
            raise RuntimeError("The engine must be installed outside the memory directory.")
        if args.action == "bundle-windows":
            if not prefix.is_dir():
                raise RuntimeError("For bundle-windows, --prefix must be an existing application staging directory.")
            result = bundle_windows(prefix, offline=args.offline)
        else:
            result = check(prefix) if args.action == "check" else install(prefix, offline=args.offline)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"StarWish engine check failed: {exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
