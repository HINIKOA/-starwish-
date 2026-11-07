"""导入器：星汐(dsh-xingxi) 档案文件夹、普通 markdown/txt、ChatGPT conversations.json。

星汐档案格式（参考 https://github.com/Yuuyuko-uu/dsh-xingxi ，作者 Yuuyuko-uu）：
  记忆卡.md        —— 八章，`## 一｜我是谁`、`## 三｜对方是谁` …，每条 `- ` 一颗星
  日记/YYYY-MM-DD.md —— 一天一颗星
  原始对话/YYYY-MM-DD.txt —— 当天原话，永久留底
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import archive, core, stars
from .db import log_event

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
CH_RE = re.compile(r"^#{2,4}\s+([一二三四五六七八])\s*[、.．｜|·:：\-—]\s*(.*)$")
DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.．、]\s*|\*\*\d+[.．]\s*)")
PREF_RE = re.compile(r"喜欢|爱吃|爱喝|偏好|讨厌|不喜欢|不吃|过敏|习惯|口味|最爱")


def _ts_from_name(name: str) -> float | None:
    m = DATE_RE.search(name)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0).timestamp()
    except ValueError:
        return None


def _read(p: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return p.read_text(enc)
        except Exception:
            continue
    return p.read_text("utf-8", errors="replace")


def parse_memory_card(text: str) -> list[dict]:
    """把记忆卡拆成章：[{num, name, intro_skipped_text, items:[str], paragraph:str}]"""
    chapters: list[dict] = []
    cur = None
    for line in text.splitlines():
        m = CH_RE.match(line.strip())
        if m:
            cur = {"num": CN_NUM[m.group(1)], "name": (m.group(2) or "").strip() or f"第{m.group(1)}章", "lines": []}
            chapters.append(cur)
            continue
        if cur is None:
            continue
        if re.match(r"^\s*这一章讲什么", line) or line.strip().startswith(">"):
            continue
        cur["lines"].append(line)
    for ch in chapters:
        items, para, buf = [], [], None
        for ln in ch["lines"]:
            if BULLET_RE.match(ln):
                if buf:
                    items.append(buf.strip())
                buf = BULLET_RE.sub("", ln, count=1)
            elif ln.strip() and buf is not None and (ln.startswith("  ") or ln.startswith("\t")):
                buf += "\n" + ln.strip()
            elif ln.strip():
                if buf:
                    items.append(buf.strip())
                    buf = None
                para.append(ln.strip())
            # 空行忽略
        if buf:
            items.append(buf.strip())
        ch["items"] = [re.sub(r"\*\*", "", i).strip() for i in items if i.strip()]
        ch["paragraph"] = "\n".join(p for p in para if not p.startswith("#")).strip()
        ch["full"] = "\n".join(ln for ln in ch["lines"]).strip()
    return chapters


def _is_identity(ch) -> bool:
    return ch["num"] == 1 or "我是谁" in ch["name"]


def _is_owner(ch) -> bool:
    return "对方" in ch["name"] or "主人" in ch["name"] or (ch["num"] == 3 and "对方" not in ch["name"] and "谁" in ch["name"])


def import_xingxi(folder: str | Path) -> dict:
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在：{folder}")
    res = {"stars": 0, "duplicates": 0, "proposals": 0, "diaries": 0, "raw_files": 0, "cards": 0}

    def _add(content, **kw):
        r = stars.add_star(content, actor="import", **kw)
        res["duplicates" if r.get("duplicate") else "stars"] += 1

    cards = [p for p in folder.rglob("*.md") if "记忆卡" in p.name and "日记" not in p.parts]
    for card in cards:
        res["cards"] += 1
        text = _read(card)
        rel = card.relative_to(folder).as_posix()
        card_ts = card.stat().st_mtime
        for ch in parse_memory_card(text):
            tag = ch["name"][:16]
            if _is_identity(ch):
                body = ch["full"].strip()
                body = "\n".join(l for l in body.splitlines() if not re.match(r"^\s*这一章讲什么", l)).strip()
                if body:
                    core.create_proposal("identity", body, f"导入自星汐记忆卡「一｜{ch['name']}」", f"import:xingxi:{rel}", mode="replace")
                    res["proposals"] += 1
                continue
            if _is_owner(ch):
                owner_txt = ch["paragraph"] or "\n".join(i for i in ch["items"] if not PREF_RE.search(i))
                if owner_txt.strip():
                    core.create_proposal("owner", owner_txt, f"导入自星汐记忆卡「{ch['name']}」", f"import:xingxi:{rel}", mode="append")
                    res["proposals"] += 1
                prefs = [i for i in ch["items"] if PREF_RE.search(i)]
                if prefs:
                    core.create_proposal("preferences", "\n".join("- " + p for p in prefs),
                                         f"导入自星汐记忆卡「{ch['name']}」中提到喜好的条目", f"import:xingxi:{rel}", mode="append")
                    res["proposals"] += 1
            pieces = ch["items"] or ([ch["paragraph"]] if ch["paragraph"] else [])
            for it in pieces:
                ts = _ts_from_name(it) or card_ts
                _add(it, tags=[tag, "记忆卡"], importance=4 if _is_owner(ch) else 3,
                     source=f"import:xingxi:{rel}#{ch['num']}", created_at=ts)

    diary_dir = next((d for d in folder.rglob("日记") if d.is_dir()), None)
    if diary_dir:
        for p in sorted(diary_dir.glob("*.md")):
            text = _read(p).strip()
            if not text:
                continue
            ts = _ts_from_name(p.stem)
            body = re.sub(r"^【\d{4}-\d{2}-\d{2}】\s*", "", text).strip()
            _add(body, summary=f"{p.stem} 日记：" + stars.auto_summary(body, 40), tags=["日记"],
                 importance=3, source=f"import:xingxi:日记/{p.name}", created_at=ts)
            res["diaries"] += 1

    raw_dir = next((d for d in folder.rglob("原始对话") if d.is_dir()), None)
    if raw_dir:
        for p in sorted(raw_dir.glob("*.txt")):
            archive.append_raw_file(f"xingxi/原始对话/{p.name}", _read(p))
            res["raw_files"] += 1
    log_event("import_xingxi", folder=str(folder), **res)
    return res


def import_xingxi_zip(data: bytes) -> dict:
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                name = info.filename
                # 兼容 Windows 压缩工具写入的 GBK 文件名
                if not (info.flag_bits & 0x800):
                    try:
                        name = name.encode("cp437").decode("gbk")
                    except Exception:
                        pass
                target = (Path(td) / name).resolve()
                if Path(td).resolve() not in target.parents:
                    continue
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
        return import_xingxi(td)


def import_text(text: str, filename: str = "note.md", tags=None) -> dict:
    """普通 markdown / txt：按列表项与段落拆星；标题作为标签。"""
    res = {"stars": 0, "duplicates": 0}
    ts = _ts_from_name(filename)
    heading = ""
    paras: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush():
        if buf:
            t = "\n".join(buf).strip()
            if t:
                paras.append((heading, t))
            buf.clear()

    for ln in text.splitlines():
        h = re.match(r"^#{1,6}\s+(.*)$", ln)
        if h:
            flush()
            heading = h.group(1).strip()
            continue
        if BULLET_RE.match(ln):
            flush()
            buf.append(BULLET_RE.sub("", ln, count=1))
            flush()
            continue
        if not ln.strip():
            flush()
            continue
        buf.append(ln)
    flush()
    base_tags = list(tags or []) + [Path(filename).stem[:16]]
    for head, t in paras:
        if len(t) < 2:
            continue
        r = stars.add_star(t, tags=([head[:16]] if head else []) + base_tags, source=f"import:file:{filename}",
                           created_at=ts, actor="import")
        res["duplicates" if r.get("duplicate") else "stars"] += 1
    log_event("import_text", file=filename, **res)
    return res


def import_chatgpt(data: bytes | str) -> dict:
    """ChatGPT 导出的 conversations.json：每段对话原文进 archive（待 LLM 整理），并生成一颗标题星。"""
    convs = json.loads(data)
    if isinstance(convs, dict):
        convs = convs.get("conversations", [convs])
    res = {"conversations": 0, "messages": 0, "stars": 0, "duplicates": 0}
    for conv in convs:
        mapping = conv.get("mapping") or {}
        msgs = []
        for node in mapping.values():
            m = (node or {}).get("message") or {}
            role = (m.get("author") or {}).get("role")
            if role not in ("user", "assistant"):
                continue
            parts = (m.get("content") or {}).get("parts") or []
            txt = "\n".join(p for p in parts if isinstance(p, str)).strip()
            if not txt:
                continue
            msgs.append({"t": m.get("create_time") or conv.get("create_time") or 0, "role": role, "content": txt})
        if not msgs:
            continue
        msgs.sort(key=lambda m: m["t"] or 0)
        ct = conv.get("create_time") or msgs[0]["t"] or None
        cid = (conv.get("id") or conv.get("conversation_id") or str(int(ct or 0)))[:40]
        rel = archive.append_raw_file(f"chatgpt/{cid}.jsonl",
                                      "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs))
        title = (conv.get("title") or "").strip() or "ChatGPT 对话"
        first_user = next((m["content"] for m in msgs if m["role"] == "user"), "")
        r = stars.add_star(f"[ChatGPT 对话] {title}\n主人问：{first_user[:300]}", summary=f"ChatGPT对话：{title}"[:60],
                           tags=["chatgpt"], importance=2, source=f"archive:{rel}", created_at=ct, actor="import")
        res["duplicates" if r.get("duplicate") else "stars"] += 1
        res["conversations"] += 1
        res["messages"] += len(msgs)
    log_event("import_chatgpt", **res)
    return res


def import_path(path: str, kind: str = "auto") -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{p}")
    if p.is_dir():
        if kind in ("auto", "xingxi"):
            if any("记忆卡" in x.name for x in p.rglob("*.md")) or (p / "日记").is_dir() or kind == "xingxi":
                return {"kind": "xingxi", **import_xingxi(p)}
        total = {"kind": "folder", "stars": 0, "duplicates": 0, "files": 0}
        for f in sorted(list(p.rglob("*.md")) + list(p.rglob("*.txt"))):
            r = import_text(_read(f), f.name)
            total["stars"] += r["stars"]
            total["duplicates"] += r["duplicates"]
            total["files"] += 1
        return total
    data = p.read_bytes()
    return import_bytes(data, p.name, kind)


def import_bytes(data: bytes, filename: str, kind: str = "auto") -> dict:
    low = filename.lower()
    if kind == "chatgpt" or (kind == "auto" and low.endswith(".json")):
        return {"kind": "chatgpt", **import_chatgpt(data)}
    if kind == "xingxi" or (kind == "auto" and low.endswith(".zip")):
        return {"kind": "xingxi", **import_xingxi_zip(data)}
    text = data.decode("utf-8-sig", errors="replace")
    if kind == "auto" and "记忆卡" in filename:
        tmp_res = {"kind": "xingxi-card"}
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "记忆卡.md").write_text(text, "utf-8")
            tmp_res.update(import_xingxi(td))
        return tmp_res
    return {"kind": "text", **import_text(text, filename)}
