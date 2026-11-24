"""Read-only scope checks shared by the Codex hooks and their consumer."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path


def regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
        return (stat.S_ISREG(info.st_mode) and path.resolve(strict=True) == path
                and info.st_nlink == 1 and not (getattr(info, "st_file_attributes", 0) & 0x400))
    except (OSError, RuntimeError):
        return False


def manual_grant(soul: Path, workspace: Path) -> bool:
    path = soul / "graphflow-workspaces.sqlite3"
    if not regular_file(path):
        return False
    key = hashlib.sha256(os.fsencode(os.path.normcase(str(workspace)))).hexdigest()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
        modern = db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_grant_modes' AND type='table'").fetchone()
        query = ("SELECT w.path, COALESCE(m.mode,w.approved) FROM workspaces w "
                 "LEFT JOIN workspace_grant_modes m ON m.id=w.id WHERE w.id=?"
                 if modern else "SELECT path, approved FROM workspaces WHERE id=?")
        row = db.execute(query, (key,)).fetchone()
    return bool(row and Path(row[0]) == workspace and row[1] == 1)


def git_root(value) -> Path | None:
    try:
        cwd = Path(value)
        if not cwd.is_absolute() or (os.name == "nt" and cwd.drive.startswith("\\\\")):
            return None
        cwd = cwd.resolve(strict=True)
        for path in (cwd, *cwd.parents):
            marker = path / ".git"
            if marker.exists() or marker.is_symlink():
                if (marker.is_dir() or marker.is_file()) and marker.resolve(strict=True) == marker and not (
                    getattr(marker.lstat(), "st_file_attributes", 0) & 0x400
                ):
                    return path
                return None
        return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def in_workspace(workspace: Path, value) -> bool:
    return git_root(value) == workspace


def local_soul(soul: Path) -> bool:
    return (soul.is_absolute() and soul.resolve(strict=True) == soul
            and not (os.name == "nt" and soul.drive.startswith("\\\\")))
