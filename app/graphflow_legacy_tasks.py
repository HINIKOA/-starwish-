"""Explicitly adopt a selected legacy pending episode into the project ledger."""
import hashlib
import json
import os
import re

from . import graphflow, graphflow_edits, graphflow_legacy_journal, graphflow_tasks, graphflow_workflows
from .graphflow import GraphFlowError


ENV = "STARWISH_GRAPHFLOW_LEGACY_TASK_IMPORT"
_PAGE = 20
_CLOSED_EVENTS = {"outcome", "episode-outcome", "resolved-episode"}


def enabled() -> bool:
    return os.environ.get(ENV) == "1" and graphflow_tasks.enabled()


def _identifier(row):
    for key in ("episodeId", "id", "turnId"):
        value = row.get(key)
        if isinstance(value, str) and graphflow_legacy_journal.valid_topic(value):
            return value
    return ""


def _pending(workspace):
    status, raw = graphflow_legacy_journal._read(workspace)
    if status == "missing":
        return {}
    if status != "ready":
        raise GraphFlowError("旧任务日志超限或不可读取。", 413 if status == "too_large" else 503)
    pending, excluded = {}, set()
    for row in graphflow_legacy_journal._events(raw):
        if not row:
            continue
        ident = _identifier(row)
        if not ident:
            continue
        kind = row.get("kind")
        if kind == "pending-episode":
            if ident in pending:
                excluded.add(ident)
            pending[ident] = row
        elif isinstance(kind, str) and kind in _CLOSED_EVENTS:
            excluded.add(ident)
    return {ident: row for ident, row in pending.items() if ident not in excluded}


def transfer(action: str = "list", legacy_id: str = "", target: str = "", confirm_sha256: str = "",
             rootDir: str = "", after: str = "") -> dict:
    if not enabled():
        raise GraphFlowError("未启用旧任务迁移和项目任务台账。", 503)
    workspace = graphflow.checked_workspace(rootDir)
    if (any(not isinstance(value, str) for value in (action, legacy_id, target, confirm_sha256, after))
            or action not in ("list", "preview", "import")):
        raise GraphFlowError("旧任务操作必须是 list、preview 或 import。")
    if action == "list":
        if legacy_id or target or confirm_sha256 or (after and not graphflow_legacy_journal.valid_topic(after)):
            raise GraphFlowError("列表只接受有效的分页游标。")
        ids = sorted(ident for ident in _pending(workspace) if ident > after)
        page = ids[:_PAGE]
        return {"state": "list", "legacy_ids": page, "next_after": page[-1] if len(ids) > _PAGE else None,
                "ledger_written": False}
    if (after or not graphflow_legacy_journal.valid_topic(legacy_id)
            or (action == "preview" and confirm_sha256)
            or (action == "import" and not re.fullmatch(r"[0-9a-f]{64}", confirm_sha256))):
        raise GraphFlowError("请选择一个有效旧任务编号，迁移时须提交预览确认值。")
    row = _pending(workspace).get(legacy_id)
    if row is None:
        raise GraphFlowError("当前工作区没有唯一且未回填的旧待办。", 404)
    _, normalized, path, symbol = graphflow.validate_read(target=target, impact_only=True)
    source = graphflow_edits._read_tracked(workspace, path)
    start, end = graphflow.locate_edit_spans(workspace, path, [symbol], source)[symbol]
    plan = graphflow_workflows.preview("symbol-review", target=normalized)
    try:
        bound = [legacy_id, row, plan["plan_sha256"], hashlib.sha256(source).hexdigest()]
        receipt = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
    except (ValueError, RecursionError) as exc:
        raise GraphFlowError("此旧待办内容不可用于迁移。", 422) from exc
    if action == "preview":
        return {"state": "preview", "legacy_id": legacy_id, "plan": plan, "confirm_sha256": receipt,
                "anchor": {"file": path, "symbol": symbol, "start_line": start, "end_line": end},
                "ledger_written": False}
    if receipt != confirm_sha256:
        raise GraphFlowError("旧待办或计划已变化，请重新预览。", 409)
    key = graphflow_tasks.LEGACY_IMPORT_PREFIX + hashlib.sha256(legacy_id.encode()).hexdigest()
    task = graphflow_tasks.create(key, plan["name"], plan["plan_sha256"], normalized,
                                  source="legacy_import", initial_summary=f"GraphFlow 旧待办：{legacy_id}")
    return {"state": "imported", "legacy_id": legacy_id, "task": task, "ledger_written": not task["duplicate"]}
