"""Legacy episode bridge backed by StarWish's authorized project ledger."""
import secrets

from . import graphflow, graphflow_planner, graphflow_tasks
from .graphflow import GraphFlowError


def run(task: str, configPath: str = "", rootDir: str = "", query: str = "", target: str = "",
        intent: str = "review", requestId: str = "", *, source: str = "mcp") -> dict:
    if not graphflow_tasks.enabled():
        raise GraphFlowError("请先启用项目任务台账。", 503)
    if not isinstance(requestId, str):
        raise GraphFlowError("请求编号格式不正确。")
    plan = graphflow_planner.build(task, rootDir=rootDir, query=query, target=target, intent=intent, configPath=configPath)
    item = graphflow_tasks.create(requestId or secrets.token_hex(16), **plan["read_only_workflow"], source=source)
    if item["duplicate"]:
        raise GraphFlowError("请求编号已登记；请通过任务台账查看已保存的计划。", 409)
    return {"episodeId": item["id"], "revision": item["revision"], "state": item["state"],
            "executionDescriptor": plan, "persisted": True, "executed": False}


def report(episodeId: str, success: bool, lessons: list[str] | None = None, configPath: str = "",
           rootDir: str = "", revision: int | None = None, references: list[str] | None = None,
           *, source: str = "mcp") -> dict:
    if type(success) is not bool or configPath != "":
        raise GraphFlowError("请提供明确的 success，不能传入配置文件路径。")
    if lessons is None:
        lessons = []
    if (not isinstance(lessons, list) or len(lessons) > 4 or any(not isinstance(item, str)
            or not item.strip() or any(ord(char) < 32 for char in item) for item in lessons)):
        raise GraphFlowError("最多回填四条单行经验。")
    summary = "；".join(item.strip() for item in lessons) or ("报告已完成。" if success else "报告未完成。")
    if len(summary) > 400:
        raise GraphFlowError("合并后的回填经验最多 400 字。")
    graphflow.checked_workspace(rootDir)
    # The ledger still performs an optimistic revision check inside its write transaction.
    if revision is None:
        revision = graphflow_tasks.detail(episodeId)["revision"]
    saved = graphflow_tasks.report(episodeId, revision, "completed" if success else "blocked", summary,
                                   [] if references is None else references, source=source)
    return {**saved, "episodeId": episodeId, "success": success, "current_verification": False}
