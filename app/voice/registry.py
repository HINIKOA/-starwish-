"""说话人登记簿：一场观看一个，存在 soul/eye/voice/<会话>.json；跨场次的「声纹库」存在 soul/eye/voices/library.json。

只存声纹向量（192 个数字）和名字，不存录音。纯 Python 实现，不依赖 numpy。
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path

from .. import config
from ..db import log_event

_lock = threading.RLock()
OWNER_ID = "owner"
COLORS = ["#7ecbff", "#ff9ec7", "#ffd27a", "#9dffb0", "#c9a7ff", "#ff9f7a", "#7affea", "#f4ff7a", "#b0c4ff", "#ffb3b3"]
NAME_SOURCES = {"dialogue": "台词推断", "visual": "画面", "self_intro": "自我介绍", "owner": "主人", "library": "声纹库"}


class VoiceError(ValueError):
    pass


# ---------------- 向量小工具 ----------------
def norm(v: list[float]) -> list[float]:
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / s for x in v]


def cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) or 1) * (math.sqrt(sum(y * y for y in b)) or 1))


def blend(a: list[float], wa: float, b: list[float], wb: float) -> list[float]:
    t = (wa + wb) or 1.0
    return norm([(x * wa + y * wb) / t for x, y in zip(a, b)])


# ---------------- 片名 → 系列键（同一系列共用声纹库） ----------------
_EP = re.compile(r"(\s*[-_·:：]?\s*(第\s*[0-9一二三四五六七八九十百]+\s*[集话話期季部]|[Ss]\d{1,2}\s*[Ee]\d{1,3}|[Ee][Pp]?\s*\d{1,3}|\(\d{4}\)|（\d{4}）|\d{1,3}))+\s*$")


def series_key(title: str) -> str:
    t = (title or "").strip()
    k = _EP.sub("", t).strip(" -_·:：").lower()
    return k or t.lower()


# ---------------- 声纹库 ----------------
def voices_dir() -> Path:
    d = config.soul_path("eye", "voices")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lib_path() -> Path:
    return voices_dir() / "library.json"


def load_library() -> dict:
    p = _lib_path()
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"titles": {}, "owner": None}


def save_library(lib: dict) -> None:
    with _lock:
        p = _lib_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(lib, ensure_ascii=False), "utf-8")
        tmp.replace(p)


def library_public() -> dict:
    lib = load_library()
    titles = []
    for k, v in sorted(lib.get("titles", {}).items()):
        titles.append({"key": k, "title": v.get("title", k), "updated": v.get("updated"),
                       "speakers": [{"name": s["name"], "label": s.get("label", ""), "gender": s.get("gender", "")} for s in v.get("speakers", [])]})
    o = lib.get("owner")
    return {"titles": titles, "owner": {"enrolled": True, "enrolled_at": o.get("enrolled_at"), "seconds": o.get("seconds")} if o else {"enrolled": False}}


def library_delete(key: str) -> bool:
    lib = load_library()
    if key in lib.get("titles", {}):
        del lib["titles"][key]
        save_library(lib)
        log_event("voice_library_delete", key=key)
        return True
    return False


def set_owner(emb: list[float], seconds: float) -> dict:
    lib = load_library()
    lib["owner"] = {"centroid": norm(emb), "seconds": round(seconds, 1), "enrolled_at": time.time()}
    save_library(lib)
    log_event("voice_owner_enroll", seconds=round(seconds, 1))
    return library_public()["owner"]


def clear_owner() -> None:
    lib = load_library()
    lib["owner"] = None
    save_library(lib)
    log_event("voice_owner_clear")


# ---------------- 一场观看的登记簿 ----------------
class Registry:
    def __init__(self, sid: str, data: dict):
        self.sid = sid
        self.d = data

    # ---- 存取 ----
    @staticmethod
    def path(sid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f\-]{6,40}", sid or ""):
            raise VoiceError("会话编号不对")
        d = config.soul_path("eye", "voice")
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{sid}.json"

    @classmethod
    def exists(cls, sid: str) -> bool:
        try:
            return cls.path(sid).exists()
        except VoiceError:
            return False

    @classmethod
    def load(cls, sid: str, title: str = "", use_library: bool = True) -> "Registry":
        p = cls.path(sid)
        if p.exists():
            return cls(sid, json.loads(p.read_text("utf-8")))
        r = cls(sid, {"sid": sid, "title": title, "series": series_key(title), "speakers": {}, "counters": {},
                      "timeline": [], "chunks": 0, "lines_since_identify": 0, "merges": [], "created": time.time()})
        if use_library:
            r._preload_library()
        r.save()
        return r

    def save(self) -> None:
        with _lock:
            p = self.path(self.sid)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d, ensure_ascii=False), "utf-8")
            tmp.replace(p)

    def _preload_library(self) -> None:
        lib = load_library()
        ent = lib.get("titles", {}).get(self.d["series"])
        for s in (ent or {}).get("speakers", []):
            spk = self._new(s.get("gender", ""), centroid=s["centroid"], label=s.get("label") or None)
            spk.update(name=s["name"], name_status="confirmed", from_library=True, n=min(int(s.get("n", 5)), 20))
            spk["proposals"].append({"name": s["name"], "source": "library", "evidence": "上次看这部片时认识的", "t": time.time(), "votes": 1})

    # ---- 说话人 ----
    @property
    def speakers(self) -> dict:
        return self.d["speakers"]

    def active(self) -> list[dict]:
        return [s for s in self.speakers.values() if not s.get("merged_into")]

    def resolve(self, sid_or_label: str) -> dict | None:
        """按编号 / 标签 / 名字找人，顺着合并链找到现在的那个人。"""
        key = (sid_or_label or "").strip()
        s = self.speakers.get(key)
        if not s:
            for x in self.speakers.values():
                if key and (x["label"] == key or (x.get("name") and x["name"] == key)):
                    s = x
                    if not x.get("merged_into"):
                        break
        seen = 0
        while s and s.get("merged_into") and seen < 20:
            s = self.speakers.get(s["merged_into"])
            seen += 1
        return s

    def _new(self, gender: str, centroid: list[float], label: str | None = None, owner: bool = False) -> dict:
        if owner:
            sid, label = OWNER_ID, "主人"
        else:
            g = gender or "人"
            c = self.d["counters"]
            if label and re.fullmatch(r"(男|女|人)(\d+)", label):
                g, k = label[0], int(label[1:])
                c[g] = max(c.get(g, 0), k)
            else:
                c[g] = c.get(g, 0) + 1
                label = f"{g}{c[g]}"
            sid = f"s{len(self.speakers) + 1}"
            while sid in self.speakers:
                sid += "x"
        spk = {"id": sid, "label": label, "gender": gender, "name": "", "name_status": "", "proposals": [],
               "centroid": norm(centroid), "n": 0, "seconds": 0.0, "segments": 0, "f0": [],
               "first_t": None, "last_t": None, "merged_into": None, "owner": owner,
               "color": "#ffd27a" if owner else COLORS[(len(self.speakers)) % len(COLORS)]}
        self.speakers[sid] = spk
        return spk

    def display(self, spk: dict | str | None) -> str:
        if isinstance(spk, str):
            spk = self.resolve(spk)
        if not spk:
            return "？"
        if spk.get("owner"):
            return "主人"
        if spk.get("name") and spk.get("name_status") == "confirmed":
            return spk["name"]
        best = self.best_proposal(spk)
        if best:
            return f"{spk['label']}（可能是{best['name']}）"
        return spk["label"]

    @staticmethod
    def best_proposal(spk: dict) -> dict | None:
        ps = [p for p in spk.get("proposals", []) if not p.get("rejected")]
        return max(ps, key=lambda p: (p.get("votes", 1), p.get("t", 0))) if ps else None

    # ---- 匹配 ----
    def match(self, emb: list[float], seconds: float, f0: float | None, vcfg: dict, t: float | None = None) -> tuple[dict, float, bool]:
        """返回 (说话人, 相似度, 是否新建)。"""
        t = t or time.time()
        emb = norm(emb)
        thr = float(vcfg.get("match_threshold", 0.5))
        min_learn = float(vcfg.get("min_learn_sec", 1.5))
        split = float(vcfg.get("gender_split_hz", 165))
        lib_owner = load_library().get("owner") if vcfg.get("owner_detect", True) else None
        best, score = None, -1.0
        for s in self.active():
            c = cos(emb, s["centroid"])
            if c > score:
                best, score = s, c
        if lib_owner:
            oc = cos(emb, lib_owner["centroid"])
            if oc >= float(vcfg.get("owner_threshold", 0.55)) and oc >= score:
                best = self.speakers.get(OWNER_ID) or self._new("", lib_owner["centroid"], owner=True)
                score = oc
        created = False
        if best is None or score < thr:
            from .audio import gender_of
            best = self._new(gender_of(f0, split) if f0 else "", emb)
            created, score = True, 1.0
            log_event("voice_new_speaker", sid=self.sid, speaker=best["id"], label=best["label"])
        # 只用够长的句子学习，避免短句、抢话把声纹带偏
        if created or seconds >= min_learn:
            w = min(best["n"], 50) or 0
            best["centroid"] = emb if w == 0 else blend(best["centroid"], w, emb, 1.0)
            best["n"] += 1
        if f0:
            best["f0"] = (best["f0"] + [round(f0, 1)])[-30:]
            if not best.get("owner") and len(best["f0"]) >= 3:
                from .audio import gender_of
                best["gender"] = gender_of(sorted(best["f0"])[len(best["f0"]) // 2], split)
        best["seconds"] = round(best["seconds"] + seconds, 2)
        best["segments"] += 1
        best["first_t"] = best["first_t"] or t
        best["last_t"] = t
        return best, round(score, 3), created

    def add_timeline(self, spk_id: str, t: float, dur: float) -> None:
        tl = self.d["timeline"]
        tl.append({"spk": spk_id, "t": round(t, 2), "dur": round(dur, 2)})
        del tl[:-300]

    def recent_speakers(self, seconds: float = 30, now: float | None = None) -> list[tuple[dict, float, float]]:
        """最近 seconds 秒内说过话的人：[(说话人, 距今开始秒, 距今结束秒)]，按时间排序、合并相邻同一人。"""
        now = now or time.time()
        out: list[list] = []
        for e in self.d["timeline"]:
            if now - (e["t"] + e["dur"]) > seconds:
                continue
            s = self.resolve(e["spk"])
            if not s:
                continue
            a, b = now - e["t"], now - e["t"] - e["dur"]
            if out and out[-1][0]["id"] == s["id"]:
                out[-1][2] = b
            else:
                out.append([s, a, b])
        return [(s, round(max(0, a), 1), round(max(0, b), 1)) for s, a, b in out]

    # ---- 重新聚类：把被拆开的同一个人合并 ----
    def recluster(self, vcfg: dict) -> list[dict]:
        thr = float(vcfg.get("merge_threshold", 0.65))
        merges = []
        changed = True
        while changed:
            changed = False
            act = [s for s in self.active() if not s.get("owner")]
            best = None
            for i in range(len(act)):
                for j in range(i + 1, len(act)):
                    c = cos(act[i]["centroid"], act[j]["centroid"])
                    if c >= thr and (best is None or c > best[0]):
                        best = (c, act[i], act[j])
            if best:
                c, a, b = best
                keep, drop = (a, b) if (a["seconds"], -int(a["id"][1:].rstrip("x") or 0)) >= (b["seconds"], -int(b["id"][1:].rstrip("x") or 0)) else (b, a)
                self._merge(keep, drop, reason="auto", score=c)
                merges.append({"keep": keep["id"], "drop": drop["id"], "score": round(c, 3), "keep_label": keep["label"], "drop_label": drop["label"]})
                changed = True
        return merges

    def _merge(self, keep: dict, drop: dict, reason: str, score: float | None = None) -> None:
        if keep["id"] == drop["id"]:
            raise VoiceError("不能和自己合并")
        keep["centroid"] = blend(keep["centroid"], max(1, keep["n"]), drop["centroid"], max(1, drop["n"]))
        keep["n"] += drop["n"]
        keep["seconds"] = round(keep["seconds"] + drop["seconds"], 2)
        keep["segments"] += drop["segments"]
        keep["f0"] = (keep["f0"] + drop["f0"])[-30:]
        keep["first_t"] = min(x for x in (keep["first_t"], drop["first_t"]) if x) if (keep["first_t"] or drop["first_t"]) else None
        keep["last_t"] = max(x or 0 for x in (keep["last_t"], drop["last_t"])) or None
        if drop.get("name_status") == "confirmed" and keep.get("name_status") != "confirmed":
            keep["name"], keep["name_status"] = drop["name"], "confirmed"
        for p in drop.get("proposals", []):
            self._vote(keep, p["name"], p.get("source", "dialogue"), p.get("evidence", ""), votes=p.get("votes", 1), rejected=p.get("rejected", False))
        drop["merged_into"] = keep["id"]
        self.d["merges"].append({"keep": keep["id"], "drop": drop["id"], "reason": reason, "score": score, "t": time.time()})
        log_event("voice_merge", sid=self.sid, keep=keep["label"], drop=drop["label"], reason=reason, score=score)

    def merge(self, keep_id: str, drop_id: str) -> dict:
        keep, drop = self.resolve(keep_id), self.resolve(drop_id)
        if not keep or not drop:
            raise VoiceError("找不到要合并的人")
        if keep.get("owner") or drop.get("owner"):
            raise VoiceError("主人不参与合并")
        self._merge(keep, drop, reason="owner")
        return keep

    # ---- 名字：提名、投票、确认 ----
    def _vote(self, spk: dict, name: str, source: str, evidence: str = "", votes: int = 1, rejected: bool = False) -> dict:
        name = name.strip()[:20]
        for p in spk["proposals"]:
            if p["name"] == name:
                if rejected:
                    p["rejected"] = True
                if source not in p.setdefault("sources", [p.get("source")]):
                    p["sources"].append(source)
                p["votes"] = p.get("votes", 1) + votes
                p["t"] = time.time()
                if evidence:
                    p["evidence"] = evidence[:120]
                return p
        p = {"name": name, "source": source, "sources": [source], "evidence": evidence[:120], "votes": votes, "t": time.time()}
        if rejected:
            p["rejected"] = True
        spk["proposals"].append(p)
        return p

    def propose(self, who: str, name: str, source: str, evidence: str, vcfg: dict) -> dict | None:
        """一路线索提名。两路线索都提到同一个名字（或累计票数够了）就自动确认。返回变化说明或 None。"""
        spk = self.resolve(who)
        name = re.sub(r"[\s，。、“”\"'【】（）()]", "", name or "")[:20]
        if not spk or not name or spk.get("owner") or name in ("主人", spk["label"]):
            return None
        if spk.get("name_status") == "confirmed":
            if spk["name"] == name:
                return None
            if not vcfg.get("allow_override", False):
                self._vote(spk, name, source, evidence)
                return {"speaker": spk["id"], "label": spk["label"], "name": name, "status": "kept", "current": spk["name"]}
        # 同一个名字已经被别人确认过 → 只记一票，不抢名字
        holder = next((s for s in self.active() if s["id"] != spk["id"] and s.get("name") == name and s.get("name_status") == "confirmed"), None)
        p = self._vote(spk, name, source, evidence)
        if p.get("rejected"):
            return {"speaker": spk["id"], "label": spk["label"], "name": name, "status": "rejected_before"}
        need = int(vcfg.get("auto_accept_votes", 2))
        status = "proposed"
        if not holder and need > 0 and (p["votes"] >= need or len(set(p.get("sources", []))) >= 2):
            spk["name"], spk["name_status"] = name, "confirmed"
            status = "confirmed"
        elif not spk.get("name_status"):
            spk["name"], spk["name_status"] = name, "proposed"
        elif spk.get("name_status") == "proposed":
            best = self.best_proposal(spk)
            spk["name"] = best["name"] if best else name
        log_event("voice_name", sid=self.sid, speaker=spk["label"], name=name, source=source, status=status)
        return {"speaker": spk["id"], "label": spk["label"], "name": name, "status": status, "source": source}

    def set_name(self, who: str, name: str) -> dict:
        spk = self.resolve(who)
        if not spk:
            raise VoiceError("找不到这个人")
        name = (name or "").strip()[:20]
        if not name:
            spk["name"], spk["name_status"] = "", ""
        else:
            self._vote(spk, name, "owner", "主人指定")
            spk["name"], spk["name_status"] = name, "confirmed"
        log_event("voice_name", sid=self.sid, speaker=spk["label"], name=name, source="owner", status="confirmed")
        return spk

    def reject(self, who: str, name: str | None = None) -> dict:
        spk = self.resolve(who)
        if not spk:
            raise VoiceError("找不到这个人")
        name = name or spk.get("name")
        for p in spk["proposals"]:
            if p["name"] == name:
                p["rejected"] = True
        if spk.get("name") == name:
            best = self.best_proposal(spk)
            spk["name"], spk["name_status"] = (best["name"], "proposed") if best else ("", "")
        return spk

    # ---- 对外 ----
    def roster_text(self) -> str:
        rows = []
        for s in sorted(self.active(), key=lambda x: x.get("first_t") or 0):
            if s.get("owner"):
                rows.append("主人（拿着遥控器的那位）")
                continue
            if s.get("name_status") == "confirmed":
                rows.append(f"{s['label']}={s['name']}")
            elif self.best_proposal(s):
                rows.append(f"{s['label']}（可能叫{self.best_proposal(s)['name']}）")
            else:
                rows.append(f"{s['label']}（还不知道名字）")
        return "；".join(rows)

    def public(self) -> dict:
        sp = []
        for s in sorted(self.active(), key=lambda x: (not x.get("owner"), x.get("first_t") or 0)):
            sp.append({k: s.get(k) for k in ("id", "label", "gender", "name", "name_status", "seconds", "segments", "color",
                                              "owner", "first_t", "last_t", "from_library")}
                      | {"display": self.display(s), "f0": (sorted(s["f0"])[len(s["f0"]) // 2] if s["f0"] else None),
                         "proposals": [{k: p.get(k) for k in ("name", "sources", "evidence", "votes", "rejected")} for p in s["proposals"]]})
        return {"sid": self.sid, "title": self.d.get("title"), "series": self.d.get("series"), "speakers": sp,
                "merges": self.d["merges"][-20:], "chunks": self.d["chunks"]}

    def save_to_library(self) -> int:
        """把这一场确认了名字的人存进声纹库（按系列）。返回存了几个人。"""
        named = [s for s in self.active() if s.get("name_status") == "confirmed" and s.get("name") and not s.get("owner")]
        if not named:
            return 0
        lib = load_library()
        ent = lib.setdefault("titles", {}).setdefault(self.d["series"], {"title": self.d.get("title"), "speakers": []})
        ent["title"], ent["updated"] = self.d.get("title"), time.time()
        for s in named:
            old = next((x for x in ent["speakers"] if x["name"] == s["name"]), None)
            if old:
                old["centroid"] = blend(old["centroid"], max(1, old.get("n", 1)), s["centroid"], max(1, s["n"]))
                old["n"] = min(200, old.get("n", 1) + s["n"])
                old["gender"] = s.get("gender") or old.get("gender", "")
            else:
                ent["speakers"].append({"name": s["name"], "label": s["label"], "gender": s.get("gender", ""),
                                        "centroid": s["centroid"], "n": s["n"]})
        save_library(lib)
        log_event("voice_library_save", series=self.d["series"], count=len(named))
        return len(named)
