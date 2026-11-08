"""星愿 StarWish —— FastAPI 主程序：REST API + 网页面板 + OpenAI 兼容代理 + 定时任务。"""
from __future__ import annotations

import asyncio
import json
import os
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import hmac

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import __version__, agent, archive, datasync, jobs, organize, config, core, evolve, eye, fusion, importers, maintenance, providers, semantic, stars, sync, voice
from .embed import models as embed_models
from .db import init_db, log_event, read_events
from .proxy import router as proxy_router

log = logging.getLogger("starwish")


# ---------------- 定时任务（简单 asyncio 循环，无需 APScheduler） ----------------
async def scheduler_loop():
    while True:
        try:
            await asyncio.sleep(60)
            cfg = config.load_config()
            st = config.load_state()
            now = time.time()
            sch = cfg["schedules"]

            def due(key, hours):
                return hours and float(hours) > 0 and now - float(st.get(key) or 0) >= float(hours) * 3600

            if due("last_consolidate_t", sch.get("consolidate_hours")) and maintenance.llm_ready(cfg):
                config.update_state(last_consolidate_t=now)
                await run_in_threadpool(_consolidate_job)
            if cfg["memory"].get("fuse_auto", True):
                try:
                    await run_in_threadpool(fusion.auto_fuse, cfg)
                except Exception as e:  # noqa: BLE001
                    log_event("star_fuse_error", error=str(e)[:200])
            try:   # 每天 03:00（北京时间）WebDAV 增量 / 云服务器同步
                await run_in_threadpool(datasync.run_daily)
            except Exception as e:  # noqa: BLE001
                log_event("daily_error", error=str(e)[:200])
            if now - float(st.get("last_log_clean") or 0) >= 12 * 3600:
                await run_in_threadpool(datasync.clean_logs)
            if due("last_snapshot", sch.get("snapshot_hours")):
                await run_in_threadpool(sync.local_snapshot)
            if due("last_backup_try", sch.get("backup_hours")) and cfg["webdav"].get("url"):
                config.update_state(last_backup_try=now)
                try:
                    await run_in_threadpool(sync.webdav_backup)
                except Exception as e:  # 记录但不中断
                    log_event("backup_error", error=str(e))
        except asyncio.CancelledError:
            break
        except Exception as e:  # pragma: no cover
            log.warning("scheduler error: %s", e)


def _embed_boot() -> None:
    """内置向量模型常驻：不在就后台下载；然后把还没有向量的星补齐。"""
    try:
        cfg = config.load_config()
        e = cfg.get("embed") or {}
        if os.environ.get("STARWISH_NO_EMBED_BOOT"):
            return
        if not embed_models.installed("small") and e.get("auto_download", True) and embed_models.deps_ok():
            embed_models.install("small", e, deps=False)
        semantic.start_backfill()
    except Exception as ex:  # noqa: BLE001
        log.warning("向量模型启动检查失败：%s", ex)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    init_db()
    core.load_core()
    log_event("server_start", version=__version__)
    _embed_boot()
    try:
        jobs.fail_stale()
    except Exception as e:  # noqa: BLE001
        log.warning("jobs: %s", e)
    from .watch import watcher as _watcher
    if not os.environ.get("STARWISH_NO_WATCH"):
        _watcher.get().start()           # 对话监控：只活在服务进程里，关窗即停
    task = asyncio.create_task(scheduler_loop())

    def _boot_data():   # 启动时：清理过期日志；关窗期间错过的 03:00 备份补上
        try:
            datasync.clean_logs()
            datasync.run_daily(catch_up=True)
        except Exception as e:  # noqa: BLE001
            log.warning("启动补跑失败：%s", e)
    if not os.environ.get("STARWISH_NO_DAILY"):
        import threading as _th
        _th.Thread(target=_boot_data, daemon=True, name="starwish-boot-data").start()
    yield
    task.cancel()
    _watcher.get().stop()


app = FastAPI(title="星愿 StarWish", version=__version__, lifespan=lifespan)
app.include_router(proxy_router)


@app.exception_handler(core.CoreLockedError)
async def _locked(_, e):
    return JSONResponse({"detail": str(e)}, status_code=423)


@app.exception_handler(core.ConfirmRequiredError)
async def _confirm(_, e):
    return JSONResponse({"detail": str(e)}, status_code=428)


@app.exception_handler(KeyError)
async def _nf(_, e):
    return JSONResponse({"detail": str(e).strip("'")}, status_code=404)


@app.exception_handler(evolve.skills.SkillError)
async def _skill_err(_, e):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(FileNotFoundError)
async def _fnf(_, e):
    return JSONResponse({"detail": str(e)}, status_code=404)


@app.exception_handler(ValueError)
async def _bad(_, e):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(PermissionError)
async def _perm(_, e):
    return JSONResponse({"detail": str(e)}, status_code=403)


# ---------------- 模型 ----------------
class CoreEdit(BaseModel):
    section: str
    content: str
    confirm: bool = False
    mode: str = "replace"
    note: str = ""


class CoreUpdate(BaseModel):
    section: str
    content: str
    mode: str = "replace"          # replace / append
    instruction: str = ""          # 主人原话（可选，记入日志）
    note: str = ""


class CoreCommand(BaseModel):
    text: str
    natural: bool = True


class CoreRollback(BaseModel):
    rev: Optional[int] = None
    legacy_no: Optional[int] = None
    mode: str = "to"               # to = 回到此版本；undo = 撤销这次修改
    confirm: bool = False


class LockReq(BaseModel):
    locked: bool
    confirm: bool = False
    note: str = ""


class ProposalIn(BaseModel):
    section: str
    content: str
    reason: str = ""
    mode: str = "append"
    source: str = "api"


class Decide(BaseModel):
    confirm: bool = False
    content: Optional[str] = None


class StarIn(BaseModel):
    content: str
    summary: str = ""
    tags: Any = None
    importance: int = 3
    source: str = "api"
    session: str = ""
    created_at: Optional[float] = None
    merge_near: bool = False


class StarPatch(BaseModel):
    content: Optional[str] = None
    summary: Optional[str] = None
    tags: Any = None
    importance: Optional[int] = None


class RecallReq(BaseModel):
    query: str = ""
    budget_tokens: Optional[int] = None
    bump: bool = True


class ArchiveIn(BaseModel):
    messages: list[dict]
    session: str = ""
    source: str = "api"


class Passphrase(BaseModel):
    passphrase: str = ""


class RestoreReq(BaseModel):
    name: str
    source: str = "local"
    passphrase: Optional[str] = None
    confirm: bool = False


class ImportPath(BaseModel):
    path: str
    kind: str = "auto"


# ---------------- 基础 ----------------
@app.get("/api/health")
def health():
    exe = os.environ.get("STARWISH_EXE") or ""
    if exe:   # 单文件版：MCP 用 exe --mcp
        mcp = {"command": exe, "args": ["--mcp"]}
    else:
        mcp = {"command": "MCP接入.bat", "args": []}
    return {"ok": True, "app": "starwish", "version": __version__, "soul_dir": str(config.SOUL_DIR),
            "exe": exe, "mcp": mcp}


LAST_PING = 0.0  # 桌面版浏览器兜底模式用：页面心跳，全部关闭一段时间后自动退出


@app.post("/api/ping")
def ping():
    global LAST_PING
    LAST_PING = time.time()
    return {"ok": True}


@app.get("/api/stats")
def api_stats():
    return {**stars.stats(), "skills": evolve.skills.stats()}


@app.get("/api/events")
def api_events(limit: int = 100):
    return read_events(min(1000, limit))


# ---------------- 核心记忆（独立模块） ----------------
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def is_owner(request: Request) -> bool:
    """主人身份：配置了主人令牌 → 必须带对令牌；没配置 → 只信任本机请求。"""
    token = (config.load_config().get("core") or {}).get("owner_token") or ""
    got = request.headers.get("x-starwish-owner", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        got = got or auth[7:].strip()
    if token:
        return bool(got) and hmac.compare_digest(got, token)
    host = request.client.host if request.client else ""
    return host in LOOPBACK


def _core_view(c: dict | None = None) -> dict:
    c = c or core.load_core()
    cfg = config.masked_config().get("core", {})
    return {**c, "text": core.core_text(c), "tokens": core.core_tokens(), "section_titles": core.SECTIONS,
            "settings": cfg, "pending": len(core.list_proposals("pending"))}


@app.get("/api/core")
def get_core():
    return _core_view()


@app.put("/api/core")
def put_core(req: CoreEdit):
    """面板保存（需要 confirm=true；上锁时 423）。"""
    c = core.set_section(req.section, req.content, req.confirm, mode=req.mode, source="panel", note=req.note)
    return _core_view(c)


@app.post("/api/core/update")
def core_update(req: CoreUpdate, request: Request):
    """任意客户端的主人修改：主人身份 → 直接生效；否则 / 上锁时 → 转为提案。"""
    mode = req.mode if req.mode in ("replace", "append") else "replace"
    res = core.owner_update({core.norm_section(req.section): (req.content, mode)}, source="api",
                            authorized=is_owner(request), instruction=req.instruction, note=req.note)
    res.pop("core", None)
    return {**res, "core": _core_view()}


@app.post("/api/core/command")
def core_command(req: CoreCommand, request: Request):
    """执行一条主人指令文本（/核心 … 或「记住，以后叫我X」）。"""
    out = core.commands.run_text(req.text, source="api", authorized=is_owner(request), natural=req.natural)
    if out is None:
        return {"matched": False, "reply": "这不是核心记忆指令。\n\n" + core.commands.HELP}
    res = out.get("result") or {}
    res.pop("core", None) if isinstance(res, dict) else None
    return {"matched": True, "reply": out["reply"], "result": res, "cmd": out.get("cmd")}


@app.get("/api/core/history")
def core_history(limit: int = 100):
    items = core.list_history(min(1000, limit))
    for x in items:
        x["source_title"] = core.SOURCE_TITLES.get(x.get("source"), x.get("source"))
    return items


@app.post("/api/core/rollback")
def core_rollback(req: CoreRollback):
    res = core.rollback(rev=req.rev, confirm=req.confirm, mode="undo" if req.mode == "undo" else "to",
                        legacy_no=req.legacy_no, source="panel")
    res.pop("core", None)
    return {**res, "core": _core_view()}


@app.post("/api/core/undo")
def core_undo(request: Request):
    if not is_owner(request):
        raise PermissionError("只有主人可以撤销核心记忆修改")
    res = core.undo_last(source="api")
    res.pop("core", None)
    return {**res, "core": _core_view()}


@app.post("/api/core/lock")
def lock_core(req: LockReq):
    return core.set_lock(req.locked, req.confirm, note=req.note)


@app.get("/api/proposals")
def get_proposals(status: Optional[str] = "pending"):
    return core.list_proposals(None if status == "all" else status)


@app.post("/api/proposals")
def post_proposal(req: ProposalIn):
    return core.create_proposal(req.section, req.content, req.reason, req.source, req.mode)


@app.post("/api/proposals/{pid}/approve")
def approve(pid: int, req: Decide):
    return core.decide_proposal(pid, True, req.confirm, req.content)


@app.post("/api/proposals/{pid}/reject")
def reject(pid: int):
    return core.decide_proposal(pid, False, False)


# ---------------- 技能 / 自进化 ----------------
class SkillOp(BaseModel):
    action: str
    name: str
    content: Optional[str] = None
    category: Optional[str] = None
    old_string: Optional[str] = None
    new_string: Optional[str] = None
    replace_all: bool = False
    file_path: Optional[str] = None
    file_content: Optional[str] = None
    note: str = ""
    as_agent: bool = False          # 以 AI 身份提交（走信任矩阵 / 审批），用于外部 Agent 通过 REST 自进化


class SkillRollback(BaseModel):
    rev: int
    mode: str = "to"
    confirm: bool = False


class SkillFeedback(BaseModel):
    good: bool
    comment: str = ""


class ReviewReq(BaseModel):
    focus: str = ""
    background: bool = True


@app.get("/api/skills")
def api_skills(archived: bool = False):
    return evolve.skills.list_skills(include_archived=archived)


@app.post("/api/skills/manage")
def api_skill_manage(op: SkillOp, request: Request):
    agent = op.as_agent or not is_owner(request)
    d = op.model_dump(exclude={"note", "as_agent"})
    r = evolve.skills.manage(**d, source="api" if not agent else "api-agent",
                             trust="agent-created" if agent else "owner", note=op.note)
    if agent and r.get("success"):
        evolve.review.note_skill_manage()
    if not r.get("success"):
        return JSONResponse(r, status_code=400)
    return r


@app.get("/api/skills/pending")
def api_skill_pending():
    return evolve.skills.list_pending()


@app.post("/api/skills/pending/{pid}/approve")
def api_skill_approve(pid: str):
    return evolve.skills.decide_pending(pid, True)


@app.post("/api/skills/pending/{pid}/reject")
def api_skill_reject(pid: str):
    return evolve.skills.decide_pending(pid, False)


@app.get("/api/skills/history")
def api_skills_history_all(limit: int = 100):
    return evolve.skills.history(None, min(limit, 1000))


@app.get("/api/skills/{name}")
def api_skill_view(name: str):
    return evolve.skills.view(name, bump_view=False)


@app.get("/api/skills/{name}/file")
def api_skill_file(name: str, path: str):
    return {"path": path, "content": evolve.skills.read_file(name, path)}


@app.get("/api/skills/{name}/history")
def api_skill_history(name: str, limit: int = 100):
    return evolve.skills.history(name, min(limit, 1000))


@app.post("/api/skills/{name}/rollback")
def api_skill_rollback(name: str, req: SkillRollback):
    if not req.confirm:
        raise core.ConfirmRequiredError("回滚技能需要 confirm=true")
    return evolve.skills.rollback(name, req.rev, "undo" if req.mode == "undo" else "to")


@app.post("/api/skills/{name}/feedback")
def api_skill_feedback(name: str, req: SkillFeedback):
    return evolve.skills.feedback(name, req.good, req.comment)


@app.post("/api/skills/{name}/pin")
def api_skill_pin(name: str, pinned: bool = True):
    return evolve.skills.set_pinned(name, pinned)


@app.get("/api/evolve/status")
def api_evolve_status():
    return evolve.review.status()


@app.post("/api/evolve/review")
async def api_evolve_review(req: ReviewReq):
    if not maintenance.llm_ready():
        return {"ok": False, "skipped": True, "reason": "未配置 LLM API Key，无法巡检"}
    if req.background:
        return {"ok": True, "started": evolve.review.spawn_review(reason="面板手动巡检", focus=req.focus)}
    return await run_in_threadpool(evolve.review.review, "面板手动巡检", req.focus, None, True)


# ---------------- 星星 ----------------
@app.get("/api/stars")
def get_stars(limit: int = 50, offset: int = 0, include_deleted: bool = False, tag: Optional[str] = None):
    return stars.list_stars(min(limit, 1000), offset, include_deleted, tag)


@app.post("/api/stars")
def post_star(s: StarIn):
    return stars.add_star(s.content, s.summary, s.tags, s.importance, s.source, s.session, s.created_at, s.merge_near)


@app.get("/api/stars/map")
def get_map():
    return stars.star_map()


class FuseReq(BaseModel):
    ids: list[int] = []             # 指定要融合的星；留空 = 自动找相似组
    use_llm: bool = True
    max_groups: int = 5


@app.get("/api/stars/fuse/preview")
def fuse_preview():
    """看看哪些星会被融合（不改动任何东西）。"""
    return {"groups": fusion.find_groups()}


@app.post("/api/stars/fuse")
async def fuse_stars(req: FuseReq):
    cfg = config.load_config()
    try:
        if req.ids:
            st = await run_in_threadpool(fusion.fuse, req.ids, req.use_llm, cfg)
            return {"fused": [{"members": sorted(set(req.ids)), "star": st}]}
        groups = (await run_in_threadpool(fusion.find_groups, cfg))[: max(1, min(20, req.max_groups))]
        done = []
        for g in groups:
            try:
                done.append({"members": g["ids"], "star": await run_in_threadpool(fusion.fuse, g["ids"], req.use_llm, cfg)})
            except ValueError:
                continue
        return {"fused": done}
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/stars/{sid}/unfuse")
def unfuse_star(sid: int):
    try:
        return fusion.unfuse(sid)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/stars/{sid}")
def get_star(sid: int):
    s = stars.get_star(sid)
    if not s:
        raise HTTPException(404, "星星不存在")
    s["raw"] = stars._raw_for(s.get("source", ""), 2000)
    if s.get("fused_from"):
        s["members"] = fusion.members(sid)
    return s


@app.patch("/api/stars/{sid}")
def patch_star(sid: int, p: StarPatch):
    return stars.update_star(sid, **p.model_dump())


@app.post("/api/stars/{sid}/brighten")
def brighten(sid: int):
    return stars.adjust_boost(sid, 1.25)


@app.post("/api/stars/{sid}/dim")
def dim(sid: int):
    return stars.adjust_boost(sid, 0.8)


@app.post("/api/stars/{sid}/delete")
def soft_delete(sid: int):
    return stars.soft_delete(sid)


@app.post("/api/stars/{sid}/restore")
def restore_star(sid: int):
    return stars.soft_delete(sid, restore=True)


@app.get("/api/search")
def api_search(q: str, limit: int = 5000):
    return stars.search(q, limit)


# ---------------- 分层召回 ----------------
@app.get("/api/recall/index")
def api_recall_index():
    return stars.recall_index()


@app.post("/api/recall")
def api_recall(r: RecallReq):
    return stars.recall(r.query, r.budget_tokens, r.bump)


@app.get("/api/recall/detail/{sid}")
def api_recall_detail(sid: int):
    d = stars.recall_detail(sid)
    if not d:
        raise HTTPException(404, "星星不存在")
    return d


# ---------------- 归档与维护 ----------------
@app.post("/api/archive")
def post_archive(a: ArchiveIn):
    return {"file": archive.append_messages(a.messages, a.session, a.source)}


def _consolidate_job():
    with jobs.run("maintenance", "整理归档对话") as j:
        r = maintenance.consolidate()
        j["detail"] = r.get("reason") or f"新增 {r.get('added', 0)}，合并 {r.get('merged', 0)}，提案 {r.get('proposals', 0)}"
        if r.get("errors"):
            j["status"] = "failed"
        return r


@app.post("/api/maintenance/consolidate")
async def api_consolidate(background: bool = True):
    if not maintenance.llm_ready():
        return {"ok": False, "skipped": True, "reason": "未配置 LLM API Key，跳过整理（其他功能不受影响）"}
    if background:
        import threading as _th
        _th.Thread(target=_consolidate_job, daemon=True).start()
        return {"ok": True, "started": True}
    return await run_in_threadpool(_consolidate_job)


@app.get("/api/maintenance/status")
def api_maint_status():
    st = config.load_state()
    return {"llm_ready": maintenance.llm_ready(), "pending": {k: v[1] - v[0] for k, v in maintenance.pending_work().items()},
            "last": st.get("last_consolidate"), "last_snapshot": st.get("last_snapshot"), "last_backup": st.get("last_backup")}


# ---------------- 设置 ----------------
@app.get("/api/settings")
def get_settings():
    return config.masked_config()


# ---------------- 模型服务（服务商 / 同步模型 / 检测） ----------------
def _prov_args(body: dict) -> tuple[str, str, str]:
    """前端没带 Key（或是掩码）时，用已保存的那一份。"""
    cfg = config.load_config()
    p = providers.find(cfg, body.get("id", "")) or {}
    if not p and body.get("id") == "legacy":
        p = {"api_key": cfg["llm"].get("api_key", ""), "base_url": cfg["llm"].get("base_url", "")}
    key = body.get("api_key") or ""
    if not key or key.startswith("****"):
        key = p.get("api_key", "")
    base = body.get("base_url") or p.get("base_url", "")
    return base, key, body.get("protocol") or p.get("protocol", "openai")


@app.get("/api/providers/presets")
def providers_presets():
    return providers.PRESETS


@app.post("/api/providers/models")
def providers_models(body: dict):
    base, key, proto = _prov_args(body or {})
    try:
        return providers.list_models(base, key, proto)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.post("/api/providers/check")
def providers_check(body: dict):
    body = body or {}
    if body.get("role"):   # 检测某个默认模型
        r = providers.resolve(config.load_config(), body["role"])
        if not r:
            raise HTTPException(400, "还没有选择这个默认模型，或它所在的服务商已关闭")
        base, key, proto, model = r["base_url"], r["api_key"], r["protocol"], r["model"]
    else:
        base, key, proto = _prov_args(body)
        model = body.get("model", "")
    try:
        res = providers.check(base, key, proto, model, body.get("kind", ""))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    log_event("provider_check", model=model or None, ok=True)
    return res


@app.put("/api/settings")
def put_settings(patch: dict):
    patch = dict(patch or {})
    for k in ("secrets_encrypted", "secrets_salt", "secrets_unlocked"):
        patch.pop(k, None)
    for sec, key in config.SECRET_FIELDS:  # 掩码值 / 未填写 → 保持原值
        if isinstance(patch.get(sec), dict):
            patch[sec].pop(key + "_set", None)
            v = patch[sec].get(key)
            if v is None or (isinstance(v, str) and v.startswith("****")):
                patch[sec].pop(key, None)
    before = semantic.active(config.load_config())
    config.update_config(patch)
    log_event("settings_update", keys=sorted(patch.keys()))
    if any(k in patch for k in ("embed", "memory", "defaults", "providers")):   # 换了向量档位 / 接口：重新加载并在后台补齐
        semantic.reset()
        after = semantic.active(config.load_config())
        if after != before:
            log_event("embed_switch", before=before[1] if before else None, after=after[1] if after else None)
        semantic.start_backfill()
    return config.masked_config()


@app.post("/api/secrets/encrypt")
def secrets_encrypt(p: Passphrase):
    config.enable_secret_encryption(p.passphrase)
    log_event("secrets_encrypt_on")
    return config.masked_config()


@app.post("/api/secrets/unlock")
def secrets_unlock(p: Passphrase):
    config.set_master_passphrase(p.passphrase)
    if not config.secrets_unlocked():
        config.set_master_passphrase(None)
        raise HTTPException(403, "主密码不正确")
    return config.masked_config()


@app.post("/api/secrets/decrypt")
def secrets_decrypt():
    config.disable_secret_encryption()
    log_event("secrets_encrypt_off")
    return config.masked_config()


# ---------------- 快照 / WebDAV ----------------
@app.post("/api/sync/snapshot")
async def api_snapshot():
    return await run_in_threadpool(sync.local_snapshot)


@app.get("/api/sync/snapshots")
def api_snapshots():
    return sync.list_local()


@app.post("/api/sync/backup")
async def api_backup():
    try:
        return await run_in_threadpool(sync.webdav_backup)
    except Exception as e:
        raise HTTPException(502, f"WebDAV 备份失败：{e}")


@app.get("/api/sync/remote")
async def api_remote():
    try:
        return await run_in_threadpool(sync.webdav_list)
    except Exception as e:
        raise HTTPException(502, f"WebDAV 列表失败：{e}")


@app.post("/api/sync/restore")
async def api_restore(r: RestoreReq):
    if not r.confirm:
        raise core.ConfirmRequiredError("恢复会覆盖当前灵魂包（会先自动做一次本地快照），需要 confirm=true")
    try:
        if r.source == "remote":
            return await run_in_threadpool(sync.webdav_restore, r.name, r.passphrase)
        return await run_in_threadpool(sync.restore_local, r.name, r.passphrase or "")
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"恢复失败：{e}")


# ---------------- 导入 ----------------
@app.post("/api/import/path")
async def api_import_path(req: ImportPath):
    try:
        return await run_in_threadpool(importers.import_path, req.path, req.kind)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.post("/api/import/upload")
async def api_import_upload(file: UploadFile = File(...), kind: str = Form("auto")):
    data = await file.read()
    return await run_in_threadpool(importers.import_bytes, data, file.filename or "upload.txt", kind)


# ---------------- 宇宙之眼（仅主人） ----------------
class EyeStart(BaseModel):
    title: str = ""
    source: str = "screen"          # screen / tab / camera
    engine: Optional[str] = None


class EyeNote(BaseModel):
    kind: str = "scene"             # subtitle / scene / note
    text: str


class EyeEnd(BaseModel):
    make_star: Optional[bool] = None


class EyeAsk(BaseModel):
    session: str
    text: str = ""
    frames: list[str] = []
    mode: str = "user"              # user = 主人开口 / 打字；auto = 切场景时主动吐槽
    engine: Optional[str] = None


class EyeEngineReq(BaseModel):
    engine: Optional[str] = None


def _eye_owner(request: Request) -> dict:
    if not is_owner(request):
        raise HTTPException(403, "宇宙之眼只对主人开放（本机访问，或带上主人令牌）")
    cfg = config.load_config()
    if not (cfg.get("eye") or {}).get("enabled", True):
        raise HTTPException(403, "宇宙之眼已在设置里关闭")
    return cfg


def _eye_session(sid: str) -> dict:
    try:
        return eye.session.get(sid, with_entries=False)
    except eye.SessionNotFound:
        raise HTTPException(404, "找不到这次观看")


@app.get("/api/eye/status")
def eye_status(request: Request):
    cfg = _eye_owner(request)
    e = config.masked_config().get("eye", {})
    engines = {}
    for k, title in eye.ENGINES.items():
        engines[k] = {**eye.engine.public_view(eye.resolve(cfg, k)), "title": title}
    pre = config.load_state().get("eye_preflight") or {}
    return {"enabled": True, "engine": e.get("engine", "frame"), "engines": engines, "settings": e,
            "usage": eye.session.usage_today(), "preflight": pre, "pace_gap": eye.session.PACE_GAP,
            "sessions": eye.session.list_sessions(8)}


@app.post("/api/eye/preflight")
async def eye_preflight(req: EyeEngineReq, request: Request):
    cfg = _eye_owner(request)
    r = eye.resolve(cfg, req.engine)
    res = await eye.preflight(r)
    key = f"{r['engine']}|{r['protocol']}|{r['base_url']}|{r['model']}"
    pre = config.load_state().get("eye_preflight") or {}
    pre[key] = res
    config.update_state(eye_preflight=pre)
    log_event("eye_preflight", engine=r["engine"], model=r["model"], ok=res["ok"])
    return {**res, "key": key, "engine": r["engine"], "model": r["model"]}


@app.post("/api/eye/session/start")
def eye_start(req: EyeStart, request: Request):
    cfg = _eye_owner(request)
    return eye.session.start(req.title, req.source, req.engine or cfg["eye"].get("engine", "frame"))


@app.get("/api/eye/sessions")
def eye_sessions(request: Request, limit: int = 30):
    _eye_owner(request)
    return eye.session.list_sessions(min(200, limit))


@app.get("/api/eye/session/{sid}")
def eye_session_get(sid: str, request: Request):
    _eye_owner(request)
    _eye_session(sid)
    d = eye.session.get(sid)
    if any(e.get("speaker") for e in d["entries"]):  # 宇宙之声：台词标上现在的称呼
        try:
            reg = voice.registry.Registry.load(sid)
            for e in d["entries"]:
                if e.get("speaker"):
                    e["who"] = reg.display(e["speaker"])
        except Exception:  # noqa: BLE001
            pass
    return d


@app.post("/api/eye/session/{sid}/note")
def eye_note(sid: str, req: EyeNote, request: Request):
    _eye_owner(request)
    _eye_session(sid)
    if req.kind not in ("subtitle", "scene", "note"):
        raise HTTPException(400, "kind 只能是 subtitle / scene / note")
    return eye.session.add(sid, req.kind, req.text)


@app.post("/api/eye/session/{sid}/end")
async def eye_end(sid: str, req: EyeEnd, request: Request):
    cfg = _eye_owner(request)
    meta = _eye_session(sid)
    make = cfg["eye"].get("star_on_end", True) if req.make_star is None else req.make_star
    summary = None
    if make and not meta.get("ended_at") and maintenance.llm_ready(cfg):
        msgs = eye.session.summary_messages(sid)
        if msgs:
            try:
                summary = await run_in_threadpool(maintenance.chat, msgs, cfg)
            except Exception as ex:  # 总结失败就用朴素版
                log.warning("宇宙之眼总结失败：%s", ex)
    vend = {"saved": 0, "cast": ""}
    if not meta.get("ended_at"):
        try:
            vend = await run_in_threadpool(voice.pipeline.end_session, cfg, sid)
        except Exception as ex:  # noqa: BLE001
            log.warning("宇宙之声收尾失败：%s", ex)
    d = eye.session.end(sid, make_star=make, llm_summary=summary, cast=vend.get("cast", ""))
    d["voice"] = vend
    return d


@app.post("/api/eye/transcribe")
async def eye_transcribe(request: Request, file: UploadFile = File(...), session: str = Form(""),
                         kind: str = Form("subtitle")):
    """把一段音频（电影声音或主人的话）交给 OpenAI 兼容转写服务。"""
    cfg = _eye_owner(request)
    e = cfg["eye"]
    base = (e.get("stt_base_url") or cfg["llm"].get("base_url") or "").rstrip("/")
    key = e.get("stt_api_key") or cfg["llm"].get("api_key") or ""
    if not base or not key:
        raise HTTPException(400, "还没配置转写服务（宇宙之眼设置 → 声音）")
    data = await file.read()
    import httpx
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.post(base + "/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                          data={"model": e.get("stt_model") or "whisper-1", "language": "zh"},
                          files={"file": (file.filename or "audio.webm", data, file.content_type or "audio/webm")})
    if r.status_code >= 400:
        raise HTTPException(502, f"转写服务出错：HTTP {r.status_code} {r.text[:200]}")
    text = (r.json().get("text") or "").strip()
    if text and session and kind in ("subtitle", "user", "note"):
        _eye_session(session)
        if kind != "user":
            eye.session.add(session, kind, text)
    return {"text": text}


def _sse(obj: dict) -> str:
    import json as _j
    return "data: " + _j.dumps(obj, ensure_ascii=False) + "\n\n"


@app.post("/api/eye/ask")
async def eye_ask(req: EyeAsk, request: Request):
    cfg = _eye_owner(request)
    e = cfg["eye"]
    meta = _eye_session(req.session)
    if meta.get("ended_at"):
        raise HTTPException(400, "这次观看已经结束")
    r = eye.resolve(cfg, req.engine or meta.get("engine") or None)
    if not r["ready"]:
        raise HTTPException(400, f"「{eye.ENGINES[r['engine']]}」还没配置好：需要地址、Key 和模型")
    text = (req.text or "").strip()
    if req.mode not in ("user", "auto"):
        raise HTTPException(400, "mode 只能是 user / auto")
    if req.mode == "user" and not text:
        raise HTTPException(400, "要问点什么呀")
    if req.mode == "auto":
        ok, wait = eye.session.auto_allowed(req.session, e.get("pace", "normal"))
        if not ok:
            return {"skipped": True, "wait": round(wait, 1)}
    try:
        eye.session.check_budget(cfg)
    except eye.BudgetError as ex:
        raise HTTPException(429, str(ex))
    frames = [f for f in req.frames if isinstance(f, str)][: max(1, int(e.get("max_frames", 6)))]
    history = eye.session.history_messages(req.session, int(e.get("history_turns", 12)))
    ctx = eye.session.recent_context(req.session)
    if req.mode == "user":
        eye.session.add(req.session, "user", text)
    system = eye.session.system_prompt(cfg, req.session, text, req.mode)
    vrule = voice.pipeline.prompt_rule(req.session) if cfg.get("voice", {}).get("visual_naming", True) else ""
    if vrule:
        system += "\n\n" + vrule
    prompt = (f"最近的台词 / 场景：\n{ctx}\n\n" if ctx else "") + (text or "（画面刚切换。）")
    if e.get("save_frames") and frames:
        d = config.soul_path("eye", "frames", req.session)
        d.mkdir(parents=True, exist_ok=True)
        import base64 as _b
        for i, f in enumerate(frames):
            try:
                (d / f"{int(time.time() * 1000)}-{i}.jpg").write_bytes(_b.b64decode(f.split(",", 1)[-1]))
            except Exception:
                pass

    async def gen():
        yield _sse({"type": "start", "engine": r["engine"], "model": r["model"], "frames": len(frames), "mode": req.mode})
        out, usage, fin = "", {}, {}
        tf = voice.naming.TagFilter()
        async for p in eye.engine.stream(r, system, history, prompt, frames):
            if p["type"] == "delta":
                out += p["text"]
                shown = tf.feed(p["text"])
                if shown:
                    yield _sse({**p, "text": shown})
                continue
            elif p["type"] == "done":
                fin = p
                usage = p.get("usage") or {}
                continue
            yield _sse(p)
        rest = tf.flush()
        if rest:
            yield _sse({"type": "delta", "text": rest})
        out, tags = voice.naming.parse_tags(out)
        named = []
        if tags:
            try:
                named = await run_in_threadpool(voice.pipeline.apply_visual_tags, cfg, req.session, tags)
            except Exception as ex:  # noqa: BLE001
                log.warning("宇宙之声认人失败：%s", ex)
            if named:
                yield _sse({"type": "names", "changes": named})
        reply = out.strip() or "…"
        try:
            eye.session.add(req.session, "ai", reply, mode=req.mode, first_ms=fin.get("first_ms"))
        except Exception:
            pass
        tokens = int(usage.get("input", 0)) + int(usage.get("output", 0))
        if not tokens:  # 上游没报用量 → 粗估
            tokens = len(frames) * 800 + (len(system) + len(prompt) + len(out)) // 2
        u = eye.session.record_usage(tokens)
        yield _sse({"type": "done", "text": reply, "silent": reply.strip("…. ") == "", "usage": usage,
                    "tokens": tokens, "today": u, "first_ms": fin.get("first_ms"), "total_ms": fin.get("total_ms")})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 宇宙之声（声纹插件） ----------------
class VoiceInstall(BaseModel):
    deps: bool = True
    models: bool = True


class VoiceName(BaseModel):
    name: str = ""
    action: str = "set"             # set = 主人指定 / confirm = 确认当前提名 / reject = 否决


class VoiceMerge(BaseModel):
    keep: str
    drop: str


def _voice_cfg(request: Request) -> dict:
    return _eye_owner(request)


def _voice_err(ex: Exception):
    if isinstance(ex, (voice.VoiceError, eye.SessionNotFound)):
        raise HTTPException(404 if isinstance(ex, eye.SessionNotFound) else 400, str(ex) or "找不到这次观看")
    if isinstance(ex, voice.backend.BackendError):
        raise HTTPException(409, str(ex))
    raise ex


# ---------------- 向量模型 ----------------
class EmbedInstall(BaseModel):
    kind: str = "small"
    deps: bool = True


@app.get("/api/embed/status")
def embed_status():
    return semantic.status()


@app.post("/api/embed/install")
def embed_install(req: EmbedInstall):
    if req.kind not in embed_models.LOCAL:
        raise HTTPException(400, "只有 small / m3 需要下载")
    jobs = embed_models.install(req.kind, (config.load_config().get("embed") or {}), deps=req.deps)
    log_event("embed_install", kind=req.kind)
    return {"jobs": jobs}


@app.delete("/api/embed/models/{kind}")
def embed_remove(kind: str):
    if kind not in embed_models.LOCAL:
        raise HTTPException(404, "没有这个模型")
    try:
        return {"removed": embed_models.uninstall(kind)}
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/embed/test")
def embed_test():
    try:
        return semantic.test_api(config.load_config())
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, str(ex)[:300])


@app.post("/api/embed/backfill")
def embed_backfill():
    semantic.reset()
    semantic.start_backfill()
    return semantic.status()


@app.get("/api/voice/status")
def voice_status(request: Request):
    cfg = _voice_cfg(request)
    v = config.masked_config().get("voice", {})
    st = voice.models.status(voice.backend.name(cfg.get("voice") or {}))
    return {**st, "settings": v, "enabled": bool(v.get("enabled")), "library": voice.registry.library_public(),
            "stt_ready": voice.pipeline.stt_ready(cfg) and (cfg.get("eye") or {}).get("stt_mode") == "api",
            "llm_ready": maintenance.llm_ready(cfg)}


@app.post("/api/voice/install")
def voice_install(req: VoiceInstall, request: Request):
    cfg = _voice_cfg(request)
    jobs = voice.models.install(cfg.get("voice") or {}, deps=req.deps, models=req.models)
    log_event("voice_install", deps=req.deps, models=req.models)
    return {"jobs": jobs}


@app.delete("/api/voice/models")
def voice_models_remove(request: Request):
    _voice_cfg(request)
    return {"removed": voice.models.uninstall_models()}


@app.post("/api/voice/session/{sid}/audio")
async def voice_audio(sid: str, request: Request, sr: int = 16000, t0: float = 0):
    """浏览器送来一段 16 位单声道 PCM（或 WAV）。"""
    cfg = _voice_cfg(request)
    _eye_session(sid)
    data = await request.body()
    if not data:
        raise HTTPException(400, "没有收到声音")
    if voice.audio.wav_header_ok(data):
        try:
            x, sr = await run_in_threadpool(voice.audio.wav_to_float, data)
            import numpy as _n
            data = (_n.clip(x, -1, 1) * 32767).astype(_n.int16).tobytes()
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(400, f"WAV 读不了：{ex}")
    try:
        return await run_in_threadpool(voice.pipeline.process_chunk, cfg, sid, data, sr, t0 or None)
    except Exception as ex:  # noqa: BLE001
        _voice_err(ex)


@app.get("/api/voice/session/{sid}/speakers")
def voice_speakers(sid: str, request: Request):
    cfg = _voice_cfg(request)
    _eye_session(sid)
    return voice.pipeline.registry(sid, cfg).public()


@app.post("/api/voice/session/{sid}/speakers/{spk}")
def voice_speaker_name(sid: str, spk: str, req: VoiceName, request: Request):
    cfg = _voice_cfg(request)
    _eye_session(sid)
    try:
        with voice.pipeline._slock(sid):
            reg = voice.pipeline.registry(sid, cfg)
            s = reg.resolve(spk)
            if not s:
                raise voice.VoiceError("找不到这个人")
            if req.action == "reject":
                reg.reject(spk, req.name or None)
            elif req.action == "confirm":
                best = req.name or (reg.best_proposal(s) or {}).get("name") or s.get("name")
                if not best:
                    raise voice.VoiceError("还没有可以确认的名字")
                reg.set_name(spk, best)
            elif req.action == "set":
                reg.set_name(spk, req.name)
            else:
                raise voice.VoiceError("action 只能是 set / confirm / reject")
            reg.save()
            return reg.public()
    except Exception as ex:  # noqa: BLE001
        _voice_err(ex)


@app.post("/api/voice/session/{sid}/merge")
def voice_merge(sid: str, req: VoiceMerge, request: Request):
    cfg = _voice_cfg(request)
    _eye_session(sid)
    try:
        with voice.pipeline._slock(sid):
            reg = voice.pipeline.registry(sid, cfg)
            reg.merge(req.keep, req.drop)
            reg.save()
            return reg.public()
    except Exception as ex:  # noqa: BLE001
        _voice_err(ex)


@app.post("/api/voice/session/{sid}/recluster")
def voice_recluster(sid: str, request: Request):
    cfg = _voice_cfg(request)
    _eye_session(sid)
    with voice.pipeline._slock(sid):
        reg = voice.pipeline.registry(sid, cfg)
        merges = reg.recluster(cfg.get("voice") or {})
        reg.save()
        return {**reg.public(), "merges": merges}


@app.post("/api/voice/session/{sid}/identify")
async def voice_identify(sid: str, request: Request):
    cfg = _voice_cfg(request)
    _eye_session(sid)
    if not maintenance.llm_ready(cfg):
        raise HTTPException(400, "认名字需要先在「设置 → LLM 维护」里配好模型")
    try:
        changes = await run_in_threadpool(voice.pipeline.identify, cfg, sid)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"认名字失败：{ex}")
    return {"changes": changes, **voice.pipeline.registry(sid, cfg).public()}


@app.post("/api/voice/owner/enroll")
async def voice_owner_enroll(request: Request, sr: int = 16000):
    cfg = _voice_cfg(request)
    data = await request.body()
    try:
        return await run_in_threadpool(voice.pipeline.enroll_owner, cfg, data, sr)
    except Exception as ex:  # noqa: BLE001
        _voice_err(ex)


@app.delete("/api/voice/owner")
def voice_owner_clear(request: Request):
    _voice_cfg(request)
    voice.registry.clear_owner()
    return voice.registry.library_public()


@app.get("/api/voice/library")
def voice_library(request: Request):
    _voice_cfg(request)
    return voice.registry.library_public()


@app.delete("/api/voice/library/{key}")
def voice_library_delete(key: str, request: Request):
    _voice_cfg(request)
    if not voice.registry.library_delete(key):
        raise HTTPException(404, "声纹库里没有这一部")
    return voice.registry.library_public()


# ---------------- 对话监控 / 任务 / 对话抽屉 / 星系整理（v0.11.0） ----------------
def _need_owner(request: Request) -> None:
    if not is_owner(request):
        raise HTTPException(403, "只有主人可以这样做")


def _watch_status() -> dict:
    from .watch import adapters, hooks, watcher
    cfg = config.load_config()
    w = cfg.get("watch") or {}
    wt = watcher.get()
    st = wt.public()
    clients = []
    for cid, meta in adapters.CLIENTS.items():
        cc = (w.get("clients") or {}).get(cid) or {}
        rts = adapters.roots(cid, cc)
        clients.append({"id": cid, "title": meta["title"], "enabled": bool(cc.get("enabled")), "hook": bool(cc.get("hook")),
                        "hook_installed": hooks.installed(cid), "hook_target": hooks.target(cid), "path": cc.get("path", ""),
                        "roots": [str(r) for r in rts], "exists": any(r.exists() for r in rts),
                        "experimental": bool(meta.get("experimental")), "verified": meta.get("verified", ""),
                        "hook_command": hooks.command_str(cid),
                        "stats": st["clients"].get(cid) or {"files": sum(1 for v in wt.state.files.values() if v.get("c") == cid)}})
    return {"enabled": bool(w.get("enabled", True)), "llm_ready": maintenance.llm_ready(cfg), "status": st, "clients": clients}


@app.get("/api/watch/status")
def watch_status():
    return _watch_status()


@app.post("/api/watch/notify")
def watch_notify(body: dict):
    """客户端「本轮结束」提醒（钩子 / --notify 调用）。只会去读该客户端自己目录下的记录文件。"""
    from .watch import watcher
    body = body or {}
    client = str(body.get("client") or "")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {k: v for k, v in body.items() if k != "client"}
    return watcher.get().notify(client, payload)


@app.post("/api/watch/process")
async def watch_process(request: Request):
    _need_owner(request)
    from .watch import watcher
    return await run_in_threadpool(watcher.get().process_now)


@app.post("/api/watch/client/{cid}")
def watch_client(cid: str, body: dict, request: Request):
    """开关某客户端的监控 / 安装或卸载钩子 / 路径覆盖。"""
    _need_owner(request)
    from .watch import adapters, hooks
    if cid not in adapters.CLIENTS:
        raise HTTPException(404, "不认识的客户端")
    body = body or {}
    patch: dict = {}
    out: dict = {}
    if "enabled" in body:
        patch["enabled"] = bool(body["enabled"])
    if "path" in body:
        patch["path"] = str(body["path"] or "").strip()
    if "hook" in body:
        want = bool(body["hook"])
        r = hooks.install(cid) if want else hooks.uninstall(cid)
        out["hook_result"] = r
        if r.get("manual"):
            want = False
        patch["hook"] = want and hooks.installed(cid)
        log_event("watch_hook", client=cid, install=want, path=r.get("path"))
    if patch:
        config.update_config({"watch": {"clients": {cid: patch}}})
    if "watch_enabled" in body:
        config.update_config({"watch": {"enabled": bool(body["watch_enabled"])}})
    return {**_watch_status(), **out}


@app.get("/api/tasks")
def api_tasks(limit: int = 60):
    from .watch import store
    return {"jobs": jobs.list_jobs(limit), "client_tasks": store.list_tasks(limit), "counts": store.counts()}


@app.get("/api/tasks/client/{tid}")
def api_task_get(tid: int):
    from .watch import store
    t = store.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.post("/api/tasks/client/{tid}")
async def api_task_status(tid: int, body: dict, request: Request):
    _need_owner(request)
    from .watch import extract
    return await run_in_threadpool(extract.set_status, tid, str((body or {}).get("status") or ""))


@app.post("/api/agent/chat")
async def api_agent_chat(body: dict, request: Request):
    _need_owner(request)
    try:
        return await run_in_threadpool(agent.chat, str((body or {}).get("text") or ""))
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reply": f"出错了：{e}", "actions": []}


def _agent_status(cfg: dict, request: Request) -> dict:
    from . import context, upstream

    llm = cfg["llm"]
    ready = context.main_ready(cfg)
    configured = providers.resolve(cfg, "chat")
    return {"model": llm["model"] if ready else (configured or {}).get("model", ""), "model_ready": ready,
            "authorized": is_owner(request), "owner_token_required": bool(cfg["core"].get("owner_token")),
            "window": context.window(cfg), "compact_at": context.compact_threshold(cfg),
            "effort": upstream.norm_effort(llm.get("agent_effort")),
            "efforts": [[k, upstream.EFFORT_LABELS[k]] for k in upstream.EFFORTS]}


@app.get("/api/agent/status")
def api_agent_status(request: Request):
    return _agent_status(config.load_config(), request)


@app.get("/api/agent/history")
def api_agent_history(request: Request):
    _need_owner(request)
    from . import context
    h = agent.load_history()
    return {**_agent_status(config.load_config(), request), "history": h[-200:], "tokens": context.total_tokens(h)}


@app.delete("/api/agent/history")
def api_agent_history_clear(request: Request):
    _need_owner(request)
    agent.clear_history()
    return {"ok": True}


@app.post("/api/organize")
async def api_organize(request: Request, background: bool = True):
    _need_owner(request)
    if background:
        return {"ok": True, "queued": organize.schedule(delay=0.1)}
    return await run_in_threadpool(organize.run_job, "整理星系（手动）")


@app.get("/api/organize/categories")
def api_categories():
    return {"categories": organize.categories(), "last": config.load_state().get("last_organize")}


@app.post("/api/stars/{sid}/category")
def api_star_category(sid: int, body: dict):
    organize.set_category(sid, str((body or {}).get("major") or ""), str((body or {}).get("minor") or ""))
    return stars.get_star(sid)


# ---------------- 数据：目录 / 缓存 / 重置 / 备份与恢复 / 增量同步（v0.11.0） ----------------
@app.get("/api/sync/delta")
def sync_delta_get(request: Request, since: float = 0):
    """云服务器接口：导出水位线之后的变化（主人令牌）。"""
    _need_owner(request)
    return datasync.export_delta(since)


@app.post("/api/sync/delta")
def sync_delta_post(body: dict, request: Request):
    """云服务器接口：合并另一台设备推来的变化（主人令牌）。核心记忆只会变成提案。"""
    _need_owner(request)
    return datasync.import_delta(body, source="cloud-push")


@app.get("/api/data/info")
def data_info():
    return datasync.info()


@app.post("/api/data/open")
def data_open(body: dict, request: Request):
    _need_owner(request)
    return {"path": datasync.open_folder(str((body or {}).get("which") or "soul"))}


@app.post("/api/data/clean_logs")
def data_clean_logs(request: Request):
    _need_owner(request)
    return {"removed": datasync.clean_logs()}


@app.post("/api/data/clear_cache")
def data_clear_cache(request: Request):
    _need_owner(request)
    return {"freed": datasync.clear_cache()}


@app.post("/api/data/reset")
def data_reset(body: dict, request: Request):
    _need_owner(request)
    if (body or {}).get("confirm") != "重置":
        raise HTTPException(428, "请输入「重置」确认")
    return datasync.reset_data()


@app.get("/api/data/backups")
async def data_backups(target: str = "local"):
    if target == "local":
        return {"items": datasync.local_list(), "dir": str(datasync.local_dir())}
    if target == "webdav":
        try:
            return {"items": await run_in_threadpool(sync.webdav_list)}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
    raise HTTPException(400, "未知的存储")


@app.post("/api/data/backup")
async def data_backup(body: dict, request: Request):
    _need_owner(request)
    target = str((body or {}).get("target") or "local")
    fn, title = {"local": (datasync.local_backup, "本地备份"), "webdav": (sync.webdav_backup, "WebDAV 完整备份"),
                 "webdav_delta": (datasync.webdav_incremental, "WebDAV 增量备份"),
                 "cloud": (datasync.cloud_sync, "云服务器同步")}.get(target, (None, ""))
    if not fn:
        raise HTTPException(400, "未知的存储")

    def go():
        with jobs.run("backup", title + "（手动）") as j:
            r = fn()
            j["detail"] = json.dumps(r, ensure_ascii=False)[:500]
            return r
    try:
        return await run_in_threadpool(go)
    except (ValueError, FileNotFoundError, PermissionError):
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.post("/api/data/restore")
async def data_restore(body: dict, request: Request):
    _need_owner(request)
    body = body or {}
    target, name, pw = str(body.get("target") or "local"), str(body.get("name") or ""), str(body.get("passphrase") or "")
    if not body.get("confirm"):
        raise HTTPException(428, "恢复会覆盖当前数据（会先自动做一次快照），请确认")
    try:
        if target == "local":
            return await run_in_threadpool(datasync.local_restore, name, pw)
        if target == "webdav":
            return await run_in_threadpool(sync.webdav_restore, name, pw or None)
        if target == "webdav_delta":
            return await run_in_threadpool(datasync.webdav_pull_deltas)
        if target == "cloud":
            return await run_in_threadpool(datasync.cloud_pull)
    except (ValueError, FileNotFoundError):
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    raise HTTPException(400, "未知的存储")


@app.post("/api/data/cloud/check")
async def data_cloud_check(body: dict):
    try:
        return await run_in_threadpool(datasync.cloud_check, str((body or {}).get("url") or ""), str((body or {}).get("token") or ""))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"连不上云服务器：{e}")


# ---------------- 静态面板 ----------------
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(config.WEB_DIR / "favicon.ico")


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/skills")
def skills_page():
    """技能进化模块页面。"""
    return FileResponse(config.WEB_DIR / "skills.html")


@app.get("/eye")
def eye_page():
    """宇宙之眼模块页面。"""
    return FileResponse(config.WEB_DIR / "eye.html")


@app.get("/core")
def core_page():
    """核心记忆模块独立页面。"""
    return FileResponse(config.WEB_DIR / "core.html")


app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")
