"""监控线程：只 stat（mtime / 大小）轮询，几秒一次；单个工作线程排队处理；偏移量落盘。

- 第一次启用某客户端：已有的对话文件从末尾开始（不回灌历史，省钱）；之后新出现的文件从头读。
- 关着软件时聊的：下次启动从保存的偏移量接着读，不丢。
- 一轮的结束：出现下一条用户消息 / Codex task_complete / 收到 notify / 文件静止超过 idle_sec。
- 没配整理模型：不推进偏移量，配好后自动补上。单轮连续失败 3 次跳过。
关窗即停：线程都是守护线程，服务停止时 stop()。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from .. import config, jobs, maintenance
from ..db import log_event
from . import adapters, extract, opencode
from .adapters import CLIENTS
from .state import State, inbox_path
from .turns import Turn, is_trivial

log = logging.getLogger("starwish.watch")
MAX_FAILS = 3


class Watcher:
    def __init__(self):
        self.state = State()
        self.q: queue.Queue = queue.Queue()
        self.queued: set = set()
        self.delayed: dict = {}          # key -> (到期时间, force, hint)
        self.stop_ev = threading.Event()
        self.lock = threading.RLock()    # 同一时刻只处理一个文件
        self.threads: list[threading.Thread] = []
        self.last_rescan: dict = {}
        self.status = {"running": False, "last_run": None, "last_error": "", "turns": 0, "skipped": 0,
                       "waiting": "", "clients": {}, "started_at": None}

    # ---------------- 配置 ----------------
    @staticmethod
    def wcfg(cfg: dict | None = None) -> dict:
        return (cfg or config.load_config()).get("watch") or {}

    def enabled_clients(self, cfg: dict | None = None) -> list[str]:
        w = self.wcfg(cfg)
        if not w.get("enabled", True):
            return []
        cl = w.get("clients") or {}
        return [c for c in CLIENTS if (cl.get(c) or {}).get("enabled", False)]

    def _cst(self, c: str) -> dict:
        return self.status["clients"].setdefault(c, {"files": 0, "turns": 0, "last_turn": None, "last_error": ""})

    # ---------------- 线程 ----------------
    def start(self) -> None:
        if self.status["running"]:
            return
        self.stop_ev.clear()
        self.status.update(running=True, started_at=time.time())
        self.replay_inbox()
        for name, fn in (("starwish-watch-poll", self._poll_loop), ("starwish-watch-work", self._work_loop)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self.stop_ev.set()
        self.status["running"] = False
        for t in self.threads:
            t.join(timeout=3)
        self.threads = []
        try:
            self.state.save()
        except Exception:
            pass

    def _poll_loop(self):
        while not self.stop_ev.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001
                self.status["last_error"] = f"轮询：{e}"
                log.warning("watch poll: %s", e)
            w = self.wcfg()
            self.stop_ev.wait(max(1.0, float(w.get("poll_sec") or 3)))

    def _work_loop(self):
        while not self.stop_ev.is_set():
            try:
                item = self.q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.handle(item)
            except Exception as e:  # noqa: BLE001
                self.status["last_error"] = str(e)[:300]
                log.warning("watch work: %s", e)

    def enqueue(self, kind: str, key: str, force: bool = False, hint: str = "", delay: float = 0) -> None:
        if delay > 0:
            self.delayed[key] = (time.time() + delay, kind, force, hint)
            return
        k = (kind, key)
        if k in self.queued and not force:
            return
        self.queued.add(k)
        self.q.put((kind, key, force, hint))

    def drain(self) -> int:
        """同步处理队列里的全部（测试 / 立即归纳用）。"""
        n = 0
        self._release_delayed(force_all=True)
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return n
            n += self.handle(item) or 0

    def handle(self, item) -> int:
        kind, key, force, hint = item
        if kind != "turn":
            self.queued.discard((kind, key))
        if kind == "file":
            return self.process_file(key, force, hint)
        if kind == "db":
            return self.process_db(key, force)
        if kind == "turn":
            return self._process_turns([key], None)
        return 0

    def _release_delayed(self, force_all: bool = False):
        now = time.time()
        for key, (due, kind, force, hint) in list(self.delayed.items()):
            if force_all or due <= now:
                self.delayed.pop(key, None)
                self.enqueue(kind, key, force, hint)

    # ---------------- 轮询 ----------------
    def poll_once(self, now: float | None = None) -> None:
        now = now or time.time()
        cfg = config.load_config()
        w = self.wcfg(cfg)
        self._release_delayed()
        hot = float(w.get("hot_days") or 3) * 86400
        idle = float(w.get("idle_sec") or 45)
        rescan = float(w.get("rescan_sec") or 60)
        for c in self.enabled_clients(cfg):
            ccfg = (w.get("clients") or {}).get(c) or {}
            if CLIENTS[c]["kind"] == "opencode":
                self._poll_db(c, ccfg, w, now)
                continue
            if c not in self.state.clients or now - self.last_rescan.get(c, 0) >= rescan:
                self.rescan(c, ccfg, w, now)
            for key, fs in list(self.state.files.items()):
                if fs.get("c") != c:
                    continue
                if now - float(fs.get("m") or 0) > hot and not fs.get("p") and fs.get("s", -1) >= 0:
                    continue          # 冷文件：只在重新扫描目录时看一眼
                try:
                    st = os.stat(key)
                except OSError:
                    continue
                self._check(key, fs, st.st_size, st.st_mtime, now, idle)

    def rescan(self, c: str, ccfg: dict, w: dict, now: float) -> int:
        self.last_rescan[c] = now
        init = c in self.state.clients
        backfill = bool(w.get("backfill_existing"))
        hot = float(w.get("hot_days") or 3) * 86400
        found = adapters.discover(c, ccfg, float(w.get("hot_days") or 3))
        new = 0
        idle = float(w.get("idle_sec") or 45)
        for p, mt, sz in found:
            key = str(p)
            fs = self.state.files.get(key)
            if fs is not None:
                if now - float(fs.get("m") or 0) > hot:   # 冷文件在这里补查
                    self._check(key, fs, sz, mt, now, idle)
                continue
            if init or (backfill and now - mt <= hot):
                self.state.files[key] = {"c": c, "o": 0, "s": -1, "m": 0, "meta": {}, "p": False, "f": 0}
            else:   # 第一次启用：已有的对话从末尾开始，不回灌历史
                self.state.files[key] = {"c": c, "o": sz, "s": sz, "m": mt, "meta": {}, "p": False, "f": 0}
            new += 1
        if not init:
            self.state.clients[c] = {"init": now}
        self._cst(c)["files"] = sum(1 for v in self.state.files.values() if v.get("c") == c)
        if new or not init:
            self.state.save()
        return new

    def _check(self, key: str, fs: dict, size: int, mtime: float, now: float, idle: float) -> None:
        if size != fs.get("s") or mtime != fs.get("m"):
            self.enqueue("file", key)
        elif fs.get("p") and now - mtime >= idle and now >= float(fs.get("retry") or 0):
            self.enqueue("file", key, force=True)

    def _poll_db(self, c: str, ccfg: dict, w: dict, now: float) -> None:
        for db in adapters.roots(c, ccfg):
            key = str(db)
            try:
                st = db.stat()
            except OSError:
                continue
            sig = [st.st_mtime, st.st_size]
            for ext in ("-wal",):
                try:
                    s2 = os.stat(key + ext)
                    sig += [s2.st_mtime, s2.st_size]
                except OSError:
                    pass
            ds = self.state.dbs.get(key)
            if ds is None:
                back = bool(w.get("backfill_existing"))
                since = int((now - float(w.get("hot_days") or 3) * 86400) * 1000) if back else int(now * 1000)
                ds = self.state.dbs[key] = {"c": c, "since": since, "sess": {}, "sig": None, "p": False}
                self.state.clients.setdefault(c, {"init": now})
                self.state.save()
            idle = float(w.get("idle_sec") or 45)
            if sig != ds.get("sig"):
                ds["lc"] = now
                self.enqueue("db", key)
            elif ds.get("p") and now - float(ds.get("lc") or 0) >= idle and now >= float(ds.get("retry") or 0):
                self.enqueue("db", key, force=True)

    # ---------------- 处理 ----------------
    def _llm_ok(self, cfg) -> bool:
        ok = maintenance.llm_ready(cfg)
        self.status["waiting"] = "" if ok else "还没有配置整理模型（设置 → 默认模型），对话先记着偏移，配好后自动补上"
        return ok

    def process_file(self, key: str, force: bool = False, hint: str = "") -> int:
        with self.lock:
            fs = self.state.files.get(key)
            if not fs:
                return 0
            cfg = config.load_config()
            c = fs["c"]
            path = Path(key)
            try:
                st = path.stat()
            except OSError:
                return 0
            if not self._llm_ok(cfg):
                fs["p"] = True
                fs["retry"] = time.time() + 60
                fs["s"], fs["m"] = st.st_size, st.st_mtime
                return 0
            off = int(fs.get("o") or 0)
            if CLIENTS[c]["kind"] == "dsh":
                data = adapters.read_dsh_bytes(path)
                if data is None:
                    fs["s"], fs["m"] = st.st_size, st.st_mtime
                    return 0
                if off > len(data):
                    off = 0
                chunk = data[off:]
            else:
                if st.st_size < off:      # 文件被改写 / 截断：从头再来
                    off = 0
                with open(path, "rb") as f:
                    f.seek(off)
                    chunk = f.read()
            meta = fs.setdefault("meta", {})
            if not force and time.time() - st.st_mtime >= float(self.wcfg(cfg).get("idle_sec") or 45):
                force = True     # 文件早就不动了（如关着软件时聊的）：最后一轮也算说完了
            turns, safe = adapters.parse(c, chunk, off, meta, path)
            if force and turns and hint and not turns[-1].turn_complete:
                h = hint.strip()
                if h and h[-40:] not in turns[-1].assistant_text:
                    turns[-1].add_assistant(h)
            n = self._process_turns(turns, fs, force=force, end_safe=safe)
            fs["s"], fs["m"] = st.st_size, st.st_mtime
            self.state.save()
            return n

    def _process_turns(self, turns: list[Turn], fs: dict | None, force: bool = False, end_safe: int | None = None,
                       on_done=None) -> int:
        """逐轮抽取；每处理完一轮就推进偏移量并落盘（中途断电也不重复）。返回处理的非琐碎轮数。"""
        cfg = config.load_config()
        w = self.wcfg(cfg)
        min_chars = int(w.get("min_turn_chars") or 12)
        done = 0
        job = None
        totals = {"stars": 0, "proposals": 0, "fragments": 0, "closed": 0, "failed": 0}
        pending = False
        stopped = False
        if fs is not None:
            fs.pop("stopped", None)
        for t in turns:
            if not t.turn_complete and not force:
                pending = True
                break
            if is_trivial(t, min_chars):
                self.status["skipped"] += 1
            else:
                if job is None:
                    job = jobs.start("watch", f"归纳 {CLIENTS.get(t.client, {}).get('title', t.client)} 的对话")
                try:
                    r = extract.process_turn(t, cfg)
                    for k in totals:
                        totals[k] += r.get(k, 0)
                    done += 1
                    if fs is not None:
                        fs["f"] = 0
                    cs = self._cst(t.client)
                    cs["turns"] += 1
                    cs["last_turn"] = time.time()
                    self.status["turns"] += 1
                except Exception as e:  # noqa: BLE001
                    msg = f"{type(e).__name__}: {e}"[:300]
                    self._cst(t.client)["last_error"] = msg
                    self.status["last_error"] = msg
                    log_event("watch_error", client=t.client, error=msg)
                    if fs is not None:
                        fs["f"] = int(fs.get("f") or 0) + 1
                        if fs["f"] < MAX_FAILS:
                            fs["p"] = True
                            fs["retry"] = time.time() + 60
                            stopped = True
                            break
                        fs["f"] = 0       # 连续失败：跳过这一轮
            if fs is not None and "o" in fs:
                fs["o"] = t.end
                self.state.save()
            if on_done:
                on_done(t)
        if fs is not None and "o" in fs and not stopped:
            if not pending and end_safe is not None:
                fs["o"] = max(int(fs.get("o") or 0), int(end_safe))
            fs["p"] = pending
        if fs is not None and stopped:
            fs["stopped"] = True
        self.status["last_run"] = time.time()
        if job is not None:
            parts = [f"{done} 轮"] + [f"{lab} {totals[k]}" for k, lab in (("stars", "星"), ("proposals", "核心提案"),
                     ("fragments", "任务碎片"), ("closed", "闭环"), ("failed", "失败")) if totals[k]]
            jobs.finish(job, "failed" if stopped and not done else "done", "，".join(parts))
            if (totals["stars"] or totals["closed"]) and w.get("organize_after", True):
                try:   # 新星交给主模型整理进星系（排队，任务抽屉可见）
                    from .. import organize
                    organize.schedule()
                except Exception:
                    pass
        return done

    def process_db(self, key: str, force: bool = False) -> int:
        with self.lock:
            ds = self.state.dbs.get(key)
            if not ds:
                return 0
            cfg = config.load_config()
            if not self._llm_ok(cfg):
                ds["p"], ds["retry"] = True, time.time() + 60
                return 0
            w = self.wcfg(cfg)
            try:
                turns, info = opencode.read_new(Path(key), ds, float(w.get("idle_sec") or 45), force)
            except Exception as e:  # noqa: BLE001
                self.status["last_error"] = f"OpenCode 数据库：{e}"[:300]
                self._cst(ds.get("c", "opencode"))["last_error"] = str(e)[:300]
                return 0
            sess = ds.setdefault("sess", {})

            def mark(t: Turn):
                sess[t.session_id] = max(int(sess.get(t.session_id) or 0), int(t.end))
                self.state.save()
            box: dict = {"f": int(ds.get("f") or 0)}
            n = self._process_turns(turns, box, force=True, on_done=mark)
            ds["f"] = box.get("f", 0)
            if box.get("stopped"):      # 模型出错：下次重试（已处理的会话游标已保存）
                ds["p"], ds["retry"] = True, time.time() + 60
            else:
                pm = info["pending_min"]
                ds["since"] = (pm - 1) if pm is not None else max(int(ds.get("since") or 0), int(info["max_seen"]))
                ds["p"] = pm is not None
            st = Path(key).stat()
            ds["sig"] = [st.st_mtime, st.st_size]
            try:
                s2 = os.stat(key + "-wal")
                ds["sig"] += [s2.st_mtime, s2.st_size]
            except OSError:
                pass
            self.state.save()
            return n

    # ---------------- 触发 ----------------
    def notify(self, client: str, payload: dict | None = None, delay: float = 1.5) -> dict:
        """客户端的 Stop 钩子 / notify：本轮结束了，尽快处理。"""
        payload = payload or {}
        if client not in CLIENTS:
            return {"ok": False, "reason": f"不认识的客户端：{client}"}
        cfg = config.load_config()
        w = self.wcfg(cfg)
        if client not in self.enabled_clients(cfg):
            return {"ok": False, "reason": "这个客户端的对话监控没有打开"}
        ccfg = (w.get("clients") or {}).get(client) or {}
        if CLIENTS[client]["kind"] == "opencode":
            for db in adapters.roots(client, ccfg):
                if str(db) in self.state.dbs:
                    self.enqueue("db", str(db), force=True, delay=delay)
            return {"ok": True, "queued": "db"}
        sid = str(payload.get("session_id") or payload.get("thread-id") or payload.get("thread_id") or "")
        p = None
        tp = payload.get("transcript_path")
        if tp:
            cand = Path(os.path.expandvars(str(tp))).expanduser()
            if adapters.under_roots(client, ccfg, cand) and cand.exists():
                p = cand
        if p is None and sid:
            p = adapters.find_session_file(client, ccfg, sid, float(w.get("hot_days") or 3))
        hint = str(payload.get("last_assistant_message") or payload.get("last-assistant-message") or "")
        if p is not None:
            key = str(p)
            if key not in self.state.files:
                self.state.clients.setdefault(client, {"init": time.time()})
                self.state.files[key] = {"c": client, "o": 0, "s": -1, "m": 0, "meta": {}, "p": False, "f": 0}
                self.state.save()
            self.enqueue("file", key, force=True, hint=hint, delay=delay)
            return {"ok": True, "queued": key}
        msgs = payload.get("input-messages") or payload.get("input_messages")
        prompt = "\n".join(msgs) if isinstance(msgs, list) else str(payload.get("prompt") or "")
        if hint and prompt:      # 读不到记录文件（如 dsh 的压缩日志）：用钩子自带的文字
            t = Turn(client, sid, str(payload.get("cwd") or ""), "", prompt, hint, turn_complete=True)
            self.q.put(("turn", t, True, ""))
            return {"ok": True, "queued": "payload"}
        return {"ok": False, "reason": "没找到这次对话的记录文件，轮询稍后会捕获"}

    def replay_inbox(self) -> int:
        """服务没开时钩子写进 inbox 的提醒：启动时补上，然后清空。"""
        p = inbox_path()
        if not p.exists():
            return 0
        n = 0
        try:
            lines = p.read_text("utf-8").splitlines()
            p.unlink()
        except OSError:
            return 0
        for ln in lines:
            try:
                rec = json.loads(ln)
                self.notify(str(rec.get("client") or ""), rec.get("payload") or {}, delay=0)
                n += 1
            except Exception:
                continue
        if n:
            log_event("watch_inbox", count=n)
        return n

    def process_now(self) -> dict:
        """设置里的「立即归纳」：重新扫描，静止 5 秒以上的最后一轮也算完成，全部处理掉。"""
        cfg = config.load_config()
        w = self.wcfg(cfg)
        now = time.time()
        if not self._llm_ok(cfg):
            return {"ok": False, "turns": 0, "reason": self.status["waiting"]}
        self.replay_inbox()
        n = 0
        with jobs.run("watch", "立即归纳全部客户端") as j:
            for c in self.enabled_clients(cfg):
                ccfg = (w.get("clients") or {}).get(c) or {}
                if CLIENTS[c]["kind"] == "opencode":
                    self._poll_db(c, ccfg, w, now)
                    for key, ds in list(self.state.dbs.items()):
                        if ds.get("c") == c:
                            n += self.process_db(key, force=True)
                    continue
                self.rescan(c, ccfg, w, now)
                for key, fs in list(self.state.files.items()):
                    if fs.get("c") != c:
                        continue
                    try:
                        st = os.stat(key)
                    except OSError:
                        continue
                    if st.st_size != fs.get("s") or st.st_mtime != fs.get("m") or fs.get("p"):
                        n += self.process_file(key, force=now - st.st_mtime >= 5)
            n += self.drain()
            j["detail"] = f"处理了 {n} 轮对话"
        return {"ok": True, "turns": n}

    def public(self) -> dict:
        s = dict(self.status)
        s["queue"] = self.q.qsize() + len(self.delayed)
        return s


_W: Watcher | None = None
_wl = threading.Lock()


def get() -> Watcher:
    global _W
    with _wl:
        if _W is None:
            _W = Watcher()
        return _W


def reset() -> None:
    """换了灵魂包目录（测试）时重新读取偏移量。"""
    global _W
    with _wl:
        if _W is not None:
            _W.stop()
        _W = None
