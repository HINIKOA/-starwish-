"""快照与 WebDAV 备份。

- 本地快照：soul/snapshots/starwish-时间.zip（不含 config.json 与 snapshots 本身）。
- WebDAV：直接用 requests 发 MKCOL / PUT / PROPFIND / GET / DELETE。
- 加密：设置了备份口令时，用 PBKDF2(SHA256, 39 万次) 派生密钥 + Fernet 加密整个 zip（.zip.enc）；
  未设置口令则上传明文 zip 并给出警告。
"""
from __future__ import annotations

import base64
import io
import os
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

from . import config
from .db import log_event

MAGIC = b"SSENC1"
EXCLUDE_TOP = {"snapshots", "config.json", "config.json.tmp", "soul.db", "soul.db-wal", "soul.db-shm", "cache"}


def _derive(passphrase: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8"))))


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(16)
    return MAGIC + salt + _derive(passphrase, salt).encrypt(data)


def decrypt_bytes(data: bytes, passphrase: str) -> bytes:
    if not data.startswith(MAGIC):
        return data
    salt = data[len(MAGIC):len(MAGIC) + 16]
    return _derive(passphrase, salt).decrypt(data[len(MAGIC) + 16:])


def build_zip_bytes() -> bytes:
    """打包灵魂包。数据库用 SQLite 在线备份 API 拷贝，保证一致性。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if config.DB_PATH.exists():
            tmp = config.SNAPSHOT_DIR / f".db-{os.getpid()}-{time.time_ns()}.tmp"
            src = sqlite3.connect(str(config.DB_PATH))
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            z.write(tmp, "soul.db")
            tmp.unlink(missing_ok=True)
        for p in sorted(config.SOUL_DIR.rglob("*")):
            rel = p.relative_to(config.SOUL_DIR)
            if not p.is_file() or rel.parts[0] in EXCLUDE_TOP or p.suffix == ".tmp":
                continue
            z.write(p, rel.as_posix())
        z.writestr("STARWISH_SNAPSHOT.txt", f"星愿灵魂包快照\n创建时间: {datetime.now().isoformat()}\n")
    return buf.getvalue()


def _snap_name(enc: bool) -> str:
    return f"starwish-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip" + (".enc" if enc else "")


def local_snapshot(keep: int | None = None) -> dict:
    config.ensure_dirs()
    data = build_zip_bytes()
    name = _snap_name(False)
    p = config.SNAPSHOT_DIR / name
    i = 1
    while p.exists():
        p = config.SNAPSHOT_DIR / name.replace(".zip", f"-{i}.zip")
        i += 1
    p.write_bytes(data)
    keep = keep or int(config.load_config()["schedules"].get("keep_local_snapshots", 10))
    snaps = list_local()
    for old in snaps[keep:]:
        (config.SNAPSHOT_DIR / old["name"]).unlink(missing_ok=True)
    config.update_state(last_snapshot=time.time())
    log_event("snapshot_local", name=p.name, size=len(data))
    return {"name": p.name, "size": len(data)}


def list_local() -> list[dict]:
    if not config.SNAPSHOT_DIR.exists():
        return []
    items = [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
             for p in config.SNAPSHOT_DIR.glob("starwish-*.zip*")]
    return sorted(items, key=lambda d: d["name"], reverse=True)


def restore_bytes(data: bytes, passphrase: str = "") -> dict:
    """用快照内容覆盖当前灵魂包（先自动做一次本地快照，保证可回退）。"""
    if data.startswith(MAGIC):
        if not passphrase:
            raise ValueError("该备份已加密，需要备份口令")
        data = decrypt_bytes(data, passphrase)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    if "soul.db" not in names and "core.json" not in names:
        raise ValueError("不是有效的星愿快照")
    safety = local_snapshot()
    for n in names:
        target = (config.SOUL_DIR / n).resolve()
        if config.SOUL_DIR not in target.parents:
            continue
        if n == "soul.db":
            for ext in ("-wal", "-shm"):
                Path(str(config.DB_PATH) + ext).unlink(missing_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(n) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    log_event("restore", files=len(names), safety_snapshot=safety["name"])
    return {"ok": True, "files": len(names), "safety_snapshot": safety["name"]}


def restore_local(name: str, passphrase: str = "") -> dict:
    p = (config.SNAPSHOT_DIR / name).resolve()
    if config.SNAPSHOT_DIR.resolve() not in p.parents or not p.exists():
        raise FileNotFoundError("快照不存在")
    return restore_bytes(p.read_bytes(), passphrase)


# ---------------- WebDAV ----------------

class WebDAV:
    def __init__(self, url: str, user: str = "", password: str = "", remote_dir: str = "starwish"):
        if not url:
            raise ValueError("未配置 WebDAV 地址")
        self.base = url.rstrip("/") + "/"
        self.dir = remote_dir.strip("/")
        self.auth = (user, password) if user else None

    def _u(self, name: str = "") -> str:
        path = "/".join(quote(p) for p in [self.dir, name] if p)
        return self.base + path + ("" if name else "/")

    def _req(self, method, url, **kw):
        r = requests.request(method, url, auth=self.auth, timeout=kw.pop("timeout", 60), **kw)
        return r

    def ensure_dir(self) -> None:
        parts = [p for p in self.dir.split("/") if p]
        cur = self.base
        for p in parts:
            cur = cur + quote(p) + "/"
            r = self._req("PROPFIND", cur, headers={"Depth": "0"})
            if r.status_code == 404:
                r2 = self._req("MKCOL", cur)
                if r2.status_code not in (200, 201, 405):
                    raise RuntimeError(f"MKCOL 失败: HTTP {r2.status_code}")
            elif r.status_code >= 400 and r.status_code not in (405,):
                raise RuntimeError(f"PROPFIND 失败: HTTP {r.status_code}")

    def put(self, name: str, data: bytes) -> None:
        r = self._req("PUT", self._u(name), data=data, timeout=300)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"上传失败: HTTP {r.status_code}")

    def get(self, name: str) -> bytes:
        r = self._req("GET", self._u(name), timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"下载失败: HTTP {r.status_code}")
        return r.content

    def delete(self, name: str) -> None:
        self._req("DELETE", self._u(name))

    def list(self) -> list[dict]:
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/>'
                '<d:getlastmodified/></d:prop></d:propfind>')
        r = self._req("PROPFIND", self._u(), headers={"Depth": "1", "Content-Type": "application/xml"}, data=body)
        if r.status_code == 404:
            return []
        if r.status_code >= 400:
            raise RuntimeError(f"列目录失败: HTTP {r.status_code}")
        ns = {"d": "DAV:"}
        out = []
        root = ET.fromstring(r.content)
        for resp in root.findall("d:response", ns):
            href = unquote(resp.findtext("d:href", default="", namespaces=ns))
            name = href.rstrip("/").split("/")[-1]
            if not name.startswith("starwish-"):
                continue
            size = resp.findtext(".//d:getcontentlength", default="0", namespaces=ns)
            out.append({"name": name, "size": int(size or 0)})
        return sorted(out, key=lambda d: d["name"], reverse=True)


def _dav(cfg) -> WebDAV:
    w = cfg["webdav"]
    return WebDAV(w["url"], w.get("user", ""), w.get("password", ""), w.get("remote_dir", "starwish"))


def webdav_backup() -> dict:
    cfg = config.load_config()
    dav = _dav(cfg)
    pw = cfg["webdav"].get("passphrase", "")
    data = build_zip_bytes()
    warning = ""
    if pw:
        data = encrypt_bytes(data, pw)
    else:
        warning = "未设置备份口令：本次以明文 zip 上传，建议在设置里填写备份口令。"
    name = _snap_name(bool(pw))
    dav.ensure_dir()
    dav.put(name, data)
    keep = int(cfg["webdav"].get("keep_versions", 10))
    removed = []
    for old in dav.list()[keep:]:
        dav.delete(old["name"])
        removed.append(old["name"])
    config.update_state(last_backup=time.time())
    log_event("backup_webdav", name=name, size=len(data), encrypted=bool(pw), removed=removed)
    return {"ok": True, "name": name, "size": len(data), "encrypted": bool(pw), "warning": warning, "removed": removed}


def webdav_list() -> list[dict]:
    return _dav(config.load_config()).list()


def webdav_restore(name: str, passphrase: str | None = None) -> dict:
    cfg = config.load_config()
    data = _dav(cfg).get(name)
    return restore_bytes(data, passphrase if passphrase is not None else cfg["webdav"].get("passphrase", ""))
