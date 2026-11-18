"""Local panel routes for the opt-in code workspace."""
import os
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import config, graphflow, graphflow_context, graphflow_diagnostics, graphflow_edit_queue, graphflow_edits, graphflow_planner, graphflow_tasks, graphflow_tests, graphflow_workflows
from . import graphflow_artifacts
from . import graphflow_legacy_tasks
from . import graphflow_workspace_check, graphflow_workspaces

router = APIRouter()


def require_local_project(request: Request) -> None:
    loopback = {"127.0.0.1", "::1"}
    try:
        host = urlsplit("//" + request.headers.get("host", "")).hostname
    except ValueError:
        host = None
    origin = request.headers.get("origin")
    if (not request.client or request.client.host not in loopback or host not in loopback | {"localhost"}
            or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}
            or (origin and origin != f"{request.url.scheme}://{request.headers.get('host', '')}")):
        raise HTTPException(403, "项目代码只允许从本机星愿页面访问。")


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(default="", max_length=300)
    target: str = Field(default="", max_length=500)
    skeleton: bool = False


class ImpactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: str = Field(max_length=500)


class GraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: str = Field(default="", max_length=500)
    node_limit: int = Field(default=20, ge=1, le=graphflow.GRAPH_NODE_LIMIT)
    edge_limit: int = Field(default=32, ge=0, le=graphflow.GRAPH_EDGE_LIMIT)


class EditPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(max_length=500)
    start_line: int
    end_line: int
    expected: str = Field(max_length=graphflow_edits.MAX_BLOCK)
    replacement: str = Field(max_length=graphflow_edits.MAX_BLOCK)


class EditCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preview_id: str = Field(max_length=32)


class EditApprovalRequest(EditCheckRequest):
    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _approval(request: Request, operation, *args, feature_enabled: bool | None = None):
    require_local_project(request)
    try:
        graphflow_edit_queue.require_operator(request.headers.get("x-starwish-operator-key", ""),
                                              feature_enabled=feature_enabled)
        result = operation(graphflow._workspace(), *args)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except graphflow.GraphFlowError as exc:
        raise HTTPException(exc.status_code, str(exc), headers={"Cache-Control": "no-store"}) from exc


@router.get("/graphflow", include_in_schema=False)
def graphflow_page(request: Request):
    require_local_project(request)
    return FileResponse(config.WEB_DIR / "graphflow.html")


@router.get("/api/graphflow/status")
def graphflow_status(request: Request, workspace_id: str = ""):
    require_local_project(request)
    if workspace_id:
        def read_status():
            return {**graphflow.status(), "read_only_workspace": True}
        return _readonly(request, workspace_id, read_status)
    status = {**graphflow.status(), "edit_queue": graphflow_edit_queue.enabled(),
              "test_enabled": graphflow_tests.enabled(), "tasks_enabled": graphflow_tasks.enabled(),
              "legacy_tasks_enabled": graphflow_legacy_tasks.enabled(),
              "operator_key_configured": graphflow_edit_queue.operator_ready()}
    if not status["workspace"] and not os.environ.get("STARWISH_GRAPHFLOW_WORKSPACE"):
        status.update(authorization_required=True, reason="连接星愿 MCP 后，让 Agent 查询当前项目，或手动添加 Git 工作区。")
    claim = request.headers.get(graphflow_workspace_check.HEADER)
    if claim is not None:
        try:
            match = graphflow_workspace_check.matches(graphflow.checked_workspace(), claim)
        except (graphflow.GraphFlowError, OSError, RuntimeError):
            status["workspace_check"] = "unavailable"
        else:
            status["workspace_check"] = "matched" if match else "mismatch"
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


def _readonly(request: Request, workspace_id: str, operation, *args, **kwargs):
    require_local_project(request)
    try:
        with graphflow_workspaces.panel_scope(workspace_id):
            return JSONResponse(operation(*args, **kwargs), headers={"Cache-Control": "no-store"})
    except graphflow.GraphFlowError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


def _require_workspace_confirmation(request: Request) -> None:
    require_local_project(request)
    if not request.headers.get("origin"):
        raise HTTPException(403, "请在本机星愿页面确认授权。")


class WorkspacePathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=1024)


class WorkspaceDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ids: list[str] = Field(min_length=1, max_length=graphflow_workspaces.MAX_WORKSPACES)
    approve: bool


class WorkspacePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    auto_authorize: bool


@router.get("/api/graphflow/workspaces")
def graphflow_workspace_list(request: Request):
    return _readonly(request, "", graphflow_workspaces.view)


@router.post("/api/graphflow/workspaces/policy")
def graphflow_workspace_policy(request: Request, body: WorkspacePolicyRequest):
    _require_workspace_confirmation(request)
    def apply_policy():
        graphflow_workspaces.set_automatic(body.auto_authorize)
        return graphflow_workspaces.view()
    return _readonly(request, "", apply_policy)


@router.post("/api/graphflow/workspaces/discover")
def graphflow_workspace_discover(request: Request, body: WorkspacePathRequest):
    _require_workspace_confirmation(request)
    def discover():
        graphflow_workspaces.discover(body.path)
        return graphflow_workspaces.view()
    return _readonly(request, "", discover)


@router.post("/api/graphflow/workspaces/pick")
def graphflow_workspace_pick(request: Request):
    _require_workspace_confirmation(request)
    def pick():
        graphflow_workspaces.pick_folder()
        return graphflow_workspaces.view()
    return _readonly(request, "", pick)


@router.post("/api/graphflow/workspaces/decision")
def graphflow_workspace_decision(request: Request, body: WorkspaceDecisionRequest):
    _require_workspace_confirmation(request)
    def decide():
        graphflow_workspaces.decide(body.ids, body.approve)
        return graphflow_workspaces.view()
    return _readonly(request, "", decide)


@router.get("/api/graphflow/diagnose")
def graphflow_diagnose(request: Request):
    require_local_project(request)
    return JSONResponse(graphflow_diagnostics.inspect(), headers={"Cache-Control": "no-store"})


@router.post("/api/graphflow/read")
def graphflow_read(request: Request, body: ReadRequest, workspace_id: str = ""):
    return _readonly(request, workspace_id, graphflow.read, body.query, body.target, body.skeleton)


@router.post("/api/graphflow/impact")
def graphflow_impact(request: Request, body: ImpactRequest, workspace_id: str = ""):
    return _readonly(request, workspace_id, graphflow.impact, body.target)


@router.post("/api/graphflow/edit/preview")
def graphflow_edit_preview(request: Request, body: EditPreviewRequest):
    require_local_project(request)
    try:
        return graphflow_edits.preview(**body.model_dump())
    except graphflow.GraphFlowError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/api/graphflow/edit/check")
def graphflow_edit_check(request: Request, body: EditCheckRequest):
    require_local_project(request)
    try:
        return graphflow_edits.check(body.preview_id)
    except graphflow.GraphFlowError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/api/graphflow/edit/queue")
def graphflow_edit_queue_list(request: Request):
    return _approval(request, graphflow_edit_queue.list_items)


@router.post("/api/graphflow/edit/apply")
def graphflow_edit_apply(request: Request, body: EditApprovalRequest):
    return _approval(request, graphflow_edit_queue.mutate, body.preview_id, body.confirmed_sha256, "apply")


@router.post("/api/graphflow/edit/rollback")
def graphflow_edit_rollback(request: Request, body: EditApprovalRequest):
    return _approval(request, graphflow_edit_queue.mutate, body.preview_id, body.confirmed_sha256, "rollback")


@router.post("/api/graphflow/edit/recover")
def graphflow_edit_recover(request: Request, body: EditCheckRequest):
    return _approval(request, graphflow_edit_queue.recover, body.preview_id)


class TestRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


@router.get("/api/graphflow/test/plan")
def graphflow_test_plan(request: Request):
    return _approval(request, lambda workspace: graphflow_tests.plan(workspace, operator=True),
                     feature_enabled=graphflow_tests.enabled())


@router.post("/api/graphflow/test/run")
def graphflow_test_run(request: Request, body: TestRunRequest):
    return _approval(request, _run_test, body.profile, body.approval_sha256, body.task_id,
                     feature_enabled=graphflow_tests.enabled())


def _run_test(workspace, profile: str, approval_sha256: str, task_id: str | None) -> dict:
    if task_id:
        graphflow_tasks.require_verification_task(workspace, task_id)
    result = graphflow_tests.run(workspace, profile, approval_sha256)
    if task_id:
        try:
            result["task_verification"] = {"status": "linked",
                                           **graphflow_tasks.record_verification(workspace, task_id, result["run_id"])}
        except graphflow.GraphFlowError as exc:
            result["task_verification"] = {"status": "not_linked", "task_id": task_id, "reason": str(exc)}
    return result


@router.get("/api/graphflow/test/records")
def graphflow_test_records(request: Request):
    return _approval(request, graphflow_tests.records, feature_enabled=graphflow_tests.enabled())


@router.get("/api/graphflow/test/log")
def graphflow_test_log(request: Request, run_id: str, offset: int = 0):
    return _approval(request, graphflow_tests.logs, run_id, offset, feature_enabled=graphflow_tests.enabled())


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    target: str = Field(default="", max_length=500)
    query: str = Field(default="", max_length=300)


class WorkflowReplayRequest(WorkflowRequest):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkflowUnpinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^trace_[0-9a-f]{16}$")


class WorkflowPinRequest(WorkflowUnpinRequest):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _workflow(request: Request, operation, **args):
    require_local_project(request)
    try:
        return JSONResponse(operation(**args), headers={"Cache-Control": "no-store"})
    except graphflow.GraphFlowError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/api/graphflow/workflow/catalog")
def graphflow_workflow_catalog(request: Request):
    return _workflow(request, graphflow_workflows.catalog)


@router.post("/api/graphflow/workflow/preview")
def graphflow_workflow_preview(request: Request, body: WorkflowRequest):
    return _workflow(request, graphflow_workflows.preview, **body.model_dump())


@router.post("/api/graphflow/workflow/replay")
def graphflow_workflow_replay(request: Request, body: WorkflowReplayRequest):
    return _workflow(request, graphflow_workflows.replay, **body.model_dump())


@router.post("/api/graphflow/workflow/pin")
def graphflow_workflow_pin(request: Request, body: WorkflowPinRequest):
    return _approval(request, graphflow_workflows.pin_trajectory, body.name, body.plan_sha256, feature_enabled=True)


@router.post("/api/graphflow/workflow/unpin")
def graphflow_workflow_unpin(request: Request, body: WorkflowUnpinRequest):
    return _approval(request, graphflow_workflows.unpin_trajectory, body.name, feature_enabled=True)


class TaskCreateRequest(WorkflowReplayRequest):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")


class TaskReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(ge=0)
    state: str = Field(pattern=r"^(pending|blocked|completed)$")
    summary: str = Field(max_length=400)
    references: list[str] = Field(default_factory=list, max_length=5)


@router.get("/api/graphflow/tasks/backlog")
def graphflow_task_backlog(request: Request):
    return _workflow(request, graphflow_tasks.backlog)


@router.get("/api/graphflow/tasks/detail")
def graphflow_task_detail(request: Request, task_id: str):
    return _workflow(request, graphflow_tasks.detail, task_id=task_id)


@router.post("/api/graphflow/tasks/create")
def graphflow_task_create(request: Request, body: TaskCreateRequest):
    return _workflow(request, graphflow_tasks.create, **body.model_dump(), source="web")


@router.post("/api/graphflow/tasks/report")
def graphflow_task_report(request: Request, body: TaskReportRequest):
    return _workflow(request, graphflow_tasks.report, **body.model_dump(), source="web")


class LegacyTaskPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    legacy_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    target: str = Field(min_length=1, max_length=500)


class LegacyTaskImportRequest(LegacyTaskPreviewRequest):
    confirm_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.get("/api/graphflow/tasks/legacy")
def graphflow_legacy_task_list(request: Request, after: str = ""):
    return _workflow(request, graphflow_legacy_tasks.transfer, action="list", after=after)


@router.post("/api/graphflow/tasks/legacy/preview")
def graphflow_legacy_task_preview(request: Request, body: LegacyTaskPreviewRequest):
    return _workflow(request, graphflow_legacy_tasks.transfer, action="preview", **body.model_dump())


@router.post("/api/graphflow/tasks/legacy/import")
def graphflow_legacy_task_import(request: Request, body: LegacyTaskImportRequest):
    return _workflow(request, graphflow_legacy_tasks.transfer, action="import", **body.model_dump())


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(max_length=300)
    budget_tokens: int = Field(default=750, ge=256, le=1400)


@router.post("/api/graphflow/context")
def graphflow_context_query(request: Request, body: ContextRequest, workspace_id: str = ""):
    return _readonly(request, workspace_id, graphflow_context.build, **body.model_dump())


class ReindexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: str = Field(default="incremental", pattern=r"^(incremental|full)$")
    file_path: str = Field(default="", max_length=500)


class ArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["export", "import"]
    artifact: str = Field(default="", max_length=graphflow_artifacts.MAX_WIRE)


@router.post("/api/graphflow/artifact")
def graphflow_artifact(request: Request, body: ArtifactRequest):
    return _workflow(request, graphflow_artifacts.exchange, **body.model_dump())


@router.post("/api/graphflow/graph")
def graphflow_graph(request: Request, body: GraphRequest, workspace_id: str = ""):
    return _readonly(request, workspace_id, graphflow.dependency_graph, **body.model_dump())


@router.post("/api/graphflow/index")
def graphflow_index(request: Request, body: ReindexRequest):
    return _workflow(request, graphflow.reindex, **body.model_dump())


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task: str = Field(max_length=1200)
    mode: str = Field(default="simple", pattern=r"^(simple|insight)$")
    intent: str = Field(default="review", pattern=r"^(review|change)$")
    target: str = Field(default="", max_length=500)
    query: str = Field(default="", max_length=300)


@router.post("/api/graphflow/plan")
def graphflow_plan(request: Request, body: PlanRequest):
    return _workflow(request, graphflow_planner.build, **body.model_dump())
