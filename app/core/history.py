"""核心记忆 · 版本历史：完全来自只追加的事件日志 events.jsonl。

每次修改写一条 `core_change` 事件，带：
  rev（新版本号）、source（来源渠道）、actor、note、changed（改了哪些分区）、
  before / after（改动分区的前后内容）、snapshot（修改后三段完整内容）。
因此任意版本都能还原（回到此版本），任意一次修改都能撤销（回到修改前）。
旧版本（v1）留下的 `core_edit` 事件也会被读出来，按单分区处理。
"""
from __future__ import annotations

import difflib
import json

from .. import config
from .store import SECTIONS

CHANGE_TYPES = ("core_change", "core_edit", "core_lock")


def diff_lines(a: str, b: str) -> list[list[str]]:
    """逐行对比：返回 [["=", 行] | ["-", 行] | ["+", 行]]。"""
    al, bl = (a or "").splitlines(), (b or "").splitlines()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, al, bl, autojunk=False).get_opcodes():
        if op == "equal":
            out += [["=", x] for x in al[i1:i2]]
        else:
            out += [["-", x] for x in al[i1:i2]]
            out += [["+", x] for x in bl[j1:j2]]
    return out


def _iter_raw():
    p = config.EVENTS_PATH
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as f:
        for ln in f:
            if '"core_' not in ln:  # 快速过滤，日志很大时也很快
                continue
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if e.get("type") in CHANGE_TYPES:
                yield e


def _normalize(e: dict, legacy_no: list[int]) -> dict:
    t = e.get("type")
    if t == "core_lock":
        return {"kind": "lock", "rev": e.get("rev"), "t": e.get("t"), "ts": e.get("ts"), "locked": e.get("locked"),
                "source": e.get("source", "panel"), "actor": e.get("actor", "owner"), "note": e.get("note", ""),
                "changed": [], "before": {}, "after": {}, "snapshot": None, "rollbackable": False}
    if t == "core_edit":  # v1 旧格式
        legacy_no[0] += 1
        sec = e.get("section")
        return {"kind": "edit", "rev": None, "legacy": True, "legacy_no": legacy_no[0], "t": e.get("t"), "ts": e.get("ts"),
                "source": "v1", "actor": e.get("actor", "owner"), "note": "", "changed": [sec] if sec else [],
                "before": {sec: e.get("old", "")}, "after": {sec: e.get("new", "")}, "snapshot": None, "rollbackable": True}
    return {"kind": e.get("kind", "edit"), "rev": e.get("rev"), "t": e.get("t"), "ts": e.get("ts"),
            "source": e.get("source", ""), "actor": e.get("actor", ""), "note": e.get("note", ""),
            "instruction": e.get("instruction", ""), "changed": e.get("changed", []),
            "before": e.get("before", {}), "after": e.get("after", {}), "snapshot": e.get("snapshot"),
            "rollbackable": True}


def all_entries() -> list[dict]:
    """时间正序的全部核心记忆历史。"""
    n = [0]
    return [_normalize(e, n) for e in _iter_raw()]


def list_history(limit: int = 100, with_diff: bool = True, include_lock: bool = True) -> list[dict]:
    items = all_entries()
    if not include_lock:
        items = [x for x in items if x["kind"] != "lock"]
    items = list(reversed(items))[: max(1, int(limit))]
    if with_diff:
        for x in items:
            x["diff"] = {s: diff_lines(x["before"].get(s, ""), x["after"].get(s, "")) for s in x["changed"]}
            x["changed_titles"] = [SECTIONS.get(s, s) for s in x["changed"]]
    return items


def find_entry(rev: int | None = None, legacy_no: int | None = None) -> dict | None:
    for x in all_entries():
        if rev is not None and x.get("rev") == rev and x["kind"] != "lock":
            return x
        if legacy_no is not None and x.get("legacy_no") == legacy_no:
            return x
    return None


def last_change(exclude_undo_chain: bool = False) -> dict | None:
    for x in reversed(all_entries()):
        if x["kind"] != "lock" and x.get("changed"):
            return x
    return None
