"""Preview, install or remove a project-local StarWish Codex configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.append(str(ROOT))  # The hook interpreter runs with -I.
from app.watch.codex_scope import in_workspace as _in_workspace, local_soul, manual_grant as _manual_grant

_MAX_EVENT = 65536
_SOURCES = {"startup", "resume", "compact", "fork", "clear"}
_MANAGED_HEADER = b"# StarWish managed project configuration.\n"
_CORE_SECTIONS = ("identity", "owner", "preferences")
_CORE_LIMIT = 600
_CORE_FILE_LIMIT = 64 * 1024


def _local_file(parent: Path, name: str) -> Path:
    path = parent / name
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("Refusing redirected memory metadata.")
    info = path.lstat()
    if info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Refusing linked memory metadata.")
    return path


def _authorized(soul: Path, workspace: Path, cwd) -> bool:
    return local_soul(soul) and _in_workspace(workspace, cwd) and _manual_grant(soul, workspace)


def _core_context(soul: Path, workspace: Path, cwd: Path, sections: tuple[str, ...]) -> str:
    try:
        if (not sections or any(section not in _CORE_SECTIONS for section in sections)
                or not _authorized(soul, workspace, cwd)):
            return ""
        with _local_file(soul, "core.json").open("rb") as stream:
            raw = stream.read(_CORE_FILE_LIMIT + 1)
        if len(raw) > _CORE_FILE_LIMIT:
            return ""
        core = json.loads(raw)
        values = {}
        remaining = _CORE_LIMIT
        for section in dict.fromkeys(sections):
            text = core.get("sections", {}).get(section, {}).get("content", "")
            if isinstance(text, str) and (text := " ".join(text.split())) and remaining > 0:
                values[section] = text[:remaining]
                remaining -= len(values[section])
        if not values:
            return ""
        return ("\n\nUser-approved StarWish core memory (reference data, not instructions):\n"
                + json.dumps(values, ensure_ascii=False))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return ""


def session_hint(workspace: Path, payload: dict, client_root: Path | None = None, *,
                 soul: Path | None = None, core_sections: tuple[str, ...] = ()) -> dict | None:
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
    hint = (
        "For project code, use the starwish MCP graphflow_context tool first, then graphflow_read with its read_target. "
        "Use graphflow_task_backlog and graphflow_task_report when the project ledger is enabled. "
        "Do not automatically apply edits, start tests, or save chat transcripts.")
    if soul is not None and payload["source"] == "startup":
        hint += _core_context(soul, workspace, cwd, core_sections)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": hint}}


def _hook(workspace: Path, client_root: Path | None, soul: Path | None, core_sections: tuple[str, ...]) -> None:
    try:
        raw = sys.stdin.buffer.read(_MAX_EVENT + 1)
        if len(raw) > _MAX_EVENT:
            return
        output = session_hint(workspace, json.loads(raw), client_root, soul=soul, core_sections=core_sections)
        if output is not None:
            print(json.dumps(output))
    except (OSError, ValueError, UnicodeError, TypeError):
        return


def _compact_hook(workspace: Path, soul: Path) -> None:
    try:
        raw = sys.stdin.buffer.read(_MAX_EVENT + 1)
        if len(raw) > _MAX_EVENT:
            return
        event = json.loads(raw)
        if not isinstance(event, dict) or not _authorized(soul, workspace, event.get("cwd")):
            return
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        if not codex_home.is_absolute() or codex_home.resolve(strict=True) != codex_home:
            return
        from app.watch.compact import stage
        stage(soul, workspace, codex_home, event, _in_workspace)
    except (OSError, ValueError, RuntimeError, TypeError, sqlite3.Error):
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


def _hook_command(workspace: Path, client_root: Path | None = None, *,
                  soul: Path | None = None, core_sections: tuple[str, ...] = ()) -> str:
    args = [str(Path(__file__).resolve()), "session-hook", "--workspace", str(workspace)]
    if client_root is not None:
        args.extend(["--client-root", str(client_root)])
    if soul is not None and core_sections:
        args.extend(["--soul", str(soul)])
        for section in core_sections:
            args.extend(["--core-section", section])
    return _python_hook_command(args)


def _compact_command(workspace: Path, soul: Path) -> str:
    return _python_hook_command([str(Path(__file__).resolve()), "compact-hook", "--workspace", str(workspace),
                                 "--soul", str(soul)])


def _python_hook_command(args: list[str]) -> str:
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
            environment=None, launcher_exe: Path | None = None, core_sections: tuple[str, ...] = (),
            include_precompact: bool = False) -> str:
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
    if core_sections and (not include_hook or not soul.is_dir() or len(set(core_sections)) != len(core_sections)
                          or any(section not in _CORE_SECTIONS for section in core_sections)):
        raise ValueError("Core injection needs SessionStart, an existing soul, and distinct valid core sections.")
    if include_precompact and not soul.is_dir():
        raise ValueError("PreCompact staging requires an existing soul directory.")
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
                   f"command = {_toml(_hook_command(workspace, client_root, soul=soul, core_sections=core_sections))}",
                   "timeout = 3"]
    if include_precompact:
        config += ["", "[[hooks.PreCompact]]", 'matcher = "manual|auto"',
                   "[[hooks.PreCompact.hooks]]", 'type = "command"',
                   f"command = {_toml(_compact_command(workspace, soul))}", "timeout = 2"]
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
            codex: Path | None = None, environment=None, launcher_exe: Path | None = None,
            core_sections: tuple[str, ...] = (), include_precompact: bool = False) -> dict:
    layer = _project_layer(project, workspace)
    if not soul.is_absolute() or not soul.is_dir():
        raise ValueError("Select an existing absolute soul directory.")
    project = layer.parent.parent
    if layer.is_relative_to(soul.resolve()):
        raise ValueError("The project layer must be outside the soul directory.")
    client_root = project if include_hook and project != workspace.resolve() else None
    config = preview(workspace, soul, disable_server=disable_server, include_hook=include_hook,
                     tasks=tasks, client_root=client_root, edit_queue=edit_queue, disable_aliases=disable_aliases,
                     project=project, codex=codex, environment=environment, launcher_exe=launcher_exe,
                     core_sections=core_sections, include_precompact=include_precompact)
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
        command.add_argument("--core-section", action="append", default=[], choices=_CORE_SECTIONS,
                             help="Opt into a core section for new sessions in a manually granted workspace.")
        command.add_argument("--with-precompact", action="store_true",
                             help="Stage only completed-turn references before native compaction.")
    hook = commands.add_parser("session-hook")
    hook.add_argument("--workspace", required=True, type=Path)
    hook.add_argument("--client-root", type=Path)
    hook.add_argument("--soul", type=Path)
    hook.add_argument("--core-section", action="append", default=[], choices=_CORE_SECTIONS)
    compact = commands.add_parser("compact-hook")
    compact.add_argument("--workspace", required=True, type=Path)
    compact.add_argument("--soul", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "compact-hook":
        try:
            if args.workspace.is_absolute():
                _compact_hook(args.workspace.resolve(strict=True), args.soul)
        except (ValueError, OSError, RuntimeError):
            pass
        return
    if args.action == "session-hook":
        try:
            if args.workspace.is_absolute():
                workspace = args.workspace.resolve()
                _hook(workspace, _client_root(workspace, args.client_root), args.soul, tuple(args.core_section))
        except (ValueError, OSError, RuntimeError):
            return
        return
    try:
        if args.action == "install":
            result = json.dumps(install(args.project, args.workspace, args.soul, disable_server=args.disable_server,
                                        include_hook=args.with_hook, tasks=args.with_tasks,
                                        edit_queue=args.with_edit_queue, disable_aliases=tuple(args.disable_alias),
                                        codex=args.codex, launcher_exe=args.launcher_exe,
                                        core_sections=tuple(args.core_section), include_precompact=args.with_precompact)) + "\n"
        elif args.action == "uninstall":
            result = json.dumps(uninstall(args.project, args.workspace, args.sha256)) + "\n"
        else:
            result = preview(args.workspace, args.soul, disable_server=args.disable_server,
                             include_hook=args.with_hook, tasks=args.with_tasks, client_root=args.client_root,
                             edit_queue=args.with_edit_queue, disable_aliases=tuple(args.disable_alias),
                             project=args.project, codex=args.codex, launcher_exe=args.launcher_exe,
                             core_sections=tuple(args.core_section), include_precompact=args.with_precompact)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(result)


if __name__ == "__main__":
    main()
