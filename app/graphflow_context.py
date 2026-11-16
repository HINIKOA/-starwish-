"""Compact live code anchors plus previously reported project outcomes."""
from __future__ import annotations

import json

from . import graphflow, graphflow_tasks
from .db import estimate_tokens
from .graphflow import GraphFlowError


def build(query: str, budget_tokens: int = 750, rootDir: str = "") -> dict:
    graphflow.checked_workspace(rootDir)
    query, _, _, _ = graphflow.validate_read(query=query)
    if type(budget_tokens) is not int or not 256 <= budget_tokens <= 1400:
        raise GraphFlowError("上下文预算必须为 256–1400 个估算 token。")
    anchors = []
    code_status = "ready"
    try:
        result = graphflow.read(query=query, anchors_only=True)
        if result["index"]["partial"]:
            code_status = "partial"
        for item in result["matches"]:
            if not isinstance(item, dict):
                continue
            file, start, end = item.get("file"), item.get("line"), item.get("endLine")
            if (not isinstance(file, str) or not graphflow._safe_source(file)
                    or type(start) is not int or type(end) is not int or not 1 <= start <= end <= 9999999):
                continue
            anchors.append({"file": file, "start_line": start, "end_line": end,
                            "symbol": str(item.get("name", ""))[:160],
                            "read_target": f"{file}:{start}-{min(end, start + 199)}"})
    except GraphFlowError as exc:
        code_status = f"unavailable_{exc.status_code}"
    history_unavailable = False
    try:
        outcomes = graphflow_tasks.find_completed(query, check_code=True)
    except GraphFlowError as exc:
        if exc.status_code != 503:
            raise
        outcomes = []
        history_unavailable = True
    packet = {
        "summary": "代码定位来自当前索引；任务结论为历史回填，不等于当前验证。按 read_target 读取所需代码。",
        "anchors": [],
        "outcomes": [],
        "code_status": code_status,
        "tokenBudget": {"limit": budget_tokens, "estimated": 0, "approximate": True},
        "truncated": False,
    }
    if history_unavailable:
        packet["outcomes_status"] = "unavailable"

    def serialized() -> str:
        return json.dumps(packet, ensure_ascii=False)

    for index in range(max(len(anchors), len(outcomes))):
        for key, items in (("anchors", anchors), ("outcomes", outcomes)):
            if index >= len(items):
                continue
            packet[key].append(items[index])
            if estimate_tokens(serialized()) + 3 > budget_tokens or len(serialized()) > 5000:
                packet[key].pop()
                packet["truncated"] = True
    packet["tokenBudget"]["estimated"] = estimate_tokens(serialized())
    # The estimate itself occupies at most one additional estimated token.
    packet["tokenBudget"]["estimated"] = estimate_tokens(serialized())
    return packet
