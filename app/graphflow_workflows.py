"""Preview and replay deterministic read-only project macros."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from pathlib import Path

from . import config, graphflow, graphflow_edits, graphflow_history, graphflow_tests, graphflow_trajectories
from .graphflow import GraphFlowError

ENV = "STARWISH_GRAPHFLOW_WORKFLOWS"
LEGACY_ENV = "STARWISH_GRAPHFLOW_LEGACY_MACROS"
CODEMOD_ENV = "STARWISH_GRAPHFLOW_LEGACY_CODEMODS"
COMMAND_MAP_ENV = "STARWISH_GRAPHFLOW_LEGACY_COMMAND_MAP"
MACRO_ACTIONS = ("list", "preview", "run", "task-list", "task-run", "mine-git", "mine")
MAX_STEPS = 6
_MAX_LEGACY_BYTES = 512 * 1024
_MAX_LEGACY_MACROS = 50
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_INPUTS = {"$target": "target", "$query": "query"}
_BUILTINS = {
    "symbol-review": {"title": "符号审查", "steps": [
        {"op": "read", "target": "$target"},
        {"op": "impact", "target": "$target"},
        {"op": "test_plan"},
    ]},
    "change-review": {"title": "改动审查", "steps": [{"op": "changes"}, {"op": "test_plan"}]},
    "history-review": {"title": "历史共改线索", "steps": [{"op": "cochange"}]},
}


def _specs(workspace: Path) -> dict:
    try:
        extra = json.loads(os.environ.get(ENV) or "{}")
        if not isinstance(extra, dict) or len(extra) > 12 or extra.keys() & _BUILTINS.keys():
            raise ValueError
        specs = {**_BUILTINS, **extra}
        for name, spec in specs.items():
            if (not _NAME.fullmatch(name) or not isinstance(spec, dict) or set(spec) != {"title", "steps"}
                    or not isinstance(spec["title"], str) or not 1 <= len(spec["title"].strip()) <= 80
                    or not isinstance(spec["steps"], list) or not 1 <= len(spec["steps"]) <= MAX_STEPS):
                raise ValueError
            for step in spec["steps"]:
                if not isinstance(step, dict):
                    raise ValueError
                op = step.get("op")
                keys = set(step)
                if op == "read":
                    if keys - {"op", "target", "query", "skeleton"} or ("query" in keys) == ("target" in keys):
                        raise ValueError
                    graphflow.validate_read(
                        "query" if step.get("query") == "$query" else step.get("query", ""),
                        "file.py#symbol" if step.get("target") == "$target" else step.get("target", ""),
                        step.get("skeleton", False),
                    )
                elif op == "impact" and keys == {"op", "target"}:
                    target = "file.py#symbol" if step["target"] == "$target" else step["target"]
                    graphflow.validate_read(target=target, impact_only=True)
                elif op not in ("changes", "test_plan", "cochange") or keys != {"op"}:
                    raise ValueError
    except (ValueError, TypeError) as exc:
        raise GraphFlowError(f"{ENV} 工作流配置无效；仅允许只读切片、影响分析、改动、历史共改和测试建议。", 503) from exc
    return {**_legacy_specs(workspace), **specs}


def _legacy_rows(workspace: Path) -> list[dict]:
    source = workspace / ".graphflow" / "macros.json"
    try:
        metadata = source.lstat()
        if (source.resolve(strict=True) != source or source.is_relative_to(config.SOUL_DIR)
                or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
            return []
        with source.open("rb") as file:
            raw = file.read(_MAX_LEGACY_BYTES + 1)
        if len(raw) > _MAX_LEGACY_BYTES:
            return []
        payload = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RuntimeError):
        return []
    rows = payload.get("macros") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    ids = Counter(row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str))
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
            and _NAME.fullmatch(row["id"]) and ids[row["id"]] == 1]


def _legacy_specs(workspace: Path) -> dict:
    if os.environ.get(LEGACY_ENV) != "1":
        return {}
    specs = {}
    for row in _legacy_rows(workspace):
        if row.get("kind") != "context-bundle":
            continue
        targets = row.get("steps")
        if not isinstance(targets, list) or not 1 <= len(targets) <= graphflow.MAX_READ_TARGETS:
            continue
        try:
            normalized = [graphflow.validate_read(target=target)[1] for target in targets]
        except GraphFlowError:
            continue
        name = row["id"]
        specs[name] = {"title": f"GraphFlow · {name}", "steps": [{"op": "read_batch", "targets": normalized}]}
        if len(specs) == _MAX_LEGACY_MACROS:
            break
    return specs


def _codemods(workspace: Path) -> dict:
    if os.environ.get(CODEMOD_ENV) != "1":
        return {}
    reserved = _specs(workspace)
    result = {}
    for row in _legacy_rows(workspace):
        name, steps = row["id"], row.get("steps")
        if (row.get("kind") != "edit-codemod" or name in reserved or graphflow_trajectories.ID.fullmatch(name)
                or not isinstance(steps, list) or len(steps) != 2 or not all(isinstance(step, str) for step in steps)):
            continue
        old, new = steps
        try:
            want = graphflow_edits._block(old)
            if not any(want) or len(want) > 200 or want == graphflow_edits._block(new):
                continue
            old.encode("utf-8")
            new.encode("utf-8")
        except (ValueError, UnicodeError):
            continue
        result[name] = (old, new)
        if len(result) == _MAX_LEGACY_MACROS:
            break
    return result


def _codemod(workspace: Path, name: str, spec: tuple[str, str], paths: list[str], confirmed: str, budget: int) -> dict:
    if not 1 <= len(paths) <= graphflow_edits.MAX_EDIT_FILES:
        raise GraphFlowError("批量替换宏必须明确指定 1 至 4 个文件。")
    paths = sorted(graphflow_edits._compat_path(path) for path in paths)
    if len(set(path.casefold() for path in paths)) != len(paths):
        raise GraphFlowError("批量替换宏不接受重复文件。")
    old, new = spec
    parts = graphflow_edits._prepare_parts(workspace, [
        {"file": path, "oldCode": old, "newCode": new} for path in paths])
    report = graphflow_edits._describe(parts)
    identity = [str(workspace), "edit-codemod", name, old, new, paths, report["base_sha256"], report["result_sha256"]]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    if confirmed:
        if confirmed != digest:
            raise GraphFlowError("宏定义或文件内容已变化，请重新预览。", 409)
        from . import graphflow_edit_queue

        if not graphflow_edit_queue.enabled():
            raise GraphFlowError("本机编辑审批队列未启用。", 503)
        report = graphflow_edits._submit(workspace, parts)
    else:
        report.update(state="preview", queued=False)
    report.update(macro_id=name, kind="edit-codemod", plan_sha256=digest, test_executed=False)
    if len(json.dumps(report, ensure_ascii=False)) > budget:
        report.pop("diff")
        report["notice"] = "完整差异超出输出预算；请缩小范围，或提交后在本机面板查看完整差异。"
    return report


def _command_macros(workspace: Path) -> dict:
    raw = os.environ.get(COMMAND_MAP_ENV)
    if not raw:
        return {}
    try:
        mapping = json.loads(raw)
        if not isinstance(mapping, dict) or len(mapping) > 20:
            raise ValueError
        for name, profiles in mapping.items():
            if (not _NAME.fullmatch(name) or not isinstance(profiles, list) or not 1 <= len(profiles) <= MAX_STEPS
                    or any(not isinstance(profile, str) or not _NAME.fullmatch(profile) for profile in profiles)):
                raise ValueError
    except (ValueError, RecursionError) as exc:
        raise GraphFlowError(f"{COMMAND_MAP_ENV} 须为宏 ID 到预设测试名称列表的映射。", 503) from exc
    reserved = _specs(workspace)
    result = {}
    for row in _legacy_rows(workspace):
        name, kind, steps = row["id"], row.get("kind"), row.get("steps")
        if (name not in mapping or name in reserved or graphflow_trajectories.ID.fullmatch(name)
                or kind not in ("command-template", "command-pipeline") or not isinstance(steps, list)
                or len(steps) != len(mapping[name]) or (kind == "command-template" and len(steps) != 1)
                or any(not isinstance(step, str) or not step.strip() for step in steps)):
            continue
        fingerprint = hashlib.sha256(json.dumps([kind, steps], ensure_ascii=True).encode()).hexdigest()
        result[name] = (kind, mapping[name], fingerprint)
    return result


def _command_handoff(workspace: Path, name: str, spec: tuple, confirmed: str) -> dict:
    kind, profiles, fingerprint = spec
    plan = graphflow_tests.plan(workspace)
    choices = {row["profile"]: row["approval_sha256"] for row in plan["profiles"]}
    approvals = [(profile, choices.get(profile)) for profile in profiles]
    digest = hashlib.sha256(json.dumps([str(workspace), name, fingerprint, approvals], ensure_ascii=True).encode()).hexdigest()
    if confirmed and confirmed != digest:
        raise GraphFlowError("命令宏或测试方案已变化，请重新预览。", 409)
    if confirmed and any(approval is None for _, approval in approvals):
        raise GraphFlowError("至少一项预设测试未配置或不匹配当前改动。", 409)
    return {"macro_id": name, "kind": kind, "plan_sha256": digest,
            "state": "awaiting_operator" if confirmed else "preview",
            "steps": [{"profile": profile, "available": approval is not None} for profile, approval in approvals],
            "test_executed": False, "files_written": False,
            "notice": "请在本机面板按顺序分别审批预设测试。此宏不提交或运行命令。"}


def _params(spec: dict) -> set[str]:
    return {name for step in spec["steps"] for marker, name in _INPUTS.items() if step.get(name) == marker}


def catalog() -> list[dict]:
    workspace = graphflow._workspace()
    specs = _specs(workspace)
    try:
        specs = {**graphflow_trajectories.specs(workspace), **specs}
    except GraphFlowError:
        pass  # An optional trajectory source must not hide installed workflows.
    return [{"name": name, "title": spec["title"], "parameters": sorted(_params(spec)),
             "steps": len(spec["steps"])} for name, spec in sorted(specs.items())]


def _resolve(workspace: Path, name: str) -> dict:
    spec = _specs(workspace).get(name)
    if spec is None and graphflow_trajectories.ID.fullmatch(name):
        spec = graphflow_trajectories.specs(workspace).get(name)
    if spec is None:
        raise GraphFlowError("没有找到该工作流。", 404)
    return spec


def preview(name: str, target: str = "", query: str = "") -> dict:
    if (not isinstance(name, str) or not _NAME.fullmatch(name)
            or not isinstance(target, str) or not isinstance(query, str)):
        raise GraphFlowError("工作流名称或参数格式不正确。")
    workspace = graphflow._workspace()
    spec = _resolve(workspace, name)
    inputs = {"target": target.strip(), "query": query.strip()}
    if {key for key, value in inputs.items() if value} != _params(spec):
        raise GraphFlowError("工作流参数不完整或包含多余参数。")
    steps = []
    for template in spec["steps"]:
        step = dict(template)
        for marker, key in _INPUTS.items():
            if step.get(key) == marker:
                step[key] = inputs[key]
        if step["op"] in {"read", "impact"}:
            q, t, _, _ = graphflow.validate_read(step.get("query", ""), step.get("target", ""),
                                                 step.get("skeleton", False), step["op"] == "impact")
            if "query" in step:
                step["query"] = q
            if "target" in step:
                step["target"] = t
        steps.append(step)
    proposal = {"name": name, "title": spec["title"], "steps": steps, "read_only": True}
    digest = hashlib.sha256(json.dumps([str(workspace), proposal], ensure_ascii=True, sort_keys=True).encode())
    return {**proposal, "plan_sha256": digest.hexdigest()}


def _clip(text: str, budget: int) -> tuple[str, bool]:
    if len(json.dumps(text, ensure_ascii=False)) <= budget:
        return text, False
    low, high = 0, min(len(text), budget)
    while low < high:
        mid = (low + high + 1) // 2
        if len(json.dumps(text[:mid], ensure_ascii=False)) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low], True


def replay(name: str, plan_sha256: str, target: str = "", query: str = "", *, budget: int = 12000) -> dict:
    proposal = preview(name, target, query)
    if plan_sha256 != proposal["plan_sha256"]:
        raise GraphFlowError("工作流配置或参数已变化，请重新预览。", 409)
    workspace = graphflow._workspace()
    results = []
    status = "completed"
    per_step = (budget - 1100) // len(proposal["steps"])
    for step in proposal["steps"]:
        op = step["op"]
        try:
            if op == "read":
                text = graphflow.read(step.get("query", ""), step.get("target", ""), step.get("skeleton", False))["text"]
            elif op == "read_batch":
                text = graphflow.read_batch(targets=step["targets"])["text"]
            elif op == "impact":
                impact = graphflow.impact(step["target"])
                text = json.dumps(impact, ensure_ascii=False)
            elif op == "changes":
                files = graphflow_tests._changed(workspace)
                text = json.dumps({"count": len(files), "paths": files[:24], "more": len(files) > 24}, ensure_ascii=False)
            elif op == "cochange":
                text = json.dumps(graphflow_history.mine(workspace, budget=per_step // 2), ensure_ascii=False)
            else:
                planned = graphflow_tests.plan(workspace)["profiles"]
                text = json.dumps({"profiles": [row["profile"] for row in planned], "executed": False,
                                   "next": "在本机受控测试面板单独批准。"}, ensure_ascii=False)
            text, clipped = _clip(text, per_step)
            results.append({"op": op, "state": "completed", "text": text, "truncated": clipped})
        except GraphFlowError as exc:
            results.append({"op": op, "state": "blocked", "error": str(exc), "status": exc.status_code})
            status = "blocked"
            break
    return {"name": name, "plan_sha256": proposal["plan_sha256"], "state": status, "steps": results,
            "test_executed": False, "files_written": False}


def macro(action: str, id: str = "", args: list[str] | None = None, rootDir: str = "",
          plan_sha256: str = "", *, budget: int = 12000) -> dict:
    if action not in MACRO_ACTIONS:
        raise GraphFlowError("不支持该宏动作；星愿不运行旧脚本。", 403)
    workspace = graphflow.checked_workspace(rootDir)
    if args is None:
        args = []
    if (not isinstance(id, str) or not isinstance(plan_sha256, str) or not isinstance(args, list)
            or len(args) > graphflow_edits.MAX_EDIT_FILES or any(not isinstance(arg, str) or not arg.strip() for arg in args)):
        raise GraphFlowError("宏参数格式不正确。")
    if action == "mine-git":
        if id or args or plan_sha256:
            raise GraphFlowError("历史共改分析不接受运行参数。")
        return graphflow_history.mine(workspace, budget=budget)
    if action == "mine":
        if id or args or plan_sha256:
            raise GraphFlowError("轨迹挖掘不接受运行参数。")
        report = graphflow_trajectories.mine(workspace, budget=budget)
        if report["candidates"]:
            names = _specs(workspace)
            report["candidates"] = [item for item in report["candidates"] if item["id"] not in names]
        return report
    if action in ("task-list", "task-run"):
        from . import graphflow_tasks

        if args or (action == "task-list" and (id or plan_sha256)):
            raise GraphFlowError("任务只使用保存的参数，不接受替换参数。")
        if action == "task-list":
            return graphflow_tasks.recent()
        return graphflow_tasks.replay_task(id, plan_sha256, budget=budget)
    codemods = _codemods(workspace)
    commands = _command_macros(workspace)
    if action == "list":
        if id or args or plan_sha256:
            raise GraphFlowError("宏目录不接受运行参数。")
        report = {"macros": catalog(), "read_only": True}
        if codemods:
            report["edit_macros"] = [{"id": name, "kind": "edit-codemod", "requires_files": True}
                                     for name in sorted(codemods)]
        if commands:
            report["test_macros"] = [{"id": name, "kind": kind, "profiles": profiles}
                                     for name, (kind, profiles, _) in sorted(commands.items())]
        return report
    if id in commands:
        if args or (action == "preview" and plan_sha256):
            raise GraphFlowError("命令宏不接受动态参数，预览不接受确认哈希。")
        return _command_handoff(workspace, id, commands[id], plan_sha256)
    if id in codemods:
        if action == "preview" and plan_sha256:
            raise GraphFlowError("预览不接受审批哈希。")
        return _codemod(workspace, id, codemods[id], args, plan_sha256, budget)
    params = sorted(_params(_resolve(workspace, id)))
    if len(args) != len(params):
        raise GraphFlowError("请按目录中 parameters 的顺序传递宏参数。")
    inputs = dict(zip(params, args))
    if action == "preview" and plan_sha256:
        raise GraphFlowError("预览不接受审批哈希。")
    if not plan_sha256:
        return {**preview(id, **inputs), "state": "preview", "test_executed": False, "files_written": False}
    return replay(id, plan_sha256, **inputs, budget=budget)
