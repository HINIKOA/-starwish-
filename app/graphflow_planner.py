"""Evidence-first, non-executing plans for the legacy GraphFlow planning entry."""
from __future__ import annotations

from . import graphflow, graphflow_context, graphflow_workflows
from .graphflow import GraphFlowError


def build(task: str, mode: str = "simple", rootDir: str = "", query: str = "",
          target: str = "", intent: str = "review", configPath: str = "") -> dict:
    if not isinstance(task, str) or not 1 <= len(task.strip()) <= 1200:
        raise GraphFlowError("请提供 1–1200 字的任务说明。")
    if mode not in ("simple", "insight") or intent not in ("review", "change"):
        raise GraphFlowError("未知规划模式或任务意图。")
    if not isinstance(configPath, str) or configPath:
        raise GraphFlowError("星愿规划不接受客户端传入配置路径。")
    if not isinstance(query, str) or len(query) > 300 or not isinstance(target, str):
        raise GraphFlowError("定位参数格式不正确。")
    graphflow.checked_workspace(rootDir)
    if target:
        _, target, _, symbol = graphflow.validate_read(target=target, impact_only=True)
    else:
        symbol = ""
    task = task.strip()
    code_query = query.strip() or symbol or " ".join(task.split())[:300]
    evidence = graphflow_context.build(code_query, budget_tokens=420)
    workflow_inputs = {"name": "symbol-review", "target": target} if target else {"name": "change-review"}
    workflow = graphflow_workflows.preview(**workflow_inputs)
    # Keep the replay receipt separate from the human DAG: neither implies approval.
    receipt = {**workflow_inputs, "plan_sha256": workflow["plan_sha256"]}
    steps = []

    def add(identifier, title, description, after=(), gate="read_only"):
        steps.append({"id": identifier, "title": title, "description": description,
                      "dependsOn": list(after), "estimate": "not_estimated", "gate": gate})

    add("locate", "核对当前代码", "打开当前代码锚点，确认范围；缺少锚点时先收窄查询。")
    add("impact", "检查影响与证据", "检查相关调用方、边界行为与既有测试；历史回填不等于本次验证。", ["locate"])
    if intent == "change":
        add("preview", "生成变更预览", "核对原文和目标行，再生成 diff。此步骤不写文件。", ["impact"])
        add("apply", "审批后应用", "由操作者在本机核对 diff，确认哈希并批准。", ["preview"], "operator_approval")
        add("test", "审批后验证", "用预先配置的测试验证变更，记录退出状态和测试编号。", ["apply"], "operator_approval")
        add("rollback", "失败时评估回滚", "只在需要时请求本机回滚；若有并发人工修改，先处理冲突。",
            ["test"], "conditional_operator_approval")
        steps[-1]["condition"] = "verification_failed"
        add("report", "记录实际结果", "记录测试与回滚的实际结果，不根据计划或预览推定完成。",
            ["test"], "explicit_report")
    else:
        add("report", "回报审查结论", "引用当前文件位置和已知限制；仅在明确需要时将结论回填台账。",
            ["impact"], "explicit_report")
    challenges = [
        {"id": "scope", "question": "当前代码锚点是否足以证明修改或审查范围？",
         "evidence": "anchors_available" if evidence["anchors"] else "anchors_missing"},
        {"id": "history", "question": "哪些结论仍需按当前版本验证？", "evidence": "history_not_live_verification"},
    ]
    if target:
        challenges.append({"id": "callers", "question": "哪些调用方依赖该符号现有的输入输出约定？",
                           "tool": "graphflow_impact", "target": target})
    edges = [{"from": dep, "to": step["id"]} for step in steps for dep in step["dependsOn"]]
    if intent == "change":
        edges.append({"from": "rollback", "to": "report", "condition": "verification_failed"})
    result = {
        "mode": mode, "intent": intent, "provider": "starwish_deterministic",
        "ideas": ["按代码位置逐步核对证据", "将读取、写入审批、测试审批和结果回填分开"],
        "plan": {"steps": steps, "dag": {"nodes": [step["id"] for step in steps], "edges": edges}},
        "context": evidence, "challenges": challenges,
        "read_only_workflow": receipt, "executed": False, "persisted": False,
    }
    if mode == "insight":
        result["insight"] = {
            "six_hats_questions": {
                "facts": "哪些结论已有当前源码或测试支持？",
                "concerns": "调用方或操作者最担心什么？",
                "risks": "哪些兼容、数据或回滚风险尚未验证？",
                "benefits": "什么可观察结果能证明改进？",
                "alternatives": "能否用更局部的修改达成目标？",
                "process": "哪些步骤需要人在场确认？",
            },
            "five_whys": ["现象为何发生？", "哪些证据支持这个直接原因？", "为何没有被边界或测试阻止？",
                          "为何相关设计或流程允许该条件出现？", "什么证据能反驳最终原因？"],
            "status": "questions_not_findings",
        }
    return result
