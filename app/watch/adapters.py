"""各客户端：对话记录在哪、长什么样，统一解析成 Turn。

所有解析都容错：认不出的行直接跳过，不会因为客户端升级改了格式就崩。
位置（Windows 上 ~ 即 %USERPROFILE%），都可在设置里覆盖：
  Claude Code  ~/.claude/projects/<slug>/<session>.jsonl          （CLAUDE_CONFIG_DIR）
  Qoder        ~/.qoder/projects、~/.qoder-cn/projects               （QODER_CONFIG_DIR，Claude 同款格式）
  WorkBuddy    ~/.workbuddy/projects、~/.workbuddy-ai/projects       （WORKBUDDY_CONFIG_DIR，OpenAI Responses 风格的平铺记录）
  CodeBuddy    ~/.codebuddy/projects                                （CODEBUDDY_CONFIG_DIR，同 WorkBuddy）
  Codex        ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl          （CODEX_HOME）
  OpenCode     ~/.local/share/opencode/opencode.db                   （XDG_DATA_HOME；见 opencode.py）
  dsh          无默认位置，设置里填会话根目录（实验性）
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .turns import Turn, tool_desc

CLIENTS: dict[str, dict] = {
    "claude": {"title": "Claude Code", "kind": "claude", "env": "CLAUDE_CONFIG_DIR", "homes": [".claude"], "sub": "projects",
               "hook": "settings", "verified": "钩子输入已核对官方文档；JSONL 行结构按通行格式"},
    "qoder": {"title": "Qoder", "kind": "claude", "env": "QODER_CONFIG_DIR", "homes": [".qoder", ".qoder-cn"], "sub": "projects",
              "hook": "settings", "verified": "未核实（按 Claude 同款格式处理）"},
    "workbuddy": {"title": "WorkBuddy", "kind": "workbuddy", "env": "WORKBUDDY_CONFIG_DIR", "homes": [".workbuddy", ".workbuddy-ai"],
                  "sub": "projects", "hook": "settings", "verified": "记录类型来自第三方实测；内容细节容错解析"},
    "codebuddy": {"title": "CodeBuddy", "kind": "workbuddy", "env": "CODEBUDDY_CONFIG_DIR", "homes": [".codebuddy"], "sub": "projects",
                  "hook": "settings", "verified": "钩子格式已核对官方文档；记录同 WorkBuddy"},
    "codex": {"title": "Codex", "kind": "codex", "env": "CODEX_HOME", "homes": [".codex"], "sub": "sessions",
              "hook": "codex", "verified": "rollout 格式与 notify 已核对官方文档 / 源码"},
    "opencode": {"title": "OpenCode", "kind": "opencode", "env": "", "homes": [], "sub": "",
                 "hook": "opencode", "verified": "1.x 表结构已核对源码；2.x 与插件加载未核实"},
    "dsh": {"title": "DeepSeek Harness", "kind": "dsh", "env": "", "homes": [], "sub": "", "hook": "dsh",
            "experimental": True, "verified": "实验性：事件名未核实；默认 zstd 压缩需装 zstandard"},
}


def home() -> Path:
    """用户主目录（测试可用 STARWISH_HOME 覆盖）。Windows 上就是 %USERPROFILE%。"""
    h = os.environ.get("STARWISH_HOME")
    return Path(h) if h else Path.home()


def config_dirs(client: str) -> list[Path]:
    """客户端自己的配置目录（钩子写在这里）。"""
    c = CLIENTS[client]
    env = os.environ.get(c["env"]) if c.get("env") else None
    if env:
        return [Path(env).expanduser()]
    return [home() / h for h in c.get("homes", [])]


def roots(client: str, ccfg: dict | None = None) -> list[Path]:
    """对话记录的根目录（或 OpenCode 的数据库文件）。设置里的路径覆盖优先，多个用 ; 分隔。"""
    ccfg = ccfg or {}
    over = str(ccfg.get("path") or "").strip()
    if over:
        return [Path(os.path.expandvars(p.strip())).expanduser() for p in re.split(r"[;\n]", over) if p.strip()]
    c = CLIENTS[client]
    if c["kind"] == "opencode":
        base = os.environ.get("XDG_DATA_HOME")
        b = Path(base) if base else home() / ".local" / "share"
        return [b / "opencode" / "opencode.db"]
    if c["kind"] == "dsh":
        return []
    return [d / c["sub"] for d in config_dirs(client)]


# ---------------- 发现文件 ----------------
def _scan(d: Path, pattern: re.Pattern, out: list, depth: int) -> None:
    try:
        it = os.scandir(d)
    except OSError:
        return
    with it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth > 0 and e.name not in ("subagents", "tool-results", "file-history"):
                        _scan(Path(e.path), pattern, out, depth - 1)
                elif pattern.match(e.name):
                    st = e.stat()
                    out.append((Path(e.path), st.st_mtime, st.st_size))
            except OSError:
                pass


JSONL = re.compile(r"^(?!agent-).+\.jsonl$")
ROLLOUT = re.compile(r"^rollout-.+\.jsonl$")
DSH = re.compile(r"^session.*\.jsonl(\.zstd)?$")


def discover(client: str, ccfg: dict | None = None, hot_days: float = 3) -> list[tuple[Path, float, int]]:
    """列出对话文件 (路径, mtime, 大小)。只列目录，不读内容。"""
    kind = CLIENTS[client]["kind"]
    out: list = []
    for r in roots(client, ccfg):
        if kind in ("claude", "workbuddy"):
            _scan(r, JSONL, out, 1)
        elif kind == "codex":
            today = date.today()
            days = max(1, int(hot_days) + 1)
            dirs = {r / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" for d in (today - timedelta(n) for n in range(-1, days))}
            for d in sorted(dirs):
                _scan(d, ROLLOUT, out, 0)
            _scan(r, ROLLOUT, out, 0)   # 有人把 rollout 平铺在根目录
        elif kind == "dsh":
            _scan(r, DSH, out, 3)
        elif kind == "opencode":
            try:
                st = r.stat()
                out.append((r, st.st_mtime, st.st_size))
            except OSError:
                pass
    return out


# ---------------- 通用：逐行读取 + 拼成轮次 ----------------
def iter_lines(data: bytes, base: int):
    """只给完整的行（以 \\n 结尾）；返回生成器，元素 (起点, 终点, 对象或 None)。"""
    pos = 0
    n = len(data)
    while pos < n:
        nl = data.find(b"\n", pos)
        if nl < 0:
            return
        raw = data[pos:nl].strip()
        obj = None
        if raw:
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                obj = None
        yield base + pos, base + nl + 1, obj
        pos = nl + 1


def complete_end(data: bytes, base: int) -> int:
    i = data.rfind(b"\n")
    return base + i + 1 if i >= 0 else base


def text_of(content) -> str:
    """消息内容 → 纯文本（字符串 / 块列表 / {text}）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "") if not isinstance(content.get("content"), list) \
            else text_of(content.get("content"))
    out = []
    for p in content:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and p.get("type") in (None, "text", "input_text", "output_text"):
            t = p.get("text")
            if isinstance(t, str):
                out.append(t)
    return "\n".join(x for x in out if x)


def iso(ts) -> str:
    """各种时间戳 → ISO 8601（带时区）。"""
    if ts is None or ts == "":
        return ""
    if isinstance(ts, (int, float)):
        v = ts / 1000 if ts > 1e11 else ts
        return datetime.fromtimestamp(v, timezone.utc).astimezone().isoformat(timespec="seconds")
    s = str(ts)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except Exception:
        return s


class Builder:
    """把事件流拼成轮次。事件：user / assistant / tool / end / meta。"""

    def __init__(self, client: str, meta: dict):
        self.client = client
        self.meta = meta
        self.turns: list[Turn] = []
        self.cur: Turn | None = None

    def user(self, text: str, ts, start: int, end: int):
        text = (text or "").strip()
        if not text:
            return
        if self.cur:
            self.cur.turn_complete = True
            self.turns.append(self.cur)
        self.cur = Turn(self.client, self.meta.get("session_id", ""), self.meta.get("cwd", ""), iso(ts), text,
                        start=start, end=end)

    def assistant(self, text: str, end: int):
        if self.cur:
            self.cur.add_assistant(text)
            self.cur.end = end

    def tool(self, desc: str, end: int):
        if self.cur:
            self.cur.add_tool(desc)
            self.cur.end = end

    def touch(self, end: int):
        if self.cur:
            self.cur.end = end

    def finish_turn(self, end: int):
        if self.cur:
            self.cur.end = end
            self.cur.turn_complete = True
            self.turns.append(self.cur)
            self.cur = None

    def result(self, consumed_to: int) -> tuple[list[Turn], int]:
        """返回 (轮次列表, 可以安全推进到的偏移)。最后一轮未完成时也放进列表（turn_complete=False）。"""
        out = list(self.turns)
        safe = consumed_to
        if self.cur:
            out.append(self.cur)
            safe = self.cur.start
        for t in out:
            t.session_id = t.session_id or self.meta.get("session_id", "")
            t.cwd = t.cwd or self.meta.get("cwd", "")
        return out, safe


# ---------------- Claude Code / Qoder ----------------
_CMD = re.compile(r"^\s*<(local-command-|command-name|command-message|command-args|system-reminder|bash-|user-memory-input)")


def parse_claude(client: str, data: bytes, base: int, meta: dict, path: Path) -> tuple[list[Turn], int]:
    meta.setdefault("session_id", path.stem)
    b = Builder(client, meta)
    for s, e, o in iter_lines(data, base):
        if not isinstance(o, dict):
            b.touch(e)
            continue
        if o.get("sessionId"):
            meta["session_id"] = o["sessionId"]
        if o.get("cwd"):
            meta["cwd"] = o["cwd"]
        if o.get("isSidechain"):      # 子代理的内部对话，不算主人的轮次
            b.touch(e)
            continue
        t = o.get("type")
        msg = o.get("message") or {}
        c = msg.get("content") if isinstance(msg, dict) else None
        if t == "user":
            if o.get("isMeta") or (isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c)):
                b.touch(e)
                continue
            txt = text_of(c)
            if _CMD.match(txt or ""):
                b.touch(e)
                continue
            b.user(txt, o.get("timestamp"), s, e)
            if b.cur:
                b.cur.session_id = meta.get("session_id", "")
                b.cur.cwd = meta.get("cwd", "")
        elif t == "assistant":
            if isinstance(c, list):
                for p in c:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        b.assistant(p.get("text", ""), e)
                    elif p.get("type") == "tool_use":
                        b.tool(tool_desc(p.get("name"), p.get("input")), e)
                b.touch(e)
            else:
                b.assistant(text_of(c), e)
        else:
            b.touch(e)
    return b.result(complete_end(data, base))


# ---------------- WorkBuddy / CodeBuddy（Responses 风格平铺记录） ----------------
def parse_workbuddy(client: str, data: bytes, base: int, meta: dict, path: Path) -> tuple[list[Turn], int]:
    meta.setdefault("session_id", path.stem)
    b = Builder(client, meta)
    for s, e, o in iter_lines(data, base):
        if not isinstance(o, dict):
            b.touch(e)
            continue
        if o.get("sessionId") or o.get("session_id"):
            meta["session_id"] = o.get("sessionId") or o.get("session_id")
        if o.get("cwd"):
            meta["cwd"] = o["cwd"]
        t = o.get("type")
        if t == "message" or (t is None and o.get("role")):
            role = o.get("role") or (o.get("message") or {}).get("role")
            content = o.get("content") if "content" in o else (o.get("message") or {}).get("content")
            if role == "user":
                txt = text_of(content)
                if txt and not _CMD.match(txt):
                    b.user(txt, o.get("timestamp"), s, e)
                    if b.cur:
                        b.cur.session_id, b.cur.cwd = meta.get("session_id", ""), meta.get("cwd", "")
                else:
                    b.touch(e)
            elif role == "assistant":
                b.assistant(text_of(content), e)
            else:
                b.touch(e)
        elif t == "function_call":
            b.tool(tool_desc(o.get("name"), o.get("arguments")), e)
        elif t in ("user", "assistant"):   # 少数版本混用 Claude 格式
            sub, _ = parse_claude(client, data[s - base:e - base], s, meta, path)
            for x in sub:
                if x.user_text:
                    b.user(x.user_text, x.ts, s, e)
                b.assistant(x.assistant_text, e)
                if x.tool_summary:
                    b.tool(x.tool_summary, e)
            b.touch(e)
        else:
            b.touch(e)
    return b.result(complete_end(data, base))


# ---------------- Codex ----------------
_ROLL_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-z-]{20,})\.jsonl$", re.I)


def parse_codex(client: str, data: bytes, base: int, meta: dict, path: Path) -> tuple[list[Turn], int]:
    if "session_id" not in meta:
        m = re.match(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)\.jsonl$", path.name)
        meta["session_id"] = m.group(1) if m else path.stem
    b = Builder(client, meta)
    for s, e, o in iter_lines(data, base):
        if not isinstance(o, dict):
            b.touch(e)
            continue
        t, p = o.get("type"), o.get("payload") or {}
        if t == "session_meta":
            meta["session_id"] = p.get("id") or p.get("session_id") or meta["session_id"]
            if p.get("cwd"):
                meta["cwd"] = p["cwd"]
            b.touch(e)
        elif t == "turn_context":
            if p.get("cwd"):
                meta["cwd"] = p["cwd"]
            b.touch(e)
        elif t == "event_msg":
            pt = p.get("type")
            if pt == "user_message":
                b.user(p.get("message", ""), o.get("timestamp"), s, e)
                if b.cur:
                    b.cur.cwd = meta.get("cwd", "")
            elif pt == "agent_message":
                b.assistant(p.get("message", ""), e)
            elif pt in ("task_complete", "turn_complete"):
                if b.cur and not b.cur.assistant_text and p.get("last_agent_message"):
                    b.assistant(p["last_agent_message"], e)
                b.finish_turn(e)
            elif pt == "turn_aborted":
                b.finish_turn(e)
            else:
                b.touch(e)
        elif t == "response_item":
            pt = p.get("type")
            if pt in ("function_call", "custom_tool_call", "local_shell_call"):
                b.tool(tool_desc(p.get("name") or pt, p.get("arguments") or p.get("input") or p.get("action")), e)
            else:
                b.touch(e)
        else:
            b.touch(e)
    return b.result(complete_end(data, base))


# ---------------- DeepSeek Harness（实验性） ----------------
def _deep_texts(v, out: list, depth=0):
    if depth > 6:
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if k in ("text", "content", "message") and isinstance(x, str):
                out.append(x)
            elif k not in ("reasoning", "thinking", "signature"):
                _deep_texts(x, out, depth + 1)
    elif isinstance(v, list):
        for x in v:
            _deep_texts(x, out, depth + 1)


def read_dsh_bytes(path: Path) -> bytes | None:
    """.jsonl 直接读；.jsonl.zstd 需要 zstandard（没装就返回 None）。"""
    if path.name.endswith(".zstd"):
        try:
            import zstandard  # type: ignore
        except Exception:
            return None
        raw = path.read_bytes()
        out = bytearray()
        d = zstandard.ZstdDecompressor()
        try:
            with d.stream_reader(raw, read_across_frames=True) as r:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    out += chunk
        except Exception:
            pass
        return bytes(out)
    return path.read_bytes()


def parse_dsh(client: str, data: bytes, base: int, meta: dict, path: Path) -> tuple[list[Turn], int]:
    meta.setdefault("session_id", path.parent.name)
    b = Builder(client, meta)
    for s, e, o in iter_lines(data, base):
        if not isinstance(o, dict):
            b.touch(e)
            continue
        t = str(o.get("type") or "")
        d = o.get("data") if isinstance(o.get("data"), (dict, list)) else {}
        if t == "session":
            meta["session_id"] = o.get("id") or meta["session_id"]
            if o.get("cwd"):
                meta["cwd"] = o["cwd"]
            b.touch(e)
        elif t.startswith("user/") or t == "user":
            out: list = []
            _deep_texts(d, out)
            b.user("\n".join(out), o.get("time"), s, e)
        elif t == "text-chunks":
            b.assistant("".join(map(str, (d or {}).get("texts") or [])), e)
        elif t.startswith("assistant/") and t not in ("assistant/chunk",):
            out = []
            _deep_texts(d, out)
            if out:
                b.assistant("\n".join(out), e)
            else:
                b.touch(e)
        elif t == "assistant/chunk" and isinstance(d, dict) and isinstance(d.get("text"), str):
            if b.cur:
                b.cur.assistant_text += d["text"]
                b.cur.end = e
        elif "tool" in t and isinstance(d, dict) and d.get("name"):
            b.tool(tool_desc(d.get("name"), d.get("input") or d.get("args")), e)
        elif "turn-end" in t or "turn/end" in t or t.endswith("turn-stopped"):
            b.finish_turn(e)
        else:
            b.touch(e)
    return b.result(complete_end(data, base))


PARSERS = {"claude": parse_claude, "workbuddy": parse_workbuddy, "codex": parse_codex, "dsh": parse_dsh}


def parse(client: str, data: bytes, base: int, meta: dict, path: Path) -> tuple[list[Turn], int]:
    return PARSERS[CLIENTS[client]["kind"]](client, data, base, meta, Path(path))


def find_session_file(client: str, ccfg: dict, session_id: str, hot_days: float = 3) -> Path | None:
    """钩子只给了 session_id（如 Codex 的 thread-id）时，按文件名找。"""
    if not session_id:
        return None
    best = None
    for p, mt, _ in discover(client, ccfg, hot_days):
        if session_id in p.name and (best is None or mt > best[1]):
            best = (p, mt)
    return best[0] if best else None


def under_roots(client: str, ccfg: dict, p: Path) -> bool:
    """notify 传来的路径必须在这个客户端的目录下（防止被指去读任意文件）。"""
    try:
        rp = p.resolve()
    except OSError:
        return False
    for r in roots(client, ccfg):
        try:
            rp.relative_to(r.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def now_ms() -> int:
    return int(time.time() * 1000)
