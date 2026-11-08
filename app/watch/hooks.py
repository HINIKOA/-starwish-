"""各客户端的「本轮结束」钩子：安装 / 卸载 / 状态。

  Claude Code / Qoder / WorkBuddy / CodeBuddy   settings.json 的 hooks.Stop（Claude 同款格式）
  Codex                                         ~/.codex/config.toml 顶层 notify = [...]（单个 JSON 参数）
  OpenCode                                      ~/.config/opencode/plugins/starwish.js（session.idle）
  DeepSeek Harness                              用官方 dsh-hooks-claude-code 桥接 Claude 的 Stop 钩子（只给说明）

原则：改之前先备份到 soul/watch/hook-backups/；重复安装不会重复添加；卸载只删星愿自己的条目，清掉空壳；
配置文件不是合法 JSON 时拒绝改写。钩子命令是「本程序 --notify <客户端>」，服务没开也会 0 秒退出、不卡客户端。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from .. import config
from .adapters import CLIENTS, config_dirs, home
from .state import watch_dir

MARK_TOML = "# starwish-notify"
MARK_JS = "starwish-notify"


# ---------------- 钩子命令 ----------------
def command_argv(client: str) -> list[str]:
    """单文件版：STARWISH_EXE；文件夹版：同目录的 星愿记忆.exe；源码运行：python desktop.py。"""
    exe = os.environ.get("STARWISH_EXE") or ""
    if exe:
        return [exe, "--notify", client]
    folder_exe = config.APP_DIR / "星愿记忆.exe"
    if folder_exe.exists():
        return [str(folder_exe), "--notify", client]
    py = sys.executable or "python"
    if os.name == "nt" and py.lower().endswith("pythonw.exe"):
        py = py[:-len("pythonw.exe")] + "python.exe"
    return [py, str(config.APP_DIR / "desktop.py"), "--notify", client]


def command_str(client: str) -> str:
    argv = command_argv(client)
    if os.name == "nt":
        return " ".join(a if re.fullmatch(r"[\w.-]+", a) else f'"{a}"' for a in argv)
    return " ".join(shlex.quote(a) for a in argv)


def is_ours(cmd: str) -> bool:
    c = str(cmd or "")
    return "--notify" in c and ("星愿记忆" in c or "starwish" in c.lower() or "desktop.py" in c)


# ---------------- 通用 ----------------
def backup(path: Path, client: str) -> Path | None:
    if not path.exists():
        return None
    d = watch_dir() / "hook-backups"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{client}-{time.strftime('%Y%m%dT%H%M%S')}-{path.name}"
    shutil.copy2(path, dst)
    return dst


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".starwish-tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


def settings_path(client: str) -> Path:
    return config_dirs(client)[0] / "settings.json"


def codex_config_path() -> Path:
    env = os.environ.get("CODEX_HOME")
    return (Path(env).expanduser() if env else home() / ".codex") / "config.toml"


def opencode_plugin_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    b = Path(base).expanduser() if base else home() / ".config"
    return b / "opencode" / "plugins" / "starwish.js"


# ---------------- Claude 同款 settings.json ----------------
def _load_json(path: Path) -> dict:
    if not path.exists() or not path.read_text("utf-8").strip():
        return {}
    try:
        d = json.loads(path.read_text("utf-8"))
    except Exception as e:
        raise ValueError(f"{path} 不是合法的 JSON，为了安全没有改动它：{e}")
    if not isinstance(d, dict):
        raise ValueError(f"{path} 的内容不是对象，没有改动它")
    return d


def _strip_ours(d: dict) -> int:
    """从 hooks.Stop 里删掉星愿的条目，清掉空壳。返回删了几条。"""
    hooks = d.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    groups = hooks.get("Stop")
    if isinstance(groups, list):
        keep = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                hs = [h for h in g["hooks"] if not (isinstance(h, dict) and is_ours(h.get("command")))]
                removed += len(g["hooks"]) - len(hs)
                if hs:
                    keep.append({**g, "hooks": hs})
                elif len(g["hooks"]) == 0:
                    keep.append(g)
            else:
                keep.append(g)
        if keep:
            hooks["Stop"] = keep
        else:
            hooks.pop("Stop", None)
    if not hooks:
        d.pop("hooks", None)
    return removed


def install_settings(client: str) -> dict:
    p = settings_path(client)
    d = _load_json(p)
    bk = backup(p, client)
    _strip_ours(d)
    hooks = d.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{p} 里的 hooks 不是对象，没有改动它")
    hooks.setdefault("Stop", []).append({"hooks": [{"type": "command", "command": command_str(client), "timeout": 10}]})
    _write(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "path": str(p), "backup": str(bk) if bk else ""}


def uninstall_settings(client: str) -> dict:
    p = settings_path(client)
    if not p.exists():
        return {"ok": True, "path": str(p), "removed": 0}
    d = _load_json(p)
    n = _strip_ours(d)
    if n:
        backup(p, client)
        _write(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "path": str(p), "removed": n}


def settings_installed(client: str) -> bool:
    try:
        d = _load_json(settings_path(client))
    except ValueError:
        return False
    for g in ((d.get("hooks") or {}).get("Stop") or []):
        for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
            if isinstance(h, dict) and is_ours(h.get("command")):
                return True
    return False


# ---------------- Codex config.toml ----------------
_NOTIFY = re.compile(r"^\s*notify\s*=", re.M)


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)   # TOML 基本字符串与 JSON 字符串转义兼容


def install_codex() -> dict:
    p = codex_config_path()
    text = p.read_text("utf-8") if p.exists() else ""
    lines = text.splitlines()
    lines = [ln for ln in lines if MARK_TOML not in ln]
    for ln in lines:
        if _NOTIFY.match(ln) and not ln.lstrip().startswith("#"):
            raise ValueError(f"{p} 里已经有别的 notify 设置（Codex 只允许一个），请手动合并：notify = "
                             + "[" + ", ".join(_toml_str(a) for a in command_argv("codex")) + "]")
    bk = backup(p, "codex")
    line = "notify = [" + ", ".join(_toml_str(a) for a in command_argv("codex")) + "]  " + MARK_TOML
    # notify 是顶层键，必须写在第一个 [表] 之前
    idx = next((i for i, ln in enumerate(lines) if re.match(r"^\s*\[", ln)), len(lines))
    lines.insert(idx, line)
    _write(p, "\n".join(lines).rstrip("\n") + "\n")
    return {"ok": True, "path": str(p), "backup": str(bk) if bk else ""}


def uninstall_codex() -> dict:
    p = codex_config_path()
    if not p.exists():
        return {"ok": True, "path": str(p), "removed": 0}
    lines = p.read_text("utf-8").splitlines()
    keep = [ln for ln in lines if MARK_TOML not in ln]
    n = len(lines) - len(keep)
    if n:
        backup(p, "codex")
        _write(p, ("\n".join(keep).rstrip("\n") + "\n") if keep else "")
    return {"ok": True, "path": str(p), "removed": n}


def codex_installed() -> bool:
    p = codex_config_path()
    return p.exists() and MARK_TOML in p.read_text("utf-8")


# ---------------- OpenCode 插件 ----------------
PLUGIN_JS = """// {mark} —— 由「星愿记忆」生成：OpenCode 每轮结束（session.idle）时提醒星愿去读新对话。可随时删除本文件。
import {{ spawn }} from "node:child_process";

const CMD = {argv};

export const StarWishNotify = async () => ({{
  event: async ({{ event }}) => {{
    const idle = event?.type === "session.idle" ||
      (event?.type === "session.status" && event?.properties?.status?.type === "idle");
    if (!idle) return;
    try {{
      const payload = JSON.stringify({{ session_id: event?.properties?.sessionID || "" }});
      const p = spawn(CMD[0], [...CMD.slice(1), payload], {{ detached: true, stdio: "ignore", windowsHide: true }});
      p.on("error", () => {{}});
      p.unref();
    }} catch (e) {{}}
  }},
}});
"""


def install_opencode() -> dict:
    p = opencode_plugin_path()
    bk = None
    if p.exists() and MARK_JS not in p.read_text("utf-8", errors="replace"):
        bk = backup(p, "opencode")
    _write(p, PLUGIN_JS.format(mark=MARK_JS, argv=json.dumps(command_argv("opencode"), ensure_ascii=False)))
    return {"ok": True, "path": str(p), "backup": str(bk) if bk else ""}


def uninstall_opencode() -> dict:
    p = opencode_plugin_path()
    if p.exists() and MARK_JS in p.read_text("utf-8", errors="replace"):
        p.unlink()
        return {"ok": True, "path": str(p), "removed": 1}
    return {"ok": True, "path": str(p), "removed": 0}


def opencode_installed() -> bool:
    p = opencode_plugin_path()
    return p.exists() and MARK_JS in p.read_text("utf-8", errors="replace")


# ---------------- 统一入口 ----------------
DSH_HELP = ("DeepSeek Harness 没有自己的钩子文件：在 dsh 的 cordis.yml 里启用官方插件 @deepseek-ai/dsh-hooks-claude-code，"
            "configPath 指向一个含 Stop 钩子的 Claude 设置文件（可先给 Claude Code 安装钩子，再把 configPath 指向 ~/.claude/settings.json），"
            "钩子命令改成：{cmd}")


def install(client: str) -> dict:
    kind = CLIENTS[client]["hook"]
    if kind == "settings":
        return install_settings(client)
    if kind == "codex":
        return install_codex()
    if kind == "opencode":
        return install_opencode()
    return {"ok": False, "manual": True, "help": DSH_HELP.format(cmd=command_str("dsh"))}


def uninstall(client: str) -> dict:
    kind = CLIENTS[client]["hook"]
    if kind == "settings":
        return uninstall_settings(client)
    if kind == "codex":
        return uninstall_codex()
    if kind == "opencode":
        return uninstall_opencode()
    return {"ok": True, "removed": 0}


def installed(client: str) -> bool:
    kind = CLIENTS[client]["hook"]
    try:
        if kind == "settings":
            return settings_installed(client)
        if kind == "codex":
            return codex_installed()
        if kind == "opencode":
            return opencode_installed()
    except OSError:
        return False
    return False


def target(client: str) -> str:
    kind = CLIENTS[client]["hook"]
    return str({"settings": lambda: settings_path(client), "codex": codex_config_path,
                "opencode": opencode_plugin_path}.get(kind, lambda: "")())
