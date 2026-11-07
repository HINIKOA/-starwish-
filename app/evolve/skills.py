"""技能库：AI 的「程序性记忆」（怎么做某件事），与星星（发生过什么）和核心记忆（我是谁）并列。

格式兼容 Hermes Agent / agentskills.io：
    soul/skills/[分类/]<名字>/SKILL.md      YAML 头（name、description 必填）+ Markdown 正文
                                  references/ templates/ scripts/ assets/   辅助文件
操作：create / patch（模糊匹配热补丁）/ edit（整篇重写）/ archive（归档=变暗，不硬删）/ restore /
      write_file / remove_file / rollback（回到任意版本）
每次修改：原子写入 → 安全扫描（不通过自动回滚或转主人批准）→ 记 `skill_change` 事件（前/后全文）→ 更新使用统计。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .. import config
from ..db import estimate_tokens, log_event, tokenize
from . import guard
from .fuzzy import fuzzy_find_and_replace

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_NAME = 64
MAX_DESC = 300
MAX_CONTENT = 100_000
MAX_FILE = 1_048_576
SUBDIRS = {"references", "templates", "scripts", "assets"}
_FM_END = re.compile(r"\n---\s*\n")
LOCK = threading.RLock()

SOURCE_TITLES = {"owner": "主人", "panel": "面板", "mcp": "MCP·AI", "review": "后台巡检", "api": "API", "import": "导入",
                 "rollback": "回滚", "approve": "批准待审", "command": "对话指令"}


class SkillError(Exception):
    pass


def skills_dir() -> Path:
    d = config.SOUL_DIR / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_dir() -> Path:
    d = skills_dir() / ".archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _usage_path() -> Path:
    return skills_dir() / ".usage.json"


def _pending_path() -> Path:
    return skills_dir() / ".pending.json"


# ---------------- 工具函数 ----------------

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def parse_frontmatter(content: str) -> tuple[dict, str]:
    content = (content or "").lstrip("\ufeff")
    if not content.startswith("---"):
        return {}, content
    m = _FM_END.search(content[3:])
    if not m:
        return {}, content
    raw, body = content[3:m.start() + 3], content[m.end() + 3:]
    try:
        import yaml  # type: ignore
        fm = yaml.safe_load(raw) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
        for ln in raw.splitlines():
            if ":" in ln and not ln.startswith((" ", "\t", "-")):
                k, v = ln.split(":", 1)
                fm[k.strip()] = v.strip().strip("'\"")
    return fm, body


def validate_name(name: str) -> None:
    if not name:
        raise SkillError("技能名不能为空")
    if len(name) > MAX_NAME or not NAME_RE.match(name):
        raise SkillError(f"技能名「{name}」不合法：只能用小写字母、数字、- _ .，以字母或数字开头，最长 {MAX_NAME}")


def validate_content(content: str) -> dict:
    if not (content or "").strip():
        raise SkillError("SKILL.md 内容为空")
    if len(content) > MAX_CONTENT:
        raise SkillError(f"SKILL.md 超过 {MAX_CONTENT} 字符，请把细节拆到 references/")
    if not content.lstrip("\ufeff").startswith("---"):
        raise SkillError("SKILL.md 必须以 YAML 头（---）开始，至少包含 name 和 description")
    fm, body = parse_frontmatter(content)
    if not fm:
        raise SkillError("YAML 头没有闭合或无法解析")
    for k in ("name", "description"):
        if not str(fm.get(k) or "").strip():
            raise SkillError(f"YAML 头必须包含 {k}")
    if len(str(fm["description"])) > MAX_DESC:
        raise SkillError(f"description 最长 {MAX_DESC} 字（一句话：什么时候用 + 做什么），细节写进正文")
    if not body.strip():
        raise SkillError("YAML 头之后必须有正文（步骤、注意事项……）")
    return fm


def make_skill_md(name: str, description: str, body: str, tags=None, extra: dict | None = None) -> str:
    lines = ["---", f"name: {name}", f"description: {json.dumps(description, ensure_ascii=False)}"]
    if tags:
        lines.append("tags: [" + ", ".join(json.dumps(t, ensure_ascii=False) for t in tags) + "]")
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines += ["---", "", body.strip(), ""]
    return "\n".join(lines)


# ---------------- 查找 ----------------

def _iter_skill_mds(root: Path):
    for p in sorted(root.rglob("SKILL.md")):
        rel = p.relative_to(root).parts
        if any(x.startswith(".") for x in rel[:-1]):
            continue
        yield p


def find(name: str, include_archived: bool = False) -> Path | None:
    roots = [skills_dir()] + ([archive_dir()] if include_archived else [])
    for root in roots:
        for p in _iter_skill_mds(root):
            if p.parent.name == name or p.parent.relative_to(root).as_posix() == name:
                return p.parent
    return None


def _is_archived(d: Path) -> bool:
    try:
        d.relative_to(archive_dir())
        return True
    except ValueError:
        return False


# ---------------- 使用统计 & 亮度 ----------------

def _usage() -> dict:
    return _read_json(_usage_path(), {})


def _save_usage(u: dict) -> None:
    atomic_write(_usage_path(), json.dumps(u, ensure_ascii=False, indent=1))


def set_usage(name: str, **fields) -> None:
    with LOCK:
        u = _usage()
        u.setdefault(name, {"created_at": time.time()}).update(fields)
        _save_usage(u)


def bump(name: str, field: str, n: int = 1, **extra) -> None:
    with LOCK:
        u = _usage()
        r = u.setdefault(name, {"created_at": time.time()})
        r[field] = int(r.get(field, 0)) + n
        if field in ("views", "injections", "good", "bad"):
            r["last_used"] = time.time()
        r.update(extra)
        _save_usage(u)


def brightness(rec: dict, now: float | None = None) -> float:
    """技能亮度：和星星一样随时间变暗、被使用会变亮；好评加成、差评减成。永不低于 0.08。"""
    now = now or time.time()
    cfg = config.load_config()["memory"]
    ref = max(rec.get("created_at") or 0, rec.get("last_used") or 0, rec.get("updated_at") or 0)
    decay = 0.5 ** (max(0.0, now - ref) / 86400 / max(0.1, float(cfg.get("half_life_days", 30))))
    use = int(rec.get("views", 0)) + int(rec.get("injections", 0))
    fb = 1 + 0.15 * int(rec.get("good", 0)) - 0.1 * int(rec.get("bad", 0))
    b = decay * (1 + math.log(1 + use)) * max(0.3, fb)
    return round(min(3.0, max(float(cfg.get("brightness_floor", 0.08)), b)), 4)


# ---------------- 读 ----------------

def _info(d: Path, usage: dict | None = None, full: bool = False) -> dict:
    content = (d / "SKILL.md").read_text("utf-8-sig")
    fm, body = parse_frontmatter(content)
    root = archive_dir() if _is_archived(d) else skills_dir()
    rel = d.relative_to(root).as_posix()
    name = d.name
    u = (usage if usage is not None else _usage()).get(name, {})
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    out = {"name": name, "path": rel, "category": rel.split("/")[0] if "/" in rel else "",
           "description": str(fm.get("description", "")), "tags": tags, "archived": _is_archived(d),
           "created_by": u.get("created_by", "owner"), "views": u.get("views", 0), "injections": u.get("injections", 0),
           "patches": u.get("patches", 0), "good": u.get("good", 0), "bad": u.get("bad", 0),
           "created_at": u.get("created_at"), "updated_at": u.get("updated_at"), "last_used": u.get("last_used"),
           "brightness": brightness(u) if u else 1.0, "tokens": estimate_tokens(content), "pinned": bool(u.get("pinned"))}
    if full:
        out["content"] = content
        out["body"] = body
        out["frontmatter"] = fm
        out["files"] = sorted(p.relative_to(d).as_posix() for sd in SUBDIRS if (d / sd).exists()
                              for p in (d / sd).rglob("*") if p.is_file())
    return out


def list_skills(include_archived: bool = False) -> list[dict]:
    u = _usage()
    out = [_info(p.parent, u) for p in _iter_skill_mds(skills_dir())]
    if include_archived:
        out += [_info(p.parent, u) for p in _iter_skill_mds(archive_dir())]
    out.sort(key=lambda x: (x["archived"], -x["brightness"], x["name"]))
    return out


def view(name: str, bump_view: bool = True) -> dict:
    d = find(name, include_archived=True)
    if not d:
        raise KeyError(f"技能 {name} 不存在")
    info = _info(d, full=True)
    if bump_view and not info["archived"]:
        bump(d.name, "views")
    return info


def read_file(name: str, file_path: str) -> str:
    d = find(name, include_archived=True)
    if not d:
        raise KeyError(f"技能 {name} 不存在")
    t = _safe_target(d, file_path)
    if not t.exists():
        raise KeyError(f"文件 {file_path} 不存在")
    return t.read_text("utf-8", errors="replace")


# ---------------- 相关度（给注入用） ----------------

def relevant(query: str, k: int = 5, min_score: float = 0.12) -> list[tuple[float, dict]]:
    q = set(tokenize(query or ""))
    if not q:
        return []
    out = []
    for s in list_skills():
        toks = set(tokenize(" ".join([s["name"].replace("-", " "), s["description"], " ".join(s["tags"])])))
        if not toks:
            continue
        hit = len(q & toks)
        if not hit:
            continue
        score = hit / math.sqrt(len(toks) * max(1, min(len(q), 12)))
        score *= 1 + 0.2 * min(1.0, s["brightness"] / 1.5)
        if score >= min_score:
            out.append((round(score, 4), s))
    out.sort(key=lambda x: -x[0])
    return out[:k]


# ---------------- 待审（主人批准） ----------------

def list_pending() -> list[dict]:
    return _read_json(_pending_path(), [])


def _stage(op: dict, reason: str, scan: dict | None, source: str) -> dict:
    with LOCK:
        items = list_pending()
        rec = {"id": uuid.uuid4().hex[:8], "op": op, "reason": reason, "scan": scan, "source": source, "created_at": time.time()}
        items.append(rec)
        atomic_write(_pending_path(), json.dumps(items, ensure_ascii=False, indent=1))
    log_event("skill_staged", id=rec["id"], action=op.get("action"), name=op.get("name"), reason=reason, source=source)
    return rec


def decide_pending(pid: str, approve: bool) -> dict:
    with LOCK:
        items = list_pending()
        rec = next((x for x in items if x["id"] == pid), None)
        if not rec:
            raise KeyError("待审项不存在")
        items = [x for x in items if x["id"] != pid]
        atomic_write(_pending_path(), json.dumps(items, ensure_ascii=False, indent=1))
    log_event("skill_pending_" + ("approve" if approve else "reject"), id=pid, name=rec["op"].get("name"))
    if not approve:
        return {"success": True, "rejected": True}
    op = dict(rec["op"])
    return manage(**op, source="approve", trust="owner", note=f"批准待审 {pid}（来自 {rec['source']}）",
                  _bypass_gate=True)


# ---------------- 写 ----------------

def _safe_target(d: Path, file_path: str) -> Path:
    fp = (file_path or "").replace("\\", "/").strip("/")
    parts = Path(fp).parts
    if not fp or ".." in parts:
        raise SkillError("路径不合法（不允许 ..）")
    if parts == ("SKILL.md",):
        return d / "SKILL.md"
    if parts[0] not in SUBDIRS or len(parts) < 2:
        raise SkillError(f"辅助文件必须放在 {', '.join(sorted(SUBDIRS))} 之一下面，例如 references/notes.md")
    t = (d / fp).resolve()
    if not str(t).startswith(str(d.resolve())):
        raise SkillError("路径越界")
    return t


def _snapshot(d: Path | None) -> dict:
    """技能目录全部文本文件 → {相对路径: 内容}（用于前后对比与回滚）。"""
    if not d or not d.exists():
        return {}
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.stat().st_size <= MAX_FILE:
            try:
                out[p.relative_to(d).as_posix()] = p.read_text("utf-8")
            except UnicodeDecodeError:
                out[p.relative_to(d).as_posix()] = "（二进制文件）"
    return out


def _restore_snapshot(d: Path, snap: dict) -> None:
    if d.exists():
        for p in sorted(d.rglob("*"), reverse=True):
            rel = p.relative_to(d).as_posix()
            if p.is_file() and rel not in snap:
                p.unlink()
    for rel, text in snap.items():
        if text != "（二进制文件）":
            atomic_write(d / rel, text)


def _log(action: str, name: str, before: dict, after: dict, source: str, note: str = "", **extra) -> int:
    rev = int(_usage().get(name, {}).get("rev", 0)) + 1
    set_usage(name, rev=rev, updated_at=time.time())
    log_event("skill_change", action=action, name=name, rev=rev, source=source, note=note or "",
              before=before, after=after, **extra)
    return rev


def manage(action: str, name: str, content: str | None = None, category: str | None = None,
           old_string: str | None = None, new_string: str | None = None, replace_all: bool = False,
           file_path: str | None = None, file_content: str | None = None, source: str = "owner",
           trust: str = "owner", note: str = "", _bypass_gate: bool = False, **_ignored) -> dict:
    """统一入口（对应 Hermes 的 skill_manage）。trust: owner / agent-created / imported。
    返回 {"success": bool, "message": ..., ...}；AI 写入被拦下时返回 {"staged": True, "pending_id": ...}。"""
    cfg = config.load_config().get("evolve", {})
    op = {"action": action, "name": name, "content": content, "category": category, "old_string": old_string,
          "new_string": new_string, "replace_all": replace_all, "file_path": file_path, "file_content": file_content}
    op = {k: v for k, v in op.items() if v not in (None, False)}
    if trust != "owner" and not _bypass_gate:
        if not cfg.get("enabled", True):
            return {"success": False, "message": "自进化已在设置中关闭，AI 不能修改技能"}
        reason = ""
        if action in ("archive", "delete") and find(name) and _usage().get(name, {}).get("created_by") != "agent":
            reason = "AI 想归档主人创建的技能，需要主人批准"
        elif cfg.get("approval", "auto") == "always":
            reason = "设置为「AI 的所有技能修改都需主人批准」"
        if reason:
            rec = _stage(op, reason, None, source)
            return {"success": True, "staged": True, "pending_id": rec["id"], "message": f"已提交主人批准（待审 {rec['id']}）：{reason}"}
    with LOCK:
        try:
            return _manage(action, name, content, category, old_string, new_string, replace_all, file_path,
                           file_content, source, trust, note, op, cfg, _bypass_gate)
        except SkillError as e:
            return {"success": False, "message": str(e)}


def _scan_or_stage(d: Path, before: dict, op: dict, source: str, trust: str, bypass: bool):
    """写入后扫描。返回 None（放行）或结果 dict（已回滚/已转待审）。"""
    scan = guard.scan_files({k: v for k, v in _snapshot(d).items()})
    decision = "allow" if bypass else guard.decide(scan, trust)
    if decision == "allow":
        return None, scan
    # 回滚写入
    if before:
        _restore_snapshot(d, before)
    else:
        shutil.rmtree(d, ignore_errors=True)
    if decision == "ask":
        rec = _stage(op, "安全扫描发现危险内容，需主人确认", scan, source)
        return {"success": True, "staged": True, "pending_id": rec["id"], "scan": scan,
                "message": f"安全扫描判定为 dangerous，已转主人批准（待审 {rec['id']}）\n" + guard.report(scan)}, scan
    return {"success": False, "scan": scan, "message": "安全扫描未通过，已回滚\n" + guard.report(scan)}, scan


def _manage(action, name, content, category, old_string, new_string, replace_all, file_path, file_content,
            source, trust, note, op, cfg, bypass):
    created_by = "owner" if trust == "owner" else ("agent" if trust == "agent-created" else trust)
    if action == "create":
        validate_name(name)
        if category:
            validate_name(category)
        if find(name, include_archived=True):
            raise SkillError(f"技能 {name} 已存在（归档的也算），请用 patch 修改或换个名字")
        fm = validate_content(content or "")
        if str(fm.get("name")) != name:
            content = re.sub(r"(?m)^name:\s*.*$", f"name: {name}", content, count=1)
        d = skills_dir() / (category or "") / name
        atomic_write(d / "SKILL.md", content)
        blocked, scan = _scan_or_stage(d, {}, op, source, trust, bypass)
        if blocked:
            return blocked
        set_usage(name, created_at=time.time(), created_by=created_by)
        rev = _log("create", name, {}, _snapshot(d), source, note, scan=scan["verdict"])
        return {"success": True, "message": f"已创建技能 {name}（r{rev}）", "rev": rev, "scan": scan["verdict"]}

    d = find(name, include_archived=action in ("restore",))
    if not d:
        raise SkillError(f"技能 {name} 不存在（先用 skills_list 看看有哪些）")
    if _is_archived(d) and action not in ("restore",):
        raise SkillError(f"技能 {name} 已归档，先 restore")
    before = _snapshot(d)

    if action in ("patch", "edit"):
        if action == "edit" or (content and not old_string):
            new_text = content or ""
            validate_content(new_text)
            target, label, strategy, count = d / "SKILL.md", "SKILL.md", "rewrite", 1
        else:
            target = _safe_target(d, file_path) if file_path else d / "SKILL.md"
            label = file_path or "SKILL.md"
            if not target.exists():
                raise SkillError(f"文件 {label} 不存在")
            if new_string is None:
                raise SkillError("patch 需要 new_string（空字符串表示删除）")
            old_text = target.read_text("utf-8-sig")
            new_text, count, strategy, err = fuzzy_find_and_replace(old_text, old_string or "", new_string, replace_all)
            if err:
                return {"success": False, "message": err, "file_preview": old_text[:500]}
            if label == "SKILL.md":
                try:
                    validate_content(new_text)
                except SkillError as e:
                    raise SkillError(f"这个补丁会破坏 SKILL.md 结构：{e}")
        atomic_write(target, new_text)
        blocked, scan = _scan_or_stage(d, before, op, source, trust, bypass)
        if blocked:
            return blocked
        bump(name, "patches")
        rev = _log(action, name, before, _snapshot(d), source, note, strategy=strategy, scan=scan["verdict"])
        return {"success": True, "message": f"已修补 {name} 的 {label}（{count} 处，匹配策略 {strategy}，r{rev}）",
                "rev": rev, "strategy": strategy}

    if action == "write_file":
        if file_content is None:
            raise SkillError("write_file 需要 file_content")
        if len(file_content.encode("utf-8")) > MAX_FILE:
            raise SkillError("辅助文件最大 1 MiB")
        t = _safe_target(d, file_path)
        if t.name == "SKILL.md":
            validate_content(file_content)
        atomic_write(t, file_content)
        blocked, scan = _scan_or_stage(d, before, op, source, trust, bypass)
        if blocked:
            return blocked
        rev = _log(action, name, before, _snapshot(d), source, note, file=file_path)
        return {"success": True, "message": f"已写入 {name}/{file_path}（r{rev}）", "rev": rev}

    if action == "remove_file":
        t = _safe_target(d, file_path)
        if t.name == "SKILL.md":
            raise SkillError("不能删除 SKILL.md；不要这个技能请用 archive")
        if not t.exists():
            raise SkillError(f"文件 {file_path} 不存在")
        t.unlink()
        rev = _log(action, name, before, _snapshot(d), source, note, file=file_path)
        return {"success": True, "message": f"已删除 {name}/{file_path}（r{rev}）", "rev": rev}

    if action in ("archive", "delete"):  # 永不硬删除：归档 = 移进 .archive/，不再注入，可随时恢复
        rel = d.relative_to(skills_dir())
        dest = archive_dir() / rel
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(dest))
        rev = _log("archive", name, before, before, source, note, archived=True)
        return {"success": True, "message": f"已归档技能 {name}（可在面板恢复，r{rev}）", "rev": rev}

    if action == "restore":
        if not _is_archived(d):
            return {"success": True, "message": f"技能 {name} 未归档"}
        rel = d.relative_to(archive_dir())
        dest = skills_dir() / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(dest))
        rev = _log("restore", name, before, before, source, note)
        return {"success": True, "message": f"已恢复技能 {name}（r{rev}）", "rev": rev}

    raise SkillError(f"未知操作 {action}（create / patch / edit / write_file / remove_file / archive / restore）")


# ---------------- 历史 / 回滚 ----------------

def history(name: str | None = None, limit: int = 100) -> list[dict]:
    from ..core.history import diff_lines
    p = config.EVENTS_PATH
    if not p.exists():
        return []
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for ln in f:
            if '"skill_change"' not in ln:
                continue
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if name and e.get("name") != name:
                continue
            out.append(e)
    out = list(reversed(out))[:limit]
    for e in out:
        files = sorted(set(e.get("before", {})) | set(e.get("after", {})))
        e["diff"] = {f: diff_lines(e["before"].get(f, ""), e["after"].get(f, "")) for f in files
                     if e["before"].get(f, "") != e["after"].get(f, "")}
        e["source_title"] = SOURCE_TITLES.get(e.get("source"), e.get("source"))
    return out


def rollback(name: str, rev: int, mode: str = "to", source: str = "panel") -> dict:
    """mode=to：回到第 rev 版之后的样子；mode=undo：撤销第 rev 版这次修改。"""
    ev = next((e for e in history(name, 10000) if e.get("rev") == rev), None)
    if not ev:
        raise KeyError(f"找不到 {name} 的 r{rev}")
    target = ev["before"] if mode == "undo" else ev["after"]
    with LOCK:
        d = find(name, include_archived=True)
        if not target:  # 撤销「创建」= 归档
            if d and not _is_archived(d):
                return manage("archive", name, source="rollback", note=f"撤销 r{rev}（创建）")
            return {"success": True, "message": "无需处理"}
        if d is None or _is_archived(d):
            if d is not None:
                manage("restore", name, source="rollback")
            d = find(name) or (skills_dir() / name)
        before = _snapshot(d)
        _restore_snapshot(d, target)
        validate_content((d / "SKILL.md").read_text("utf-8"))
        r = _log("rollback", name, before, _snapshot(d), source, f"{'撤销' if mode == 'undo' else '回到'} r{rev}", target_rev=rev)
        return {"success": True, "message": f"已{'撤销' if mode == 'undo' else '回到'} r{rev}，生成 r{r}", "rev": r}


def feedback(name: str, good: bool, comment: str = "", source: str = "owner") -> dict:
    d = find(name)
    if not d:
        raise KeyError(f"技能 {name} 不存在")
    bump(d.name, "good" if good else "bad")
    log_event("skill_feedback", name=d.name, good=bool(good), comment=comment or "", source=source)
    if not good:  # 差评 → 记进待修列表，下一次巡检优先修
        from . import review
        review.mark_needs_fix(d.name, comment)
    return {"success": True, "message": f"已记录{'好评' if good else '差评（下次巡检会优先修复）'}：{d.name}"}


def set_pinned(name: str, pinned: bool) -> dict:
    if not find(name):
        raise KeyError(f"技能 {name} 不存在")
    set_usage(name, pinned=bool(pinned))
    return {"success": True}


def stats() -> dict:
    allv = list_skills(include_archived=True)
    active = [s for s in allv if not s["archived"]]
    return {"total": len(active), "archived": len(allv) - len(active),
            "agent_created": sum(1 for s in active if s["created_by"] == "agent"),
            "pending": len(list_pending())}
