"""Preview, install or remove a project-local StarWish Codex configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_MAX_EVENT = 65536
_SOURCES = {"startup", "resume", "compact", "fork", "clear"}
_MANAGED_HEADER = b"# StarWish managed project configuration.\n"


def session_hint(workspace: Path, payload: dict, client_root: Path | None = None) -> dict | None:
    if (not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart"
            or not isinstance(payload.get("source"), str) or payload["source"] not in _SOURCES):
        return None
    try:
        cwd = Path(payload["cwd"])
        if not cwd.is_absolute():
            return None
        cwd = cwd.resolve()
        if cwd != client_root and not cwd.is_relative_to(workspace):
            return None
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
        "For project code, use the starwish MCP graphflow_context tool first, then graphflow_read with its read_target. "
        "Use graphflow_task_backlog and graphflow_task_report when the project ledger is enabled. "
        "Do not automatically apply edits, start tests, or save chat transcripts."}}


def _hook(workspace: Path, client_root: Path | None) -> None:
    try:
        raw = sys.stdin.buffer.read(_MAX_EVENT + 1)
        if len(raw) > _MAX_EVENT:
            return
        output = session_hint(workspace, json.loads(raw), client_root)
        if output is not None:
            print(json.dumps(output))
    except (OSError, ValueError, UnicodeError, TypeError):
        return


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _client_root(workspace: Path, root: Path | None) -> Path | None:
    if root is None:
        return None
    if not root.is_absolute():
        raise ValueError("Use an absolute client root.")
    root = root.resolve(strict=True)
    if root not in workspace.parents or root in {Path(root.anchor), Path.home().resolve()}:
        raise ValueError("The client root must be a specific parent directory of the Git workspace.")
    return root


def _hook_command(workspace: Path, client_root: Path | None = None) -> str:
    args = [str(Path(__file__).resolve()), "session-hook", "--workspace", str(workspace)]
    if client_root is not None:
        args.extend(["--client-root", str(client_root)])
    if os.name != "nt":
        return shlex.join([sys._base_executable, "-I", *args])
    python = shutil.which("python.exe")
    if not python or any(any(char in arg for char in '"%$!`\r\n') for arg in args):
        raise ValueError("Windows Hook requires python.exe on PATH and paths without shell expansions.")
    return 'python.exe -I ' + ' '.join(f'"{arg}"' for arg in args)


def _portable_python() -> Path | None:
    runtime = ROOT / "runtime"
    python = runtime / "python.exe"
    if python.is_file() and any(runtime.glob("python*._pth")):
        return python
    return None


def _checked_aliases(workspace, project, codex, aliases, environment):
    if not aliases:
        return ()
    if (len(aliases) > 16 or len(set(aliases)) != len(aliases)
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name.casefold() == "starwish"
                   for name in aliases)):
        raise ValueError("List distinct GraphFlow aliases; StarWish cannot be disabled.")
    if project is None or codex is None or not codex.is_absolute() or not codex.is_file():
        raise ValueError("Disabling aliases requires an explicit project and absolute Codex binary path.")
    project = _project_layer(project, workspace).parent.parent
    if __package__:
        from .graphflow_codex_probe import migration_inventory
    else:
        from graphflow_codex_probe import migration_inventory
    try:
        _, report = migration_inventory(str(codex), project, environment)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError("Could not verify GraphFlow aliases in Codex's effective project configuration.") from exc
    candidates = report["graphflow_candidates"]
    if any(candidates.get(name) != "enabled" for name in aliases):
        raise ValueError("Requested alias is not an enabled GraphFlow candidate in this project; no configuration written.")
    return tuple(sorted(aliases))


def preview(workspace: Path, soul: Path, *, disable_server: str = "", include_hook: bool = False,
            tasks: bool = False, client_root: Path | None = None, edit_queue: bool = False,
            disable_aliases: tuple[str, ...] = (), project: Path | None = None, codex: Path | None = None,
            environment=None, launcher_exe: Path | None = None) -> str:
    if not workspace.is_absolute() or not soul.is_absolute():
        raise ValueError("Use absolute workspace and soul paths.")
    workspace, soul = workspace.resolve(), soul.resolve()
    if (not (workspace / ".git").exists() or workspace in {Path(workspace.anchor), Path.home().resolve()}
            or workspace.is_relative_to(soul)):
        raise ValueError("The workspace must be an authorized Git root outside the soul directory.")
    if disable_server not in {"", "graphflow", "user-graphflow"}:
        raise ValueError("Only an explicitly named legacy GraphFlow server can be disabled.")
    disabled = _checked_aliases(workspace, project, codex, disable_aliases, environment)
    disabled = sorted(set(filter(None, (disable_server, *disabled))))
    client_root = _client_root(workspace, client_root)
    if client_root is not None and not include_hook:
        raise ValueError("The client root is only used with the SessionStart Hook.")
    if launcher_exe is not None and (not launcher_exe.is_absolute() or launcher_exe.suffix.lower() != ".exe"
                                     or not launcher_exe.is_file()):
        raise ValueError("Use the absolute path to an existing StarWish Windows launcher supporting --mcp.")
    portable = None if launcher_exe is not None else _portable_python()
    uv = None if portable or launcher_exe is not None else shutil.which("uv")
    cwd = ROOT
    if launcher_exe is not None:
        launcher_exe = launcher_exe.resolve(strict=True)
        command, args, cwd = str(launcher_exe), ["--mcp"], launcher_exe.parent
    elif portable:
        command, args = str(portable), [str(ROOT / "mcp_server.py")]
    elif uv:
        command = uv
        args = ["run", "--no-project", "--with-requirements", str(ROOT / "requirements.txt"),
                "--python", sys._base_executable, "python", str(ROOT / "mcp_server.py")]
    else:
        command, args = sys.executable, [str(ROOT / "mcp_server.py")]
    config = ["# StarWish project profile. Preview only; merge intentionally.",
              "[mcp_servers.starwish]", f"command = {_toml(command)}",
              "args = [" + ", ".join(map(_toml, args)) + "]",
              f"cwd = {_toml(str(cwd))}", "enabled = true"]
    if launcher_exe is not None:
        config.append("startup_timeout_sec = 90")
    config += ["",
              "[mcp_servers.starwish.env]",
              f"STARWISH_SOUL_DIR = {_toml(str(soul))}",
              f"STARWISH_GRAPHFLOW_WORKSPACE = {_toml(str(workspace))}",
              f'STARWISH_GRAPHFLOW_TASKS = "{int(tasks)}"']
    if edit_queue:
        config.append('STARWISH_GRAPHFLOW_EDIT_QUEUE = "1"')
    for name in disabled:
        config += ["", f"[mcp_servers.{name}]", "enabled = false"]
    if include_hook:
        config += ["", "[[hooks.SessionStart]]", 'matcher = "startup|resume"',
                   "[[hooks.SessionStart.hooks]]", 'type = "command"',
                   f"command = {_toml(_hook_command(workspace, client_root))}", "timeout = 3"]
    return "\n".join(config) + "\n"


def _project_layer(project: Path, workspace: Path) -> Path:
    if not project.is_absolute() or not workspace.is_absolute():
        raise ValueError("Use absolute project and workspace paths.")
    project = project.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    if not (workspace / ".git").exists() or not workspace.is_relative_to(project):
        raise ValueError("The project must contain the authorized Git workspace.")
    if project in {Path(project.anchor), Path.home().resolve()} or Path.home().resolve().is_relative_to(project):
        raise ValueError("Refusing a broad project root.")
    directory = project / ".codex"
    if directory.resolve() != directory or (directory.exists() and not directory.is_dir()):
        raise ValueError("The project .codex directory must not be redirected.")
    if (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve() == directory):
        raise ValueError("The project layer must not be the user configuration directory.")
    return directory / "config.toml"


def install(project: Path, workspace: Path, soul: Path, *, disable_server: str = "", include_hook: bool = False,
            tasks: bool = False, edit_queue: bool = False, disable_aliases: tuple[str, ...] = (),
            codex: Path | None = None, environment=None, launcher_exe: Path | None = None) -> dict:
    layer = _project_layer(project, workspace)
    if not soul.is_absolute() or not soul.is_dir():
        raise ValueError("Select an existing absolute soul directory.")
    project = layer.parent.parent
    if layer.is_relative_to(soul.resolve()):
        raise ValueError("The project layer must be outside the soul directory.")
    client_root = project if include_hook and project != workspace.resolve() else None
    config = preview(workspace, soul, disable_server=disable_server, include_hook=include_hook,
                     tasks=tasks, client_root=client_root, edit_queue=edit_queue, disable_aliases=disable_aliases,
                     project=project, codex=codex, environment=environment, launcher_exe=launcher_exe)
    data = _MANAGED_HEADER + config.split("\n", 1)[1].encode("utf-8")
    layer.parent.mkdir(exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=layer.parent, prefix=".starwish-config-", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish the complete file without replacing even a concurrently created config.
        os.link(tmp, layer)
    except FileExistsError as exc:
        raise ValueError("Project config already exists; left unchanged.") from exc
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return {"status": "installed", "path": str(layer), "sha256": hashlib.sha256(data).hexdigest()}


def uninstall(project: Path, workspace: Path, confirmed_sha256: str) -> dict:
    layer = _project_layer(project, workspace)
    if not re.fullmatch(r"[0-9a-f]{64}", confirmed_sha256):
        raise ValueError("Provide the SHA-256 printed at installation.")
    info = layer.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or layer.resolve(strict=True) != layer:
        raise ValueError("The project config is not a regular unlinked file; left unchanged.")
    with layer.open("rb") as stream:
        data = stream.read(_MAX_EVENT + 1)
    def revision(meta):
        return (meta.st_dev, meta.st_ino, meta.st_mode, meta.st_nlink, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)

    if (len(data) > _MAX_EVENT or not data.startswith(_MANAGED_HEADER)
            or hashlib.sha256(data).hexdigest() != confirmed_sha256 or revision(layer.lstat()) != revision(info)):
        raise ValueError("Project config changed or is not managed by StarWish; left unchanged.")
    layer.unlink()
    return {"status": "removed", "path": str(layer)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    plan = commands.add_parser("preview")
    plan.add_argument("--workspace", required=True, type=Path)
    plan.add_argument("--soul", required=True, type=Path)
    plan.add_argument("--project", type=Path, help="Project used to resolve alias candidates.")
    plan.add_argument("--disable-server", choices=["graphflow", "user-graphflow"], default="")
    plan.add_argument("--with-hook", action="store_true")
    plan.add_argument("--client-root", type=Path)
    plan.add_argument("--with-tasks", action="store_true")
    plan.add_argument("--with-edit-queue", action="store_true", help="Allow previews to be queued, not approved.")
    for name in ("install", "uninstall"):
        project_cmd = commands.add_parser(name)
        project_cmd.add_argument("--project", required=True, type=Path)
        project_cmd.add_argument("--workspace", required=True, type=Path)
        if name == "install":
            project_cmd.add_argument("--soul", required=True, type=Path)
            project_cmd.add_argument("--disable-server", choices=["graphflow", "user-graphflow"], default="")
            project_cmd.add_argument("--with-hook", action="store_true")
            project_cmd.add_argument("--with-tasks", action="store_true")
            project_cmd.add_argument("--with-edit-queue", action="store_true", help="Allow previews to be queued, not approved.")
        else:
            project_cmd.add_argument("--sha256", required=True)
    for command in (plan, commands.choices["install"]):
        command.add_argument("--codex", type=Path, help="Absolute Codex binary for effective-config alias verification.")
        command.add_argument("--disable-alias", action="append", default=[], metavar="NAME",
                             help="Explicit enabled GraphFlow alias to disable; may be repeated.")
        command.add_argument("--launcher-exe", type=Path, help="Use a StarWish Windows launcher with --mcp.")
    hook = commands.add_parser("session-hook")
    hook.add_argument("--workspace", required=True, type=Path)
    hook.add_argument("--client-root", type=Path)
    args = parser.parse_args()
    if args.action == "session-hook":
        try:
            if args.workspace.is_absolute():
                workspace = args.workspace.resolve()
                _hook(workspace, _client_root(workspace, args.client_root))
        except (ValueError, OSError, RuntimeError):
            return
        return
    try:
        if args.action == "install":
            result = json.dumps(install(args.project, args.workspace, args.soul, disable_server=args.disable_server,
                                        include_hook=args.with_hook, tasks=args.with_tasks,
                                        edit_queue=args.with_edit_queue, disable_aliases=tuple(args.disable_alias),
                                        codex=args.codex, launcher_exe=args.launcher_exe)) + "\n"
        elif args.action == "uninstall":
            result = json.dumps(uninstall(args.project, args.workspace, args.sha256)) + "\n"
        else:
            result = preview(args.workspace, args.soul, disable_server=args.disable_server,
                             include_hook=args.with_hook, tasks=args.with_tasks, client_root=args.client_root,
                             edit_queue=args.with_edit_queue, disable_aliases=tuple(args.disable_alias),
                             project=args.project, codex=args.codex, launcher_exe=args.launcher_exe)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(result)


if __name__ == "__main__":
    main()
