"""星魂 SoulStar —— FastAPI 主程序：REST API + 网页面板 + OpenAI 兼容代理 + 定时任务。"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import __version__, archive, config, core, importers, maintenance, stars, sync
from .db import init_db, log_event, read_events
from .proxy import router as proxy_router

log = logging.getLogger("soulstar")


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
                await run_in_threadpool(maintenance.consolidate)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    init_db()
    core.load_core()
    log_event("server_start", version=__version__)
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="星魂 SoulStar", version=__version__, lifespan=lifespan)
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


class LockReq(BaseModel):
    locked: bool
    confirm: bool = False


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
    return {"ok": True, "app": "soulstar", "version": __version__, "soul_dir": str(config.SOUL_DIR)}


@app.get("/api/stats")
def api_stats():
    return stars.stats()


@app.get("/api/events")
def api_events(limit: int = 100):
    return read_events(min(1000, limit))


# ---------------- 核心记忆 ----------------
@app.get("/api/core")
def get_core():
    c = core.load_core()
    return {**c, "text": core.core_text(c), "tokens": core.core_tokens(), "section_titles": core.SECTIONS}


@app.put("/api/core")
def put_core(req: CoreEdit):
    c = core.set_section(req.section, req.content, req.confirm, mode=req.mode)
    return {**c, "text": core.core_text(c)}


@app.post("/api/core/lock")
def lock_core(req: LockReq):
    return core.set_lock(req.locked, req.confirm)


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


@app.get("/api/stars/{sid}")
def get_star(sid: int):
    s = stars.get_star(sid)
    if not s:
        raise HTTPException(404, "星星不存在")
    s["raw"] = stars._raw_for(s.get("source", ""), 2000)
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


@app.post("/api/maintenance/consolidate")
async def api_consolidate(background: bool = True):
    if not maintenance.llm_ready():
        return {"ok": False, "skipped": True, "reason": "未配置 LLM API Key，跳过整理（其他功能不受影响）"}
    if background:
        maintenance.consolidate_async()
        return {"ok": True, "started": True}
    return await run_in_threadpool(maintenance.consolidate)


@app.get("/api/maintenance/status")
def api_maint_status():
    st = config.load_state()
    return {"llm_ready": maintenance.llm_ready(), "pending": {k: v[1] - v[0] for k, v in maintenance.pending_work().items()},
            "last": st.get("last_consolidate"), "last_snapshot": st.get("last_snapshot"), "last_backup": st.get("last_backup")}


# ---------------- 设置 ----------------
@app.get("/api/settings")
def get_settings():
    return config.masked_config()


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
    config.update_config(patch)
    log_event("settings_update", keys=sorted(patch.keys()))
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


# ---------------- 静态面板 ----------------
@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")
