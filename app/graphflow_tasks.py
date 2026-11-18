"""Workspace-local workflow plans and explicit outcome feedback."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager

from . import graphflow, graphflow_edits, graphflow_tests, graphflow_workflows
from .graphflow import GraphFlowError
from .graphflow_edit_queue import state_path

ENV = "STARWISH_GRAPHFLOW_TASKS"
LEGACY_IMPORT_PREFIX = "legacy-import-"
MAX_OPEN = 100
OLD_SECONDS = 86400
_TRANSITIONS = {"pending": {"blocked", "completed"}, "blocked": {"pending", "completed"}, "completed": set()}


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


def _workspace():
    if not enabled():
        raise GraphFlowError("未启用项目任务台账。", 503)
    return graphflow._workspace()


@contextmanager
def _db(workspace):
    try:
        with closing(sqlite3.connect(state_path(workspace, "-tasks"), timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                plan TEXT NOT NULL, inputs TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_events (
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL, state TEXT NOT NULL, summary TEXT NOT NULL,
                evidence TEXT NOT NULL, source TEXT NOT NULL, at REAL NOT NULL,
                PRIMARY KEY(task_id, revision)
            )""")
            db.execute("BEGIN IMMEDIATE")
            yield db
    except (OSError, sqlite3.Error) as exc:
        raise GraphFlowError("本机项目任务台账不可用。", 503) from exc


@contextmanager
def read_ledger(workspace):
    try:
        path = state_path(workspace, "-tasks", create=False)
        if not path.exists() and not path.is_symlink():
            yield None
            return
        if path.resolve() != path or not path.is_file():
            raise OSError("Unexpected task ledger path")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            yield db
    except GraphFlowError:
        raise
    except (OSError, sqlite3.Error, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise GraphFlowError("本机项目任务台账不可用。", 503) from exc


def _event(db, task_id, revision, state, source, now, summary="", evidence=()):
    db.execute("INSERT INTO task_events VALUES(?,?,?,?,?,?,?)",
               (task_id, revision, state, summary, json.dumps(evidence, ensure_ascii=False), source, now))


def _view(row) -> dict:
    return {key: row[key] for key in ("id", "state", "revision", "created", "updated")}


def _find(db, task_id: str):
    if not valid_task_id(task_id):
        raise GraphFlowError("任务编号无效。")
    row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone() if db is not None else None
    if row is None:
        raise GraphFlowError("当前工作区未找到该任务。", 404)
    return row


def valid_task_id(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def require_verification_task(workspace, task_id: str) -> None:
    if not enabled():
        raise GraphFlowError("未启用项目任务台账。", 503)
    with read_ledger(workspace) as db:
        _find(db, task_id)


def record_verification(workspace, task_id: str, run_id: str) -> dict:
    evidence = graphflow_tests.evidence_record(workspace, run_id)
    with _db(workspace) as db:
        _find(db, task_id)
        db.execute("""CREATE TABLE IF NOT EXISTS task_verifications (
            run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
            profile TEXT NOT NULL, state TEXT NOT NULL, exit_code INTEGER, recorded_at REAL NOT NULL)""")
        db.execute("""CREATE INDEX IF NOT EXISTS task_verifications_by_task
                      ON task_verifications(task_id, recorded_at DESC, run_id)""")
        db.execute("""INSERT INTO task_verifications VALUES(?,?,?,?,?,?) ON CONFLICT(run_id) DO NOTHING""",
                   (run_id, task_id, evidence["profile"], evidence["state"], evidence["exit_code"], time.time()))
        item = db.execute("SELECT * FROM task_verifications WHERE run_id=?", (run_id,)).fetchone()
        if item["task_id"] != task_id:
            raise GraphFlowError("此测试已与另一项目任务关联。", 409)
        return dict(item)


def _has_verifications(db) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_verifications'").fetchone() is not None


def _verifications(db, task_id: str, limit: int) -> list[dict]:
    rows = db.execute("""SELECT run_id,profile,state,exit_code,recorded_at FROM task_verifications WHERE task_id=?
                         ORDER BY recorded_at DESC,run_id LIMIT ?""", (task_id, limit))
    return [dict(row) for row in rows]


def latest_verifications(db, task_ids: list[str]) -> dict[str, dict]:
    if not task_ids or not _has_verifications(db):
        return {}
    slots = ",".join("?" for _ in task_ids)
    rows = db.execute(f"""SELECT v.task_id,v.run_id,v.profile,v.state,v.exit_code,v.recorded_at
                         FROM tasks t JOIN task_verifications v ON v.run_id=(
                             SELECT run_id FROM task_verifications WHERE task_id=t.id
                             ORDER BY recorded_at DESC,run_id LIMIT 1)
                         WHERE t.id IN ({slots})""", task_ids)
    result = {}
    for row in rows:
        item = dict(row)
        result[item.pop("task_id")] = item
    return result


def topic_read_inputs(workspace, task_id: str) -> tuple[str, str]:
    if not enabled():
        raise GraphFlowError("未启用项目任务台账。", 503)
    with read_ledger(workspace) as db:
        inputs = json.loads(_find(db, task_id)["inputs"])
        return inputs["target"], inputs["query"]


def create(request_id: str, name: str, plan_sha256: str, target: str = "", query: str = "", *,
           source="mcp", initial_summary="") -> dict:
    workspace = _workspace()
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
        raise GraphFlowError("请求编号须为 8–80 位字母、数字、下划线或连字符。")
    if request_id.startswith(LEGACY_IMPORT_PREFIX) and source != "legacy_import":
        raise GraphFlowError("该请求编号前缀保留给旧任务迁移。")
    inputs = {"name": name, "target": target, "query": query, "plan_sha256": plan_sha256}
    if (any(not isinstance(value, str) for value in inputs.values()) or len(name) > 40
            or len(target) > 500 or len(query) > 300 or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256)):
        raise GraphFlowError("任务计划参数格式不正确。")
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    with _db(workspace) as db:
        existing = db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        if existing is not None:
            if source == "legacy_import":
                origin = db.execute("SELECT source FROM task_events WHERE task_id=? AND revision=0",
                                    (existing["id"],)).fetchone()
                if origin is None or origin["source"] != source:
                    raise GraphFlowError("该迁移编号已被其他任务占用。", 409)
            if existing["fingerprint"] != fingerprint:
                raise GraphFlowError("该请求编号已关联另一份计划。", 409)
            return {**_view(existing), "duplicate": True}
        if db.execute("SELECT count(*) FROM tasks WHERE state!='completed'").fetchone()[0] >= MAX_OPEN:
            raise GraphFlowError("未完成任务太多，请先处理积压。", 429)
        proposal = graphflow_workflows.preview(name, target, query)
        if proposal["plan_sha256"] != plan_sha256:
            raise GraphFlowError("工作流配置或参数已变化，请重新预览。", 409)
        task_id, now = secrets.token_hex(16), time.time()
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)", (
            task_id, request_id, fingerprint, json.dumps(proposal, ensure_ascii=False),
            json.dumps(inputs, ensure_ascii=False), "pending", 0, now, now,
        ))
        _event(db, task_id, 0, "pending", source, now, initial_summary)
        return {**_view(_find(db, task_id)), "duplicate": False}


def detail(task_id: str) -> dict:
    with read_ledger(_workspace()) as db:
        row = _find(db, task_id)
        events = db.execute("""SELECT revision,state,summary,evidence,source,at FROM task_events
                             WHERE task_id=? ORDER BY revision DESC LIMIT 12""", (task_id,)).fetchall()
        return {**_view(row), "plan": json.loads(row["plan"]), "inputs": json.loads(row["inputs"]),
                "events": [{**dict(event), "evidence": json.loads(event["evidence"])} for event in events],
                "test_verifications": _verifications(db, task_id, 10) if _has_verifications(db) else []}


def backlog() -> dict:
    now = time.time()
    counts = {"pending": 0, "blocked": 0}
    with read_ledger(_workspace()) as db:
        if db is None:
            return {"counts": counts, "older_than_24h": 0, "items": [], "more": False}
        counts.update(dict(db.execute("SELECT state,count(*) FROM tasks WHERE state!='completed' GROUP BY state").fetchall()))
        old = db.execute("SELECT count(*) FROM tasks WHERE state!='completed' AND created<=?",
                         (now - OLD_SECONDS,)).fetchone()[0]
        rows = db.execute("SELECT * FROM tasks WHERE state!='completed' ORDER BY created,id LIMIT 20").fetchall()
        return {"counts": counts, "older_than_24h": old,
                "items": [{**_view(row), "workflow": json.loads(row["plan"])["name"]} for row in rows],
                "more": sum(counts.values()) > len(rows)}


def recent() -> dict:
    limit = 20
    with read_ledger(_workspace()) as db:
        rows = db.execute("SELECT * FROM tasks ORDER BY updated DESC,id LIMIT ?", (limit + 1,)).fetchall() if db is not None else []
        return {"items": [{**_view(row), "workflow": json.loads(row["plan"])["name"]} for row in rows[:limit]],
                "more": len(rows) > limit, "read_only": True}


def replay_task(task_id: str, plan_sha256: str = "", *, budget: int = 12000) -> dict:
    with read_ledger(_workspace()) as db:
        row = _find(db, task_id)
        task = _view(row)
        inputs = json.loads(row["inputs"])
        saved = json.loads(row["plan"])
        latest = db.execute("SELECT revision,state,summary,evidence FROM task_events WHERE task_id=? AND revision=?",
                            (task_id, task["revision"])).fetchone()
    if not plan_sha256:
        outcome = {**dict(latest), "evidence": json.loads(latest["evidence"])} if latest else None
        return {"task": task, "plan": saved, "latest_event": outcome, "state": "preview", "task_updated": False,
                "files_written": False, "test_executed": False}
    if plan_sha256 != saved["plan_sha256"]:
        raise GraphFlowError("宏重放需要此任务保存的计划哈希。", 409)
    report = graphflow_workflows.replay(**inputs, budget=budget)
    return {**report, "task": task, "task_updated": False}


def _annotate_code_evidence(workspace, outcomes: list[dict]) -> None:
    digests = {}
    for outcome in outcomes:
        for reference in outcome["evidence"]:
            if reference.get("kind") != "code":
                continue
            path = reference["path"]
            if path not in digests:
                try:
                    digests[path] = hashlib.sha256(graphflow_edits._read_tracked(workspace, path)).hexdigest()
                except GraphFlowError:
                    digests[path] = None
            digest = digests[path]
            reference["source_state"] = ("unavailable" if digest is None else
                                         "matching_bytes" if digest == reference["sha256"] else "changed")


def _annotate_test_evidence(workspace, outcomes: list[dict]) -> None:
    references = []
    for outcome in outcomes:
        references.extend(item for item in outcome["evidence"] if item.get("kind") == "test")
        if "historical_test_verification" in outcome:
            references.append(outcome["historical_test_verification"])
    states = graphflow_tests.evidence_plan_states(workspace, [item["run_id"] for item in references])
    for item in references:
        item["plan_state"] = states[item["run_id"]]


def _context_trust(outcome: dict) -> str:
    linked = outcome.get("historical_test_verification")
    if linked is not None:
        if linked["state"] == "passed" and linked["exit_code"] == 0 and linked["plan_state"] == "matching_plan":
            return "historical_operator_pass"
        return "recheck_required"
    if any(item.get("source_state") == "matching_bytes" for item in outcome["evidence"]):
        return "code_reference_only"
    if any(item["kind"] == "code" for item in outcome["evidence"]):
        return "recheck_required"
    return "reported_reference_only"


def find_completed(query: str, *, check_evidence: bool = False, evidence_only: bool = False) -> list[dict]:
    if not enabled():
        return []
    query = query.casefold()
    terms = {word for word in re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]+", query)}
    for word in tuple(terms):
        if len(word) > 2 and "\u3400" <= word[0] <= "\u9fff":
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
    hits = []
    workspace = _workspace()
    with read_ledger(workspace) as db:
        if db is None:
            return []
        rows = db.execute("""SELECT t.id,t.inputs,t.updated,e.summary,e.evidence FROM tasks t
                             JOIN task_events e ON e.task_id=t.id AND e.revision=t.revision
                             WHERE t.state='completed' ORDER BY t.updated DESC LIMIT 200""")
        for row in rows:
            evidence = json.loads(row["evidence"])
            if not isinstance(evidence, list):
                continue
            searchable = f"{row['summary']} {row['inputs']} {row['evidence']}".casefold()
            score = sum(len(term) for term in terms if term in searchable)
            if score:
                outcome = {"task_id": row["id"], "evidence": evidence[:2], "reported": True}
                if not evidence_only:
                    outcome["summary"] = row["summary"]
                hits.append((score, row["updated"], outcome))
        ranked = [hit[2] for hit in sorted(hits, key=lambda hit: hit[:2], reverse=True)]
        if evidence_only:
            linked = latest_verifications(db, [outcome["task_id"] for outcome in ranked])
            outcomes = [item for item in ranked if item["evidence"] or item["task_id"] in linked][:4]
        else:
            outcomes = ranked[:4]
            linked = latest_verifications(db, [outcome["task_id"] for outcome in outcomes])
        for outcome in outcomes:
            if outcome["task_id"] in linked:
                outcome["historical_test_verification"] = linked[outcome["task_id"]]
    if check_evidence or evidence_only:
        _annotate_code_evidence(workspace, outcomes)
        _annotate_test_evidence(workspace, outcomes)
    if evidence_only:
        for outcome in outcomes:
            outcome["trust"] = _context_trust(outcome)
    return outcomes


def _references(workspace, references: list[str]) -> list[dict]:
    if (not isinstance(references, list) or len(references) > 5
            or any(not isinstance(ref, str) or len(ref) > 500 for ref in references)):
        raise GraphFlowError("每次最多提供五个证据引用。")
    items = []
    for ref in dict.fromkeys(references):
        if ref.startswith("test:"):
            run_id = ref.removeprefix("test:")
            items.append({"kind": "test", "run_id": run_id, **graphflow_tests.evidence_record(workspace, run_id)})
            continue
        match = re.fullmatch(r"code:(.+):([1-9]\d{0,6})-([1-9]\d{0,6})", ref)
        if not match:
            raise GraphFlowError("证据格式必须是 test:运行编号 或 code:文件:起始行-结束行。")
        path, start, end = match.groups()
        _, _, path, _ = graphflow.validate_read(target=f"{path}:{start}-{end}")
        data = graphflow_edits._read_tracked(workspace, path)
        text, _, _ = graphflow_edits._file_text(data)
        line_count = text.count("\n") + int(bool(text) and not text.endswith("\n"))
        if int(end) > line_count:
            raise GraphFlowError("代码证据超出文件行数。")
        items.append({"kind": "code", "path": path, "start_line": int(start), "end_line": int(end),
                      "sha256": hashlib.sha256(data).hexdigest()})
    return items


def report(task_id: str, revision: int, state: str, summary: str, references: list[str], *, source="mcp") -> dict:
    workspace = _workspace()
    if (type(revision) is not int or not isinstance(state, str) or state not in _TRANSITIONS
            or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 400
            or any(ord(char) < 32 for char in summary)):
        raise GraphFlowError("请提供有效版本、状态和一行 400 字以内的结果摘要。")
    with _db(workspace) as db:
        row = _find(db, task_id)
        if row["revision"] != revision or state not in _TRANSITIONS[row["state"]]:
            raise GraphFlowError("任务版本或状态已变化，请重新读取。", 409)
        if revision >= 100:
            raise GraphFlowError("任务更新次数已达上限。", 409)
        evidence = _references(workspace, references)
        now = time.time()
        db.execute("UPDATE tasks SET state=?,revision=?,updated=? WHERE id=?", (state, revision + 1, now, task_id))
        _event(db, task_id, revision + 1, state, source, now, summary.strip(), evidence)
        return {**_view(_find(db, task_id)), "evidence": evidence, "reported": True}
