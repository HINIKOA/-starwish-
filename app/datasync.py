"""数据：增量同步 + 本地 / WebDAV / 云服务器 三种存储 + 每天 03:00 定时（错过了下次启动补上）+ 日志清理 / 缓存 / 重置。

增量：每颗星有全局唯一的 uid；「变化时间」= max(创建, 修改, 隐藏)。水位线（上次成功同步到的时间）记在 state.json，
只传水位线之后新增 / 改过 / 隐藏的星，外加核心记忆（很小，每次都带）。
  WebDAV   每次上传一个 deltas/delta-时间.json[.enc]（设了备份口令就加密）；恢复时按顺序合并全部增量。
  云服务器 你自己服务器上的另一个星愿：POST /api/sync/delta 推送、GET /api/sync/delta?since= 拉取（主人令牌）。
  本地     完整快照 zip 写到指定文件夹。
合并规则：按 uid 匹配，较新的覆盖较旧的；没有的就新建（核心记忆按版本号，较新的覆盖）。
已知局限：融合关系（哪颗并进了哪颗）不跨设备同步，融合星本身会同步。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

from . import config, core, stars, sync
from .db import get_conn, log_event

CST = timezone(timedelta(hours=8))     # Asia/Shanghai（无夏令时；不依赖 tzdata，Windows 也准）
CHANGED = "MAX(created_at, COALESCE(updated_at, 0), COALESCE(deleted_at, 0))"
FIELDS = ("uid", "content", "summary", "tags", "created_at", "updated_at", "importance", "boost", "source", "session",
          "deleted_at", "generation", "cat_major", "cat_minor", "recall_count", "last_recalled_at")


# ---------------- 增量导出 / 合并 ----------------
def ensure_uids() -> int:
    with get_conn() as c:
        rows = c.execute("SELECT id FROM stars WHERE uid=''").fetchall()
        for r in rows:
            c.execute("UPDATE stars SET uid=? WHERE id=?", (uuid.uuid4().hex, r["id"]))
    return len(rows)


def export_delta(since: float = 0) -> dict:
    """水位线之后新增 / 修改 / 隐藏的星（不含被融合吸收的成员）+ 核心记忆。"""
    ensure_uids()
    with get_conn() as c:
        rows = c.execute(f"SELECT *, {CHANGED} AS changed FROM stars WHERE {CHANGED} > ? AND fused_into IS NULL "
                         "ORDER BY changed", (float(since or 0),)).fetchall()
    items = [{k: r[k] for k in FIELDS} for r in rows]
    until = max([float(r["changed"]) for r in rows] + [float(since or 0)])
    cr = core.load_core()
    return {"format": "starwish-delta-1", "since": float(since or 0), "until": until, "exported_at": time.time(),
            "stars": items, "core": {"rev": cr.get("rev"), "sections": core.sections_of(cr),
                                       "updated_at": cr.get("updated_at")}}


def _changed(d: dict) -> float:
    return max(float(d.get("created_at") or 0), float(d.get("updated_at") or 0), float(d.get("deleted_at") or 0))


def import_delta(delta: dict, source: str = "sync") -> dict:
    if not isinstance(delta, dict) or delta.get("format") != "starwish-delta-1":
        raise ValueError("不是星愿的增量数据")
    added = updated = skipped = 0
    for d in delta.get("stars") or []:
        uid = str(d.get("uid") or "")
        if not uid or not str(d.get("content") or "").strip():
            skipped += 1
            continue
        with get_conn() as c:
            r = c.execute(f"SELECT id, {CHANGED} AS changed FROM stars WHERE uid=?", (uid,)).fetchone()
            tags = d.get("tags") if isinstance(d.get("tags"), str) else json.dumps(d.get("tags") or [], ensure_ascii=False)
            if r is None:
                cur = c.execute("INSERT INTO stars(uid, content, summary, tags, created_at, updated_at, importance, boost, source, session, "
                                "deleted_at, generation, cat_major, cat_minor, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (uid, d["content"], d.get("summary") or "", tags, float(d.get("created_at") or time.time()),
                                 d.get("updated_at"), int(d.get("importance") or 3), float(d.get("boost") or 1.0),
                                 d.get("source") or source, d.get("session") or "", d.get("deleted_at"), int(d.get("generation") or 0),
                                 d.get("cat_major") or "", d.get("cat_minor") or "", stars._hash(d["content"])))
                sid = cur.lastrowid
                added += 1
            elif _changed(d) > float(r["changed"] or 0):
                sid = r["id"]
                c.execute("UPDATE stars SET content=?, summary=?, tags=?, updated_at=?, importance=?, boost=?, deleted_at=?, "
                          "cat_major=?, cat_minor=?, hash=? WHERE id=?",
                          (d["content"], d.get("summary") or "", tags, d.get("updated_at"), int(d.get("importance") or 3),
                           float(d.get("boost") or 1.0), d.get("deleted_at"), d.get("cat_major") or "", d.get("cat_minor") or "",
                           stars._hash(d["content"]), sid))
                updated += 1
            else:
                skipped += 1
                continue
            stars._index(c, sid, d["content"], d.get("summary") or "", json.loads(tags))
    core_res = None
    cd = delta.get("core") or {}
    if cd.get("sections") and int(cd.get("rev") or 0) > int(core.load_core().get("rev") or 0):
        # 走「提案」：自动同步通道不直接改核心记忆
        for sec, content in cd["sections"].items():
            if content and content.strip() != str(core.sections_of(core.load_core()).get(core.norm_section(sec)) or "").strip():
                try:
                    core.create_proposal(sec, content, reason="来自同步：另一台设备上的核心记忆较新", source=source, mode="replace")
                    core_res = "proposed"
                except Exception:
                    pass
    log_event("sync_import", source=source, added=added, updated=updated, skipped=skipped)
    return {"added": added, "updated": updated, "skipped": skipped, "core": core_res}


def _encode(delta: dict, passphrase: str) -> tuple[bytes, str]:
    raw = json.dumps(delta, ensure_ascii=False).encode("utf-8")
    if passphrase:
        return sync.encrypt_bytes(raw, passphrase), ".json.enc"
    return raw, ".json"


def _decode(data: bytes, passphrase: str) -> dict:
    if data.startswith(sync.MAGIC):
        if not passphrase:
            raise ValueError("增量数据已加密，需要备份口令")
        data = sync.decrypt_bytes(data, passphrase)
    return json.loads(data.decode("utf-8"))


# ---------------- WebDAV 增量 ----------------
class DeltaDAV(sync.WebDAV):
    def list(self) -> list[dict]:   # 增量目录里的文件名是 delta-*
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/></d:prop></d:propfind>')
        r = self._req("PROPFIND", self._u(), headers={"Depth": "1", "Content-Type": "application/xml"}, data=body)
        if r.status_code == 404:
            return []
        if r.status_code >= 400:
            raise RuntimeError(f"列目录失败: HTTP {r.status_code}")
        import xml.etree.ElementTree as ET
        from urllib.parse import unquote
        ns = {"d": "DAV:"}
        out = []
        for resp in ET.fromstring(r.content).findall("d:response", ns):
            name = unquote(resp.findtext("d:href", default="", namespaces=ns)).rstrip("/").split("/")[-1]
            if name.startswith("delta-"):
                out.append({"name": name})
        return sorted(out, key=lambda d: d["name"])


def _delta_dav(cfg) -> DeltaDAV:
    w = cfg["webdav"]
    return DeltaDAV(w["url"], w.get("user", ""), w.get("password", ""), (w.get("remote_dir") or "starwish").strip("/") + "/deltas")


def webdav_incremental() -> dict:
    """只上传水位线之后的变化；没有变化就不上传。成功后才推进水位线。"""
    cfg = config.load_config()
    if not cfg["webdav"].get("url"):
        raise ValueError("未配置 WebDAV 地址")
    st = config.load_state()
    since = float(st.get("webdav_delta_since") or 0)
    delta = export_delta(since)
    if not delta["stars"] and st.get("webdav_core_rev") == delta["core"]["rev"]:
        config.update_state(last_webdav_delta=time.time())
        return {"ok": True, "uploaded": False, "stars": 0, "reason": "没有新的变化"}
    pw = cfg["webdav"].get("passphrase", "")
    data, ext = _encode(delta, pw)
    name = f"delta-{datetime.now(CST).strftime('%Y%m%dT%H%M%S')}-{int(delta['until'])}{ext}"
    dav = _delta_dav(cfg)
    dav.ensure_dir()
    dav.put(name, data)
    config.update_state(webdav_delta_since=delta["until"], webdav_core_rev=delta["core"]["rev"], last_webdav_delta=time.time())
    log_event("backup_webdav_delta", name=name, stars=len(delta["stars"]), encrypted=bool(pw))
    return {"ok": True, "uploaded": True, "name": name, "stars": len(delta["stars"]), "encrypted": bool(pw),
            "warning": "" if pw else "未设置备份口令：增量以明文上传"}


def webdav_pull_deltas() -> dict:
    cfg = config.load_config()
    dav = _delta_dav(cfg)
    pw = cfg["webdav"].get("passphrase", "")
    tot = {"files": 0, "added": 0, "updated": 0}
    for f in dav.list():
        r = import_delta(_decode(dav.get(f["name"]), pw), source="webdav")
        tot["files"] += 1
        tot["added"] += r["added"]
        tot["updated"] += r["updated"]
    return tot


# ---------------- 云服务器（自己服务器上的星愿） ----------------
def _cloud(cfg) -> tuple[str, dict]:
    d = cfg.get("data") or {}
    url = str(d.get("cloud_url") or "").rstrip("/")
    if not url:
        raise ValueError("未配置云服务器地址")
    h = {"Content-Type": "application/json"}
    if d.get("cloud_token"):
        h["Authorization"] = "Bearer " + d["cloud_token"]
    return url, h


def cloud_push(timeout: float = 60) -> dict:
    cfg = config.load_config()
    url, h = _cloud(cfg)
    since = float(config.load_state().get("cloud_push_since") or 0)
    delta = export_delta(since)
    r = requests.post(url + "/api/sync/delta", data=json.dumps(delta, ensure_ascii=False).encode("utf-8"), headers=h, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"推送失败：HTTP {r.status_code} {r.text[:200]}")
    config.update_state(cloud_push_since=delta["until"], last_cloud_push=time.time())
    return {"ok": True, "pushed": len(delta["stars"]), "remote": r.json()}


def cloud_pull(timeout: float = 60) -> dict:
    cfg = config.load_config()
    url, h = _cloud(cfg)
    since = float(config.load_state().get("cloud_pull_since") or 0)
    r = requests.get(url + "/api/sync/delta", params={"since": since}, headers=h, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"拉取失败：HTTP {r.status_code} {r.text[:200]}")
    delta = r.json()
    res = import_delta(delta, source="cloud")
    # 拉回来的星会让本机的「变化时间」变新——推送水位线不必为它们再推一次
    config.update_state(cloud_pull_since=float(delta.get("until") or since), last_cloud_pull=time.time())
    return {"ok": True, **res}


def cloud_sync() -> dict:
    return {"push": cloud_push(), "pull": cloud_pull()}


def cloud_check(url: str = "", token: str = "") -> dict:
    cfg = config.load_config()
    url = (url or (cfg.get("data") or {}).get("cloud_url") or "").rstrip("/")
    token = token if token and not token.startswith("****") else (cfg.get("data") or {}).get("cloud_token", "")
    r = requests.get(url + "/api/health", timeout=10)
    h = r.json()
    if h.get("app") != "starwish":
        raise RuntimeError("这个地址不是星愿")
    r2 = requests.get(url + "/api/sync/delta", params={"since": time.time() + 3600},
                      headers={"Authorization": "Bearer " + token} if token else {}, timeout=10)
    if r2.status_code != 200:
        raise RuntimeError("主人令牌不对，或云服务器没有设置主人令牌（远程访问必须设置）")
    return {"ok": True, "version": h.get("version")}


# ---------------- 本地备份 ----------------
def local_dir() -> "os.PathLike":
    from pathlib import Path
    d = str((config.load_config().get("data") or {}).get("local_dir") or "").strip()
    return Path(os.path.expandvars(d)).expanduser() if d else config.SNAPSHOT_DIR


def local_backup() -> dict:
    d = local_dir()
    if d == config.SNAPSHOT_DIR:
        return sync.local_snapshot()
    d.mkdir(parents=True, exist_ok=True)
    pw = config.load_config()["webdav"].get("passphrase", "")
    data = sync.build_zip_bytes()
    if pw:
        data = sync.encrypt_bytes(data, pw)
    name = sync._snap_name(bool(pw))
    (d / name).write_bytes(data)
    log_event("backup_local_dir", dir=str(d), name=name, size=len(data))
    return {"name": name, "size": len(data), "dir": str(d)}


def local_list() -> list[dict]:
    d = local_dir()
    if not d.exists():
        return []
    return sorted(({"name": p.name, "size": p.stat().st_size} for p in d.glob("starwish-*.zip*")), key=lambda x: x["name"], reverse=True)


def local_restore(name: str, passphrase: str = "") -> dict:
    d = local_dir()
    p = (d / name).resolve()
    if p.parent != d.resolve() or not p.exists():
        raise FileNotFoundError("备份不存在")
    return sync.restore_bytes(p.read_bytes(), passphrase or config.load_config()["webdav"].get("passphrase", ""))


# ---------------- 定时：每天 03:00（北京时间），错过了补上 ----------------
def last_boundary(now: float, hour: int = 3) -> float:
    """最近一个已经过去的 hour:00（北京时间）的时间戳。"""
    dt = datetime.fromtimestamp(now, CST)
    b = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    if b > dt:
        b -= timedelta(days=1)
    return b.timestamp()


def daily_due(last_run: float | None, now: float, hour: int = 3) -> bool:
    """上次成功运行早于最近一个 03:00 → 该跑了（软件关着错过的，下次启动时也会是 True）。"""
    return float(last_run or 0) < last_boundary(now, hour)


def run_daily(now: float | None = None, catch_up: bool = False) -> list[dict]:
    """检查并运行到期的每日任务（WebDAV 增量 / 云服务器同步）。每次运行都记到任务抽屉。"""
    from . import jobs
    now = now or time.time()
    cfg = config.load_config()
    d = cfg.get("data") or {}
    hour = int(d.get("webdav_hour", 3) or 3)
    st = config.load_state()
    out = []
    tasks = []
    if d.get("webdav_daily", True) and cfg["webdav"].get("url"):
        tasks.append(("daily_webdav_t", "WebDAV 增量备份", webdav_incremental))
    if d.get("cloud_daily") and d.get("cloud_url"):
        tasks.append(("daily_cloud_t", "云服务器同步", cloud_sync))
    for key, title, fn in tasks:
        if not daily_due(st.get(key), now, hour):
            continue
        jid = jobs.start("backup", title + ("（补上错过的 03:00）" if catch_up or now - last_boundary(now, hour) > 3600 else "（每天 03:00）"))
        try:
            r = fn()
            config.update_state(**{key: now})
            jobs.finish(jid, "done", json.dumps(r, ensure_ascii=False)[:500])
            out.append({"task": title, "ok": True, "result": r})
        except Exception as e:  # noqa: BLE001  —— 失败不推进，下一分钟 / 下次启动再试
            jobs.finish(jid, "failed", str(e)[:500])
            config.update_state(**{key + "_fail": now})
            out.append({"task": title, "ok": False, "error": str(e)[:300]})
    return out


# ---------------- 日志 / 缓存 / 重置 ----------------
def logs_dir():
    return config.soul_path("logs")


def clean_logs(days: int | None = None, now: float | None = None) -> int:
    days = int((config.load_config().get("data") or {}).get("log_days", 30) if days is None else days)
    if days <= 0:
        return 0
    now = now or time.time()
    n = 0
    d = logs_dir()
    if d.exists():
        for p in d.rglob("*"):
            if p.is_file() and now - p.stat().st_mtime > days * 86400:
                p.unlink(missing_ok=True)
                n += 1
    config.update_state(last_log_clean=now)
    return n


def cache_dirs() -> list:
    s = config.SOUL_DIR
    out = [s / "cache"]
    for base in (s / ".webview", s / ".appwindow"):
        if base.exists():
            out += [p for p in base.rglob("*") if p.is_dir() and p.name in ("Cache", "Code Cache", "GPUCache", "DawnCache", "GrShaderCache")]
    return out


def _size(p) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def cache_size() -> int:
    return sum(_size(p) for p in cache_dirs())


def clear_cache() -> int:
    freed = 0
    for p in cache_dirs():
        freed += _size(p)
        shutil.rmtree(p, ignore_errors=True)
    log_event("cache_clear", bytes=freed)
    return freed


def reset_data() -> dict:
    """重置：先做一次本地快照（可恢复），再清空星星、提案、向量、任务与核心记忆。设置保留。"""
    snap = sync.local_snapshot()
    with get_conn() as c:
        for t in ("stars", "proposals", "embeddings"):
            c.execute(f"DELETE FROM {t}")
        for t in ("jobs", "watch_tasks", "watch_fragments"):
            try:
                c.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        try:
            c.execute("DELETE FROM stars_fts")
        except Exception:
            pass
    for f in (config.CORE_JSON, config.CORE_MD):
        f.unlink(missing_ok=True)
    for f in ("agent/chat.json", "agent/organizer.json"):
        config.soul_path(f).unlink(missing_ok=True)
    core.load_core()
    log_event("data_reset", safety_snapshot=snap["name"])
    return {"ok": True, "safety_snapshot": snap["name"]}


def info() -> dict:
    cfg = config.load_config()
    st = config.load_state()
    return {"soul_dir": str(config.SOUL_DIR), "logs_dir": str(logs_dir()), "cache_bytes": cache_size(),
            "log_days": int((cfg.get("data") or {}).get("log_days", 30)), "local_dir": str(local_dir()),
            "last": {k: st.get(k) for k in ("last_webdav_delta", "daily_webdav_t", "daily_webdav_t_fail", "last_cloud_push",
                                              "last_cloud_pull", "daily_cloud_t", "last_backup", "last_snapshot", "last_log_clean")},
            "next_daily": last_boundary(time.time(), int((cfg.get("data") or {}).get("webdav_hour", 3) or 3)) + 86400}


def open_folder(which: str) -> str:
    p = {"soul": config.SOUL_DIR, "logs": logs_dir(), "local": local_dir()}.get(which)
    if p is None:
        raise ValueError("未知目录")
    p.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            import subprocess
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return str(p)
