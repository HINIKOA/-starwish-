"""OpenCode：对话存在 SQLite（~/.local/share/opencode/opencode.db）。只读打开（mode=ro），自动识别：
  1.x  session / message / part，data 列是 JSON（message.data.role、part.data.type=text|tool…）——已对照源码
  2.x  session_message(session_id, type, seq, time_created, data)——data 结构未核实，尽力解析
游标按会话记（毫秒时间戳），不重复读。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .adapters import _deep_texts, iso, now_ms
from .turns import Turn, tool_desc


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=1")
    except sqlite3.DatabaseError:
        pass
    return c


def detect(c: sqlite3.Connection) -> str:
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"message", "part"} <= names:
        return "v1"
    if "session_message" in names:
        return "v2"
    return ""


def _j(s):
    try:
        return json.loads(s) if isinstance(s, str) else (s or {})
    except Exception:
        return {}


def _dirs(c, sids) -> dict:
    if not sids:
        return {}
    try:
        q = f"SELECT id, directory FROM session WHERE id IN ({','.join('?' * len(sids))})"
        return {r["id"]: r["directory"] or "" for r in c.execute(q, list(sids))}
    except sqlite3.DatabaseError:
        return {}


def read_new(path: Path, st: dict, idle_sec: float = 45, force: bool = False, now: int | None = None, limit: int = 2000):
    """返回 (完成的轮次列表, 待定信息)。st = {"since": ms, "sess": {sid: 已处理到的 ms}}。
    待定信息 {"pending_min": 最早未完成轮次的开始 ms 或 None, "max_seen": 本次看到的最大 ms}。"""
    now = now or now_ms()
    c = connect_ro(path)
    try:
        ver = detect(c)
        since = int(st.get("since") or 0)
        if ver == "v1":
            rows = c.execute("SELECT id, session_id, time_created, data FROM message WHERE time_created > ? "
                             "ORDER BY time_created, id LIMIT ?", (since, limit)).fetchall()
            ids = [r["id"] for r in rows]
            parts: dict[str, list] = {}
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                q = f"SELECT message_id, data FROM part WHERE message_id IN ({','.join('?' * len(chunk))}) ORDER BY id"
                for p in c.execute(q, chunk):
                    parts.setdefault(p["message_id"], []).append(_j(p["data"]))
            msgs = []
            for r in rows:
                d = _j(r["data"])
                texts, tools = [], []
                for p in parts.get(r["id"], []):
                    if p.get("type") == "text" and not p.get("synthetic") and isinstance(p.get("text"), str):
                        texts.append(p["text"])
                    elif p.get("type") == "tool":
                        tools.append(tool_desc(p.get("tool"), (p.get("state") or {}).get("input")))
                done = ((d.get("time") or {}).get("completed")) if isinstance(d.get("time"), dict) else None
                msgs.append({"sid": r["session_id"], "t": int(r["time_created"]), "role": d.get("role"),
                             "text": "\n".join(texts), "tools": tools, "done": done})
        elif ver == "v2":
            rows = c.execute("SELECT session_id, type, seq, time_created, data FROM session_message WHERE time_created > ? "
                             "ORDER BY time_created, seq LIMIT ?", (since, limit)).fetchall()
            msgs = []
            for r in rows:
                d = _j(r["data"])
                typ = str(r["type"] or d.get("type") or "").lower()
                role = "user" if "user" in typ else ("assistant" if ("assistant" in typ or "agent" in typ) else "")
                out: list = []
                _deep_texts(d, out)
                msgs.append({"sid": r["session_id"], "t": int(r["time_created"]), "role": role, "text": "\n".join(out),
                             "tools": [], "done": d.get("completed") or (role == "assistant")})
        else:
            return [], {"pending_min": None, "max_seen": since, "version": ""}
        dirs = _dirs(c, {m["sid"] for m in msgs})
    finally:
        c.close()

    sess = st.setdefault("sess", {})
    by: dict[str, list] = {}
    max_seen = since
    for m in msgs:
        max_seen = max(max_seen, m["t"])
        if m["t"] > int(sess.get(m["sid"]) or 0):
            by.setdefault(m["sid"], []).append(m)
    turns: list[Turn] = []
    pending_min = None
    for sid, ms in by.items():
        cur = None
        last_done = None
        for m in ms:
            if m["role"] == "user":
                if cur:
                    cur.turn_complete = True
                    turns.append(cur)
                cur = Turn("opencode", sid, dirs.get(sid, ""), iso(m["t"]), m["text"].strip(), start=m["t"], end=m["t"])
                last_done = None
            elif cur and m["role"] == "assistant":
                cur.add_assistant(m["text"])
                for x in m["tools"]:
                    cur.add_tool(x)
                cur.end = m["t"]
                last_done = m["done"]
        if cur:
            idle_ok = last_done and (now - int(last_done if isinstance(last_done, (int, float)) and last_done > 1 else cur.end)) >= idle_sec * 1000
            if cur.assistant_text and (force or idle_ok):
                cur.turn_complete = True
                turns.append(cur)
            else:
                pending_min = cur.start if pending_min is None else min(pending_min, cur.start)
    turns.sort(key=lambda t: t.end)
    return turns, {"pending_min": pending_min, "max_seen": max_seen, "version": ver}
