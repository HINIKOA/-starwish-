"""Read-only diagnostics for the configured StarWish project tools."""
import os
import stat
import time

from . import config, graphflow, graphflow_edit_queue, graphflow_legacy_journal, graphflow_legacy_observations
from . import graphflow_tasks, graphflow_tests


_PROJECT_MARKERS = {
    "codex": {".codex/config.toml": "config", ".codex/hooks.json": "hooks", "AGENTS.md": "rules",
              ".agents/skills/graphflow/SKILL.md": "legacy_skill"},
    "claude-code": {".mcp.json": "mcp", ".claude/settings.json": "config", "CLAUDE.md": "rules",
                    ".claude/skills/graphflow/SKILL.md": "legacy_skill"},
    "cursor": {".cursor/mcp.json": "mcp", ".cursor/hooks.json": "hooks", ".cursor/rules/graphflow.mdc": "legacy_rule"},
    "gemini": {".gemini/settings.json": "config", "GEMINI.md": "rules"},
    "vscode": {".vscode/mcp.json": "mcp"},
    "opencode": {"opencode.json": "config", "opencode.jsonc": "config"},
}
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def _marker_exists(workspace, relative):
    path = workspace / relative
    if path.is_relative_to(config.SOUL_DIR):
        return False
    try:
        if path.resolve(strict=True) != path:
            return False
        current = workspace
        parts = relative.split("/")
        for part in parts[:-1]:
            current /= part
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
                return False
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not (getattr(info, "st_file_attributes", 0) & _REPARSE)
    except (OSError, RuntimeError):
        return False


def project_markers(workspace):
    result = {}
    for host, markers in _PROJECT_MARKERS.items():
        found = sorted({label for path, label in markers.items() if _marker_exists(workspace, path)})
        if found:
            result[host] = found
    return result


def _ledger(workspace):
    if not graphflow_tasks.enabled():
        return {"status": "disabled"}
    try:
        with graphflow_tasks.read_ledger(workspace) as db:
            if db is None:
                return {"status": "not_initialized", "pending": 0, "blocked": 0, "older_than_24h": 0}
            row = db.execute("""SELECT count(CASE WHEN state='pending' THEN 1 END),
                                count(CASE WHEN state='blocked' THEN 1 END),
                                count(CASE WHEN state IN ('pending','blocked') AND created<=? THEN 1 END)
                                FROM tasks""", (time.time() - graphflow_tasks.OLD_SECONDS,)).fetchone()
        return {"status": "ready", "pending": row[0], "blocked": row[1], "older_than_24h": row[2]}
    except graphflow.GraphFlowError:
        return {"status": "unavailable"}


def _index_status(workspace):
    index = {"status": "busy", "scope": "process"}
    if graphflow._gate.acquire(blocking=False):
        try:
            snapshot = graphflow._active_snapshot
            index["status"] = "cold"
            if snapshot is not None and snapshot.workspace == workspace:
                index.update(status="cached", files=len(snapshot.files), warmed=len(snapshot.warmed))
        finally:
            graphflow._gate.release()
    return index


def inspect(rootDir: str = "", nodeLimit: int | None = None, edgeLimit: int | None = None, configPath: str = "") -> dict:
    if not isinstance(rootDir, str) or not isinstance(configPath, str) or configPath:
        raise graphflow.GraphFlowError("诊断只检查已配置工作区，不接受配置文件路径。")
    if any(value is not None and (type(value) is not int or value < 0) for value in (nodeLimit, edgeLimit)):
        raise graphflow.GraphFlowError("图谱数量限制须为非负整数。")
    try:
        workspace = graphflow.checked_workspace(rootDir)
    except (graphflow.GraphFlowError, OSError, RuntimeError, ValueError):
        if rootDir:
            raise
        return {"workspace": "not_configured", "engine": "not_checked", "index": {"status": "not_checked"},
                "tasks": {"status": "not_checked"}, "legacy_journal": {"status": "not_checked"}, "read_only": True}

    engine = graphflow.status()
    return {"workspace": "authorized", "engine": "ready" if engine["available"] else "unavailable",
            "provider": engine["provider"], "reason": engine["reason"], "index": _index_status(workspace),
            "tasks": _ledger(workspace),
            "features": {"assistant_opt_in": os.environ.get("STARWISH_GRAPHFLOW_ASSISTANT") == "1",
                         "edit_queue_configured": graphflow_edit_queue.enabled(),
                         "tests_configured": graphflow_tests.enabled(), "agent_logs_opt_in": graphflow_tests.agent_logs_enabled(),
                         "legacy_observations_opt_in": os.environ.get(graphflow_legacy_observations.ENV) == "1"},
            "legacy_journal": graphflow_legacy_journal.inspect(workspace),
            "graph_details": "not_exported", "read_only": True}


def exec_status(workspace, *, doctor=False):
    if doctor:
        report = inspect(str(workspace))
    else:
        engine = graphflow.status()
        report = {"engine": "ready" if engine["available"] else "unavailable", "provider": engine["provider"],
                  "reason": engine["reason"], "index": _index_status(workspace)}
    state = "PASS" if report["engine"] == "ready" else "UNAVAILABLE"
    index = report["index"]
    lines = [f"{state} [graphflow-status] {report['provider']} | {workspace.name}",
             f"Index: {index['status']} (current process)"]
    if index["status"] == "cached":
        lines[-1] += f"; {index['files']} indexed file(s), {index['warmed']} warmed"
    if report["reason"]:
        lines.append(report["reason"])
    if doctor:
        tasks = report["tasks"]
        lines.append("Tasks: " + tasks["status"])
        if "pending" in tasks:
            lines[-1] += f"; pending={tasks['pending']}, blocked={tasks['blocked']}, older_than_24h={tasks['older_than_24h']}"
        lines.append("Features: " + ", ".join(f"{name}={'on' if enabled else 'off'}"
                                              for name, enabled in report["features"].items()))
        lines.append("Legacy journal: " + report["legacy_journal"]["status"])
        markers = project_markers(workspace)
        lines.append("Project markers (file presence only; effective registration not checked):")
        lines.extend(f"  {host}: {', '.join(labels)}" for host, labels in markers.items())
        if not markers:
            lines.append("  (none in the authorized Git root)")
    return "\n".join(lines)
