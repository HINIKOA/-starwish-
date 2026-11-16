"""Merge review annotations without allowing them to rewrite a project plan."""
from __future__ import annotations

import hashlib
import json
import re

from . import graphflow, graphflow_planner
from .graphflow import GraphFlowError


def review(task: str, rootDir: str = "", query: str = "", target: str = "", intent: str = "review",
           configPath: str = "", plan_sha256: str = "", observations: list[dict] | None = None) -> dict:
    if not isinstance(plan_sha256, str) or (plan_sha256 and not re.fullmatch(r"[0-9a-f]{64}", plan_sha256)):
        raise GraphFlowError("计划哈希格式无效。")
    if observations is not None and (not isinstance(observations, list) or not 1 <= len(observations) <= 7
                                     or not plan_sha256):
        raise GraphFlowError("提交意见需要预览哈希和 1 到 7 条审查意见。")
    if observations is None and plan_sha256:
        raise GraphFlowError("仅预览时不要传入确认哈希。")
    workspace = graphflow.checked_workspace(rootDir)
    plan = graphflow_planner.build(task, "insight", str(workspace), query, target, intent, configPath)
    digest = hashlib.sha256(json.dumps([str(workspace), task.strip(), plan], ensure_ascii=True,
                                       sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if plan_sha256 and plan_sha256 != digest:
        raise GraphFlowError("计划或证据已变化，请重新生成洞察。", 409)
    steps = {step["id"] for step in plan["plan"]["steps"]}
    anchors = {item["read_target"] for item in plan["context"]["anchors"]}
    merged = {}
    for item in observations or []:
        if not isinstance(item, dict) or set(item) - {"step", "note", "anchors"}:
            raise GraphFlowError("意见只接受步骤、说明和当前锚点。")
        step, note, refs = item.get("step"), item.get("note"), item.get("anchors", [])
        if (not isinstance(step, str) or step not in steps or step in merged
                or not isinstance(note, str) or not 1 <= len(note.strip()) <= 200
                or any(ord(char) < 32 for char in note) or not isinstance(refs, list) or len(refs) > 3
                or any(not isinstance(ref, str) or ref not in anchors for ref in refs)):
            raise GraphFlowError("意见须引用唯一的当前步骤；锚点必须来自这份计划。")
        merged[step] = {"step": step, "note": note.strip(), "anchors": list(dict.fromkeys(refs)),
                        "source": "agent_reported", "verified": False}
    return {**plan, "plan_sha256": digest, "state": "reviewed" if merged else "preview",
            "observations": [merged[step["id"]] for step in plan["plan"]["steps"] if step["id"] in merged],
            "approved": False}
