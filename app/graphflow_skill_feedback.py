"""Read-only project workflow lessons from explicitly reported outcomes."""
import json
import re

from . import graphflow, graphflow_tasks


def insights(limit: int = 5, rootDir: str = "") -> dict:
    if type(limit) is not int or not 1 <= limit <= 20:
        raise graphflow.GraphFlowError("技能洞察数量必须在 1 到 20 之间。")
    workspace = graphflow.checked_workspace(rootDir)
    report = {"status": "disabled", "sampled": 0, "patterns": [], "read_only": True,
              "basis": "historical_reported_evidence", "current_verification": False}
    if not graphflow_tasks.enabled():
        return report
    try:
        with graphflow_tasks.read_ledger(workspace) as db:
            if db is None:
                return {**report, "status": "not_initialized"}
            rows = db.execute("""SELECT t.id, json_extract(t.plan, '$.name'), e.evidence FROM tasks t
                                 JOIN task_events e ON e.task_id=t.id AND e.revision=t.revision
                                 WHERE t.state='completed' AND e.state='completed'
                                 ORDER BY t.updated DESC, t.id LIMIT 200""").fetchall()
            linked = graphflow_tasks.latest_verifications(db, [row[0] for row in rows])
        groups = {}
        for task_id, name, payload in rows:
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name):
                continue
            evidence = json.loads(payload)
            if not isinstance(evidence, list):
                continue
            kinds = {"code" if item.get("kind") == "code" else "passed_test" for item in evidence
                     if isinstance(item, dict) and (item.get("kind") == "code" or
                        (item.get("kind") == "test" and item.get("state") == "passed" and item.get("exit_code") == 0))}
            if task_id in linked:
                kinds.discard("passed_test")
                if linked[task_id]["state"] == "passed" and linked[task_id]["exit_code"] == 0:
                    kinds.add("passed_test")
            if not kinds:
                continue
            group = groups.setdefault(name, {"workflow": name, "reports": 0, "code_reports": 0,
                                              "passed_test_reports": 0, "example_task_ids": []})
            group["reports"] += 1
            group["code_reports"] += "code" in kinds
            group["passed_test_reports"] += "passed_test" in kinds
            if len(group["example_task_ids"]) < 2:
                group["example_task_ids"].append(task_id)
        patterns = sorted(groups.values(), key=lambda item: (-item["reports"], item["workflow"]))
        return {**report, "status": "ready", "sampled": len(rows), "patterns": patterns[:limit], "more": len(patterns) > limit}
    except ValueError:
        return {**report, "status": "unavailable"}


def guide(rootDir: str = "") -> dict:
    graphflow.checked_workspace(rootDir)
    return {"server": "starwish", "read_only": True, "task_ledger_enabled": graphflow_tasks.enabled(),
            "steps": [
                {"tool": "graphflow_context", "use": "先按主题定位代码和历史回填。"},
                {"tool": "graphflow_read", "use": "再按 read_target 读取所需源码。"},
                {"tool": "graphflow_skill_insights", "use": "查已完成且有证据的工作流，示例任务仅供复核。"},
                {"tool": "graphflow_plan", "use": "为当前改动制定计划；预览不等于授权写入或测试。"},
                {"tool": "graphflow_task_report", "use": "启用台账后显式回填新结果；历史结果不替代当前验证。"},
            ], "approval": "实际写入、回滚和运行测试须在本机面板审批。"}
