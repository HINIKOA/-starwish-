"""Local, explicit grants for discovered read-only GraphFlow workspaces."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from . import config, graphflow
from .graphflow import GraphFlowError

MAX_WORKSPACES = 64
EXCLUDED, PENDING, MANUAL, AUTOMATIC = -1, 0, 1, 2
GRANT_NAMES = {EXCLUDED: "excluded", PENDING: "pending", MANUAL: "manual", AUTOMATIC: "automatic"}


class WorkspacePending(GraphFlowError):
    def __init__(self, workspace):
        super().__init__(f"已发现工作区「{workspace.name}」。请在星愿 GraphFlow 面板授权后重试；无需重连 MCP。", 403)
        self.workspace = workspace


def _validate(raw: str) -> Path:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 1024 or any(ord(c) < 32 for c in raw):
        raise GraphFlowError("请提供本机 Git 仓库的绝对根目录。")
    path = Path(raw)
    if not path.is_absolute() or (os.name == "nt" and path.drive.startswith("\\\\")):
        raise GraphFlowError("仅支持本机 Git 仓库的绝对根目录。")
    try:
        root = path.resolve(strict=True)
        home = Path.home().resolve()
        private_dirs = [home / "AppData", config.SOUL_DIR]
        for key in ("APPDATA", "LOCALAPPDATA"):
            if os.environ.get(key):
                private_dirs.append(Path(os.environ[key]).resolve())
        git_marker = root / ".git"
        if (root != path or root == Path(root.anchor) or root == home or not root.is_dir()
                or any(root.is_relative_to(item) for item in private_dirs)
                or git_marker.is_symlink() or not (git_marker.is_dir() or git_marker.is_file())):
            raise ValueError
        if getattr(git_marker.lstat(), "st_file_attributes", 0) & graphflow._REPARSE:
            raise ValueError
    except (OSError, RuntimeError, ValueError) as exc:
        raise GraphFlowError("请选择独立的本机 Git 根目录；不接受链接、用户目录或灵魂包。") from exc
    return root


def _id(root: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.normcase(str(root)))).hexdigest()


@contextmanager
def _db(write: bool = False):
    path = config.soul_path("graphflow-workspaces.sqlite3")
    if not write and not path.is_file():
        yield None
        return
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(path, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0
                )""")
                db.execute("CREATE TABLE IF NOT EXISTS workspace_policy (id INTEGER PRIMARY KEY, automatic INTEGER NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS workspace_grant_modes (id TEXT PRIMARY KEY, mode INTEGER NOT NULL)")
            yield db
    except sqlite3.Error as exc:
        raise GraphFlowError("暂时无法访问工作区授权记录，请重试。", 503) from exc


def _has_table(db, name: str) -> bool:
    return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (name,)).fetchone() is not None


def _automatic_enabled(db) -> bool:
    if not _has_table(db, "workspace_policy"):
        return False
    entry = db.execute("SELECT automatic FROM workspace_policy WHERE id=1").fetchone()
    return bool(entry and entry[0])


def view() -> dict:
    with _db() as db:
        rows = db.execute(_workspace_query(db) + " ORDER BY grant_mode DESC, path").fetchall() if db else []
        enabled = _automatic_enabled(db)
    return {"auto_authorize": enabled, "folder_picker": folder_picker_available(), "workspaces": [
        {"id": row["id"], "name": Path(row["path"]).name, "path": row["path"],
         "approved": row["grant_mode"] in {MANUAL, AUTOMATIC},
         "authorization": GRANT_NAMES.get(row["grant_mode"], "pending")} for row in rows
    ]}


def _workspace_query(db) -> str:
    if not _has_table(db, "workspace_grant_modes"):
        return "SELECT id, path, approved AS grant_mode FROM workspaces w"
    return ("SELECT w.id, w.path, COALESCE(m.mode, w.approved) AS grant_mode FROM workspaces w "
            "LEFT JOIN workspace_grant_modes m ON m.id=w.id")


def set_automatic(enabled: bool) -> None:
    with _db(True) as db:
        if enabled == _automatic_enabled(db):
            return
        if enabled:
            for row in db.execute(_workspace_query(db) + " WHERE COALESCE(m.mode, w.approved)=?", (PENDING,)).fetchall():
                try:
                    _verify_git(_validate(row["path"]))
                except GraphFlowError:
                    continue
                db.execute("INSERT INTO workspace_grant_modes(id, mode) VALUES (?, ?)", (row["id"], AUTOMATIC))
        else:
            db.execute("DELETE FROM workspace_grant_modes WHERE mode=?", (AUTOMATIC,))
        db.execute("INSERT OR REPLACE INTO workspace_policy(id, automatic) VALUES (1, ?)", (int(enabled),))


def discover(raw: str) -> Path:
    workspace = _validate(raw)
    workspace_id = _id(workspace)
    with _db(True) as db:
        if not db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
            if db.execute("SELECT count(*) FROM workspaces").fetchone()[0] >= MAX_WORKSPACES:
                raise GraphFlowError("待授权或已授权的工作区已达上限，请先清理不使用的项目。", 409)
            db.execute("INSERT INTO workspaces(id, path) VALUES(?, ?)", (workspace_id, str(workspace)))
            if _automatic_enabled(db):
                _verify_git(workspace)
                db.execute("INSERT INTO workspace_grant_modes(id, mode) VALUES (?, ?)", (workspace_id, AUTOMATIC))
    return workspace


def folder_picker_available() -> bool:
    return os.name == "nt" and _powershell().is_file()


def _powershell() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def pick_folder() -> None:
    if not folder_picker_available():
        raise GraphFlowError("此环境不支持系统文件夹选择，请使用下方的手动添加。", 503)
    script = """$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select a Git repository for StarWish GraphFlow'
$dialog.ShowNewFolderButton = $false
try {
  if ($dialog.ShowDialog() -eq 'OK') { [Console]::Write($dialog.SelectedPath) }
} finally { $dialog.Dispose() }
"""
    try:
        result = subprocess.run([str(_powershell()), "-NoProfile", "-STA", "-Command", script],
                                stdin=subprocess.DEVNULL, capture_output=True, check=True,
                                encoding="utf-8", timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise GraphFlowError("无法打开系统文件夹选择，请使用手动添加。", 503) from exc
    if result.stdout:
        discover(result.stdout)


def _lookup(workspace_id: str) -> tuple[Path, bool] | None:
    if not re.fullmatch(r"[0-9a-f]{64}", workspace_id):
        raise GraphFlowError("工作区标识无效。")
    with _db() as db:
        row = db.execute(_workspace_query(db) + " WHERE w.id=?", (workspace_id,)).fetchone() if db else None
    return (Path(row["path"]), row["grant_mode"] in {MANUAL, AUTOMATIC}) if row else None


def decide(ids: list[str], approve: bool) -> None:
    ids = list(dict.fromkeys(ids))
    if not ids or len(ids) > MAX_WORKSPACES or not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
        raise GraphFlowError("请选择有效的工作区。")
    with _db(True) as db:
        for workspace_id in ids:
            row = db.execute("SELECT path FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            if not row:
                raise GraphFlowError("工作区列表已变化，请刷新后重试。", 409)
            if approve:
                _verify_git(_validate(row["path"]))
        db.executemany("UPDATE workspaces SET approved=? WHERE id=?", [(int(approve), value) for value in ids])
        if approve:
            db.executemany("DELETE FROM workspace_grant_modes WHERE id=?", [(value,) for value in ids])
        else:
            db.executemany("INSERT OR REPLACE INTO workspace_grant_modes(id, mode) VALUES (?, ?)",
                           [(value, EXCLUDED) for value in ids])


def _verify_git(root: Path) -> None:
    git = shutil.which("git")
    if not git:
        raise GraphFlowError("请先安装 Git，再授权工作区。", 503)
    try:
        result = subprocess.run([git, "rev-parse", "--show-toplevel"], cwd=root, capture_output=True,
                                check=True, timeout=4, encoding="utf-8")
        if Path(result.stdout.strip()).resolve() != root:
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        raise GraphFlowError("这个目录不是可访问的 Git 仓库根目录。") from exc


def panel_scope(workspace_id: str):
    if not workspace_id:
        return nullcontext()
    match = _lookup(workspace_id)
    if not match or not match[1]:
        raise GraphFlowError("该工作区尚未授权或已撤销授权。", 403)
    return graphflow.use_read_workspace(_validate(str(match[0])))


def mcp_scope(root_dir: str):
    if not root_dir:
        return nullcontext()
    workspace = _validate(root_dir)
    try:
        if workspace == graphflow.checked_workspace():
            return nullcontext()
    except GraphFlowError:
        pass
    match = _lookup(_id(workspace))
    if not match:
        discover(str(workspace))
        match = _lookup(_id(workspace))
    if match and match[1]:
        return graphflow.use_read_workspace(workspace)
    raise WorkspacePending(workspace)
