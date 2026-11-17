"""Persistent task lifecycle, evidence and workspace boundaries."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, graphflow, graphflow_brief, graphflow_context, graphflow_diagnostics
from app import graphflow_skill_feedback as feedback, graphflow_tasks as tasks, graphflow_tests, graphflow_workflows
from app.db import estimate_tokens
from app.graphflow_routes import router
from mcp_server import gf_macro
from app import graphflow_episodes as episodes
from app import graphflow_legacy_tasks
from mcp_server import graphflow_legacy_task_import


@unittest.skipUnless(shutil.which("git"), "Git is required")
class TaskLedgerTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.repo = root / "repo"
        self.repo.mkdir()
        self.repo2 = root / "other"
        self.repo2.mkdir()
        for directory in (self.repo, self.repo2):
            subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
        self.source = self.repo / "source.py"
        self.source.write_text("marker_not_in_ledger = True\n", encoding="utf-8")
        subprocess.run(["git", "add", "source.py"], cwd=self.repo, check=True, capture_output=True)
        engine_base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
        engine_prefix = os.environ.get("STARWISH_GRAPHFLOW_PREFIX") or str(engine_base / "StarWish" / f"graphflow-{graphflow.VERSION}")
        env = patch.dict(os.environ, {"LOCALAPPDATA": str(root / "state"), tasks.ENV: "1",
                                      "STARWISH_GRAPHFLOW_PREFIX": engine_prefix,
                                      "STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo), graphflow_workflows.ENV: "",
                                      graphflow_legacy_tasks.ENV: "",
                                      graphflow_tests.PROFILES_ENV: json.dumps({
                                          "unit": {"argv": [sys.executable, "-c", "print('log_not_in_ledger')"]}})})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(graphflow._clear_snapshot)

    def create(self, request_id="same-request"):
        plan = graphflow_workflows.preview("change-review")
        return tasks.create(request_id, plan["name"], plan["plan_sha256"])

    def test_approved_test_backfills_only_the_selected_task_without_changing_status(self):
        task = self.create()
        another = self.create("other-request")
        self.assertEqual(tasks.detail(task["id"])["test_verifications"], [])
        api = FastAPI()
        api.include_router(router)
        key = "temporary-test-approval-key"
        auth = {"X-StarWish-Operator-Key": key}
        path = "/api/graphflow/test/run"
        plan, = graphflow_tests.plan(self.repo)["profiles"]
        body = {"profile": plan["profile"], "approval_sha256": plan["approval_sha256"], "task_id": task["id"]}
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_OPERATOR_KEY": key}), TestClient(
            api, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)
        ) as client:
            self.assertEqual(client.post(path, json=body).status_code, 403)
            self.assertEqual(client.post(path, json={**body, "task_id": "f" * 32}, headers=auth).status_code, 404)
            self.assertEqual(client.post(path, json={**body, "approval_sha256": "0" * 64}, headers=auth).status_code, 409)
            self.assertEqual(graphflow_tests.records(self.repo), [])
            self.assertEqual(tasks.detail(task["id"])["test_verifications"], [])
            response = client.post(path, json=body, headers=auth)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
        detail = tasks.detail(task["id"])
        self.assertEqual((detail["state"], detail["revision"]), ("pending", 0))
        saved, = detail["test_verifications"]
        self.assertEqual((saved["run_id"], saved["state"], saved["exit_code"]), (result["run_id"], "passed", 0))
        self.assertEqual(result["task_verification"]["status"], "linked")
        self.assertNotIn("log_not_in_ledger", json.dumps(detail))
        tasks.record_verification(self.repo, task["id"], result["run_id"])
        self.assertEqual(tasks.detail(task["id"])["test_verifications"], [saved])
        with self.assertRaises(graphflow.GraphFlowError) as duplicate:
            tasks.record_verification(self.repo, another["id"], result["run_id"])
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertEqual(tasks.detail(another["id"])["test_verifications"], [])
        self.assertEqual(tasks.find_completed("fixture proof"), [])
        self.assertEqual(feedback.insights()["patterns"], [])
        self.assertIn(f"{task['id']} [pending] · 关联测试 unit [passed]", graphflow_brief.render(self.repo))
        tasks.report(task["id"], 0, "completed", "fixture proof", [])
        hit, = tasks.find_completed("fixture proof")
        self.assertEqual(hit["historical_test_verification"], saved)
        self.assertEqual(hit["evidence"], [])
        pattern, = feedback.insights()["patterns"]
        self.assertEqual((pattern["reports"], pattern["passed_test_reports"]), (1, 1))

    def test_legacy_migration_checks_selection_source_and_confirmation(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status())
            self.skipTest("GraphFlow engine not installed")
        raw_source = b"def migrate_fixture():\n    return 1\n"
        self.source.write_bytes(raw_source)
        journal = self.repo / ".graphflow" / "session-journal.jsonl"
        journal.parent.mkdir()
        data = (b'{"kind":"pending-episode","episodeId":"legacy-open","task":"private-old-text"}\n'
                b'{"kind":"pending-episode","episodeId":"legacy-closed"}\n'
                b'{"kind":"outcome","episodeId":"legacy-closed"}\n'
                b'{"kind":"pending-episode","episodeId":"legacy-duplicate"}\n'
                b'{"kind":"pending-episode","episodeId":"legacy-duplicate"}\ninvalid\n')
        journal.write_bytes(data)
        args = {"legacy_id": "legacy-open", "target": "source.py#migrate_fixture"}
        path = tasks.state_path(self.repo, "-tasks", create=False)
        with self.assertRaises(graphflow.GraphFlowError):
            graphflow_legacy_task_import()
        with patch.dict(os.environ, {graphflow_legacy_tasks.ENV: "1", "STARWISH_GRAPHFLOW_ASSISTANT": "1"}):
            listed = json.loads(graphflow_legacy_task_import())
            self.assertEqual(listed["legacy_ids"], ["legacy-open"])
            self.assertEqual(agent.run_tool("graphflow_legacy_task_import", {}), listed)
            self.assertEqual(json.loads(graphflow_legacy_task_import(after="legacy-open"))["legacy_ids"], [])
            with self.assertRaises(graphflow.GraphFlowError):
                graphflow_legacy_task_import(rootDir=str(self.repo2))
            for rejected in ({"legacy_id": "legacy-closed"}, {"legacy_id": "legacy-duplicate"},
                             {"target": "source.py#missing_symbol"}, {"target": "../other/fixture.py#x"}):
                with self.assertRaises(graphflow.GraphFlowError):
                    graphflow_legacy_task_import("preview", **{**args, **rejected})
            preview = json.loads(graphflow_legacy_task_import("preview", **args))
            self.assertEqual(preview["anchor"]["start_line"], 1)
            self.assertFalse(path.exists())
            receipt = {**args, "confirm_sha256": preview["confirm_sha256"]}
            journal.write_bytes(data.replace(b"private-old-text", b"changed-old-text"))
            with self.assertRaises(graphflow.GraphFlowError) as changed:
                graphflow_legacy_task_import("import", **receipt)
            self.assertEqual(changed.exception.status_code, 409)
            journal.write_bytes(data)
            self.source.write_bytes(raw_source.replace(b"return 1", b"return 2"))
            with self.assertRaises(graphflow.GraphFlowError) as changed:
                graphflow_legacy_task_import("import", **receipt)
            self.assertEqual(changed.exception.status_code, 409)
            self.source.write_bytes(raw_source)
            self.assertFalse(path.exists())
            imported = json.loads(graphflow_legacy_task_import("import", **receipt))
            duplicate = json.loads(graphflow_legacy_task_import("import", **receipt))
            self.assertTrue(imported["ledger_written"])
            self.assertFalse(duplicate["ledger_written"])
            self.assertEqual(imported["task"]["id"], duplicate["task"]["id"])
            detail = tasks.detail(imported["task"]["id"])
            self.assertEqual((detail["state"], detail["revision"]), ("pending", 0))
            self.assertEqual(detail["events"][0]["source"], "legacy_import")
            self.assertIn("legacy-open", detail["events"][0]["summary"])
            self.assertNotIn("private-old-text", json.dumps([listed, preview, detail]))
            self.assertEqual(journal.read_bytes(), data)
            self.assertEqual(self.source.read_bytes(), raw_source)

    def test_legacy_migration_panel_requires_local_preview_and_preserves_origin(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status())
            self.skipTest("GraphFlow engine not installed")
        raw = b"def panel_target():\n    return 1\n"
        self.source.write_bytes(raw)
        journal = self.repo / ".graphflow" / "session-journal.jsonl"
        journal.parent.mkdir()
        journal_data = b'{"kind":"pending-episode","id":"panel-old","task":"private-old-panel-content"}\n'
        journal.write_bytes(journal_data)
        api = FastAPI()
        api.include_router(router)
        prefix = "/api/graphflow/"
        move = prefix + "tasks/legacy"
        args = {"legacy_id": "panel-old", "target": "source.py#panel_target"}
        with TestClient(api, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as web:
            self.assertFalse(web.get(prefix + "status").json()["legacy_tasks_enabled"])
            self.assertEqual(web.get(move).status_code, 503)
            with patch.dict(os.environ, {graphflow_legacy_tasks.ENV: "1"}):
                self.assertTrue(web.get(prefix + "status").json()["legacy_tasks_enabled"])
                self.assertEqual(web.get(move, headers={"Origin": "https://other.invalid"}).status_code, 403)
                self.assertEqual(web.get(move).json()["legacy_ids"], ["panel-old"])
                self.assertEqual(web.post(move + "/import", json=args).status_code, 422)
                preview = web.post(move + "/preview", json=args)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.headers["cache-control"], "no-store")
                self.assertNotIn("private-old-panel-content", preview.text)
                self.assertFalse(tasks.state_path(self.repo, "-tasks", create=False).exists())
                receipt = {**args, "confirm_sha256": preview.json()["confirm_sha256"]}
                self.assertEqual(web.post(move + "/import", json=receipt, headers={
                    "Sec-Fetch-Site": "cross-site"}).status_code, 403)
                adopted = web.post(move + "/import", json=receipt)
                self.assertEqual(adopted.status_code, 200, adopted.text)
                task_id = adopted.json()["task"]["id"]
                self.assertEqual(web.get(prefix + "tasks/detail", params={"task_id": task_id}).json()["state"], "pending")
                self.assertEqual(web.post(move + "/import", json=receipt).json()["task"]["id"], task_id)
        self.assertEqual(journal.read_bytes(), journal_data)
        self.assertEqual(self.source.read_bytes(), raw)

    def test_readers_never_initialize_or_modify_task_ledger(self):
        path = tasks.state_path(self.repo, "-tasks", create=False)
        self.assertEqual(tasks.backlog()["counts"], {"pending": 0, "blocked": 0})
        self.assertEqual(tasks.recent()["items"], [])
        self.assertEqual(tasks.find_completed("finished"), [])
        missing = "0" * 32
        for read in (tasks.detail, tasks.replay_task, lambda key: tasks.topic_read_inputs(self.repo, key)):
            with self.assertRaises(graphflow.GraphFlowError) as error:
                read(missing)
            self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(graphflow_diagnostics.inspect()["tasks"]["status"], "not_initialized")
        self.assertEqual(feedback.insights()["status"], "not_initialized")
        graphflow_brief.render(self.repo)
        self.assertFalse(path.parent.exists())
        task_id = self.create()["id"]
        tasks.report(task_id, 0, "completed", "finished fixture", [])
        original = path.read_bytes()
        self.assertEqual(tasks.detail(task_id)["state"], "completed")
        self.assertEqual(tasks.replay_task(task_id)["state"], "preview")
        self.assertEqual(tasks.topic_read_inputs(self.repo, task_id), ("", ""))
        self.assertEqual(tasks.recent()["items"][0]["id"], task_id)
        self.assertEqual(tasks.find_completed("finished")[0]["task_id"], task_id)
        self.assertIn("finished fixture", graphflow_brief.render(self.repo))
        self.assertEqual(graphflow_diagnostics.inspect()["tasks"]["status"], "ready")
        self.assertEqual(feedback.insights()["status"], "ready")
        self.assertEqual(path.read_bytes(), original)

    def test_unavailable_ledger_does_not_hide_live_code(self):
        path = tasks.state_path(self.repo, "-tasks")
        path.write_bytes(b"not a database")
        anchor = {"file": "source.py", "line": 1, "endLine": 1, "name": "marker_not_in_ledger"}
        with patch.object(graphflow, "read", return_value={"index": {"partial": False}, "matches": [anchor]}):
            context = graphflow_context.build("marker_not_in_ledger", 400)
        self.assertEqual(context["code_status"], "ready")
        self.assertEqual(context["anchors"][0]["file"], "source.py")
        self.assertEqual(context["outcomes"], [])
        self.assertEqual(context["outcomes_status"], "unavailable")
        self.assertEqual(graphflow_diagnostics.inspect()["tasks"]["status"], "unavailable")
        self.assertEqual(feedback.insights()["status"], "unavailable")
        self.assertIn("暂不可读", graphflow_brief.render(self.repo))
        self.assertEqual(path.read_bytes(), b"not a database")

    def test_context_marks_historical_code_without_rewriting_ledger(self):
        task_id = self.create()["id"]
        tasks.report(task_id, 0, "completed", "Freshness fixture.", ["code:source.py:1-1"])
        path = tasks.state_path(self.repo, "-tasks", create=False)
        original = path.read_bytes()
        source_bytes = self.source.read_bytes()
        with patch.object(graphflow, "read", return_value={"index": {"partial": False}, "matches": []}):
            def recall():
                packet = graphflow_context.build("freshness", 750)
                item, = packet["outcomes"]
                self.assertEqual(item["task_id"], task_id)
                self.assertTrue(item["reported"])
                return item["evidence"][0]["source_state"]

            self.assertEqual(recall(), "matching_bytes")
            self.source.write_bytes(source_bytes + b"# change\n")
            self.assertEqual(recall(), "changed")
            self.source.unlink()
            self.assertEqual(recall(), "unavailable")
            self.source.write_bytes(source_bytes)
            self.assertEqual(recall(), "matching_bytes")
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("source_state", tasks.detail(task_id)["events"][0]["evidence"][0])

    def test_context_compares_historical_test_plan_without_running_or_publishing_it(self):
        task_id = self.create()["id"]
        plan, = graphflow_tests.plan(self.repo)["profiles"]
        run = graphflow_tests.run(self.repo, "unit", plan["approval_sha256"])
        run_id = run["run_id"]
        tasks.record_verification(self.repo, task_id, run_id)
        tasks.report(task_id, 0, "completed", "Test plan freshness fixture.", [f"test:{run_id}"])
        test_path = tasks.state_path(self.repo, "-tests", create=False)
        ledger_path = tasks.state_path(self.repo, "-tasks", create=False)
        original = test_path.read_bytes(), ledger_path.read_bytes()
        initial = self.source.read_bytes()

        def recall(expected):
            item, = graphflow_context.build("freshness", 1000)["outcomes"]
            self.assertEqual(item["task_id"], task_id)
            proof = item["evidence"][0]
            linked = item["historical_test_verification"]
            for row in (proof, linked):
                self.assertEqual((row["run_id"], row["state"], row["plan_state"]), (run_id, "passed", expected))
            self.assertNotIn(plan["approval_sha256"], json.dumps(item))
            self.assertNotIn("log_not_in_ledger", json.dumps(item))

        with patch.object(graphflow, "read", return_value={"index": {"partial": False}, "matches": []}), \
             patch.object(graphflow_tests, "_execute") as execute:
            with patch.object(graphflow_tests, "plan", wraps=graphflow_tests.plan) as live_plan:
                recall("matching_plan")
                live_plan.assert_called_once()
            self.source.write_bytes(initial + b"# different worktree\n")
            recall("changed_plan")
            self.source.write_bytes(initial)
            recall("matching_plan")
            with patch.dict(os.environ, {graphflow_tests.PROFILES_ENV: json.dumps(
                {"unit": {"argv": [sys.executable, "-c", "pass"]}})}
            ):
                recall("changed_plan")
            with patch.dict(os.environ, {graphflow_tests.PROFILES_ENV: ""}):
                recall("unavailable")
            with patch.object(graphflow_tests, "_RETENTION_SECONDS", 0):
                recall("unavailable")
            execute.assert_not_called()
        self.assertEqual((test_path.read_bytes(), ledger_path.read_bytes()), original)
        self.assertNotIn("plan_state", tasks.detail(task_id)["test_verifications"][0])

    def test_topic_read_scope_and_missing_ledger(self):
        path = tasks.state_path(self.repo, "-tasks", create=False)
        with self.assertRaises(graphflow.GraphFlowError) as missing:
            graphflow.read_batch(topicId="0" * 32)
        self.assertEqual(missing.exception.status_code, 404)
        self.assertFalse(path.exists() or path.parent.exists())
        topic = self.create()["id"]
        before = path.read_bytes()
        with self.assertRaisesRegex(graphflow.GraphFlowError, "必须指向同一任务"):
            graphflow.read_batch(topicId=topic, resumeFromTurnId="0" * 32)
        with self.assertRaisesRegex(graphflow.GraphFlowError, "没有保存代码定位"):
            graphflow.read_batch(topicId=topic)
        with self.assertRaises(graphflow.GraphFlowError) as outside:
            graphflow.read_batch(topicId=topic, rootDir=str(self.repo2))
        self.assertEqual(outside.exception.status_code, 403)
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo2)}):
            self.create()
            with self.assertRaises(graphflow.GraphFlowError) as other:
                graphflow.read_batch(topicId=topic, targets=["source.py"])
            self.assertEqual(other.exception.status_code, 404)
        with patch.dict(os.environ, {tasks.ENV: ""}):
            with self.assertRaises(graphflow.GraphFlowError) as disabled:
                graphflow.read_batch(topicId=topic)
            self.assertEqual(disabled.exception.status_code, 503)
        self.assertEqual(path.read_bytes(), before)

    def test_project_brief_uses_live_snapshot_and_explicit_handoff_only(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.skipTest("GraphFlow engine not installed")
        (self.repo / "source.ts").write_text("export function greet() { return 42; }\n", encoding="utf-8")
        (self.repo / "entry.ts").write_text("import { greet } from './source';\ngreet();\n", encoding="utf-8")
        subprocess.run(["git", "add", "source.ts", "entry.ts"], cwd=self.repo, check=True)
        state = tasks.state_path(self.repo, "-tasks", create=False)
        first = graphflow.read_batch()["text"]
        self.assertIn("source.ts (imported by 1 file(s))", first)
        self.assertEqual(first, graphflow.read_batch(["@brief", "brief"])["text"])
        self.assertFalse(state.parent.exists())
        with self.assertRaises(graphflow.GraphFlowError):
            graphflow.read_batch(["@brief", "untracked.py"])
        topic = self.create()["id"]
        tasks.report(topic, 0, "completed", "Confirmed handoff.", ["code:source.py:1-1"])
        plan, = graphflow_tests.plan(self.repo)["profiles"]
        run = graphflow_tests.run(self.repo, "unit", plan["approval_sha256"])
        self.assertEqual(run["state"], "passed")
        records = tasks.state_path(self.repo, "-tests", create=False)
        before = (state.read_bytes(), records.read_bytes())
        private = self.repo / ".graphflow"
        private.mkdir()
        (private / "session-journal.jsonl").write_text("private_journal_marker", encoding="utf-8")
        brief = graphflow.read_batch(["@brief", "source.py:1-1"])["text"]
        for text in (topic, "Confirmed handoff.", "unit [passed]", "marker_not_in_ledger"):
            self.assertIn(text, brief)
        for text in ("private_journal_marker", "log_not_in_ledger", run["run_id"]):
            self.assertNotIn(text, brief)
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo2)}):
            (self.repo2 / "other.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "other.py"], cwd=self.repo2, check=True)
            other = graphflow.read_batch()["text"]
            self.assertNotIn(topic, other)
            self.assertNotIn("unit [passed]", other)
        self.assertEqual((state.read_bytes(), records.read_bytes()), before)

    def test_topic_read_reuses_location_not_history(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.skipTest("GraphFlow engine not installed")
        self.source.write_text("def topic_symbol():\n    return 'marker_not_in_ledger'\n", encoding="utf-8")
        target = "source.py#topic_symbol"
        plan = graphflow_workflows.preview("symbol-review", target=target)
        topic = tasks.create("test-topic-source", plan["name"], plan["plan_sha256"], target=target)["id"]
        tasks.report(topic, 0, "blocked", "private_topic_outcome", [])
        path = tasks.state_path(self.repo, "-tasks", create=False)
        before = path.read_bytes()
        journal = self.repo / ".graphflow" / "session-journal.jsonl"
        journal.parent.mkdir()
        journal.write_text(topic + " private_journal_outcome\n", encoding="utf-8")
        first = graphflow.read_batch(topicId=topic)
        self.assertIn("marker_not_in_ledger", first["text"])
        self.assertNotIn("private_", first["text"])
        self.assertEqual(first["topic_id"], topic)
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_ASSISTANT": "1"}):
            self.assertEqual(first["text"], agent.run_tool("gf_read", {"topicId": topic})["text"])
        args = {"resumeFromTurnId": topic, "dedup": True, "sessionId": "test-topic-session"}
        self.assertEqual(first["text"], graphflow.read_batch(**args)["text"])
        repeated = graphflow.read_batch(**args)
        self.assertTrue(repeated["deduplicated"])
        self.assertIn(f"Topic Resume: {topic}", repeated["text"])
        other = self.repo / "other.py"
        other.write_text("override_marker = True\n", encoding="utf-8")
        subprocess.run(["git", "add", "other.py"], cwd=self.repo, check=True)
        self.assertIn("override_marker", graphflow.read_batch(topicId=topic, targets=["other.py:1-1"])["text"])
        self.assertIn("topic_symbol", graphflow.read_batch(topicId=topic, q="topic_symbol")["text"])
        self.source.write_text("def topic_symbol():\n    return 'fresh_topic_marker'\n", encoding="utf-8")
        self.assertIn("fresh_topic_marker", graphflow.read_batch(**args)["text"])
        self.source.unlink()
        with self.assertRaises(graphflow.GraphFlowError) as gone:
            graphflow.read_batch(topicId=topic)
        self.assertEqual(gone.exception.status_code, 404)
        self.assertEqual(path.read_bytes(), before)

    def test_idempotent_create_and_stale_update(self):
        first = self.create()
        again = self.create()
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["duplicate"])
        with self.assertRaises(graphflow.GraphFlowError) as reused:
            tasks.create("same-request", "change-review", "0" * 64)
        self.assertEqual(reused.exception.status_code, 409)
        moved = tasks.report(first["id"], 0, "blocked", "Waiting for tests.", [])
        self.assertEqual(moved["revision"], 1)
        with self.assertRaises(graphflow.GraphFlowError) as stale:
            tasks.report(first["id"], 0, "completed", "Finished.", [])
        self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(tasks.backlog()["counts"], {"pending": 0, "blocked": 1})
        tasks.report(first["id"], 1, "completed", "Finished.", [])
        self.assertEqual(tasks.backlog()["items"], [])
        self.assertEqual([item["state"] for item in tasks.detail(first["id"])["events"]],
                         ["completed", "blocked", "pending"])

    def test_evidence_references_only_and_workspace_scope(self):
        first = self.create()
        plan, = graphflow_tests.plan(self.repo)["profiles"]
        run = graphflow_tests.run(self.repo, "unit", plan["approval_sha256"])
        self.assertEqual(run["state"], "passed")
        with self.assertRaises(graphflow.GraphFlowError):
            tasks.report(first["id"], 0, "completed", "Verified.", ["code:source.py:1-2"])
        done = tasks.report(first["id"], 0, "completed", "Verified.",
                            ["code:source.py:1-1", f"test:{run['run_id']}"])
        self.assertEqual([item["kind"] for item in done["evidence"]], ["code", "test"])
        self.assertEqual(done["evidence"][1]["exit_code"], 0)
        serialized = json.dumps(tasks.detail(first["id"]))
        self.assertNotIn("marker_not_in_ledger", serialized)
        self.assertNotIn("log_not_in_ledger", serialized)
        no_evidence = self.create("no-evidence")
        tasks.report(no_evidence["id"], 0, "completed", "Not verified.", [])
        report = feedback.insights(1, str(self.repo))
        self.assertEqual(report["sampled"], 2)
        self.assertEqual(report["patterns"], [{"workflow": "change-review", "reports": 1, "code_reports": 1,
                                               "passed_test_reports": 1, "example_task_ids": [first["id"]]}])
        self.assertFalse(report["current_verification"])
        self.assertNotIn("Verified.", json.dumps(report))
        tasks.record_verification(self.repo, first["id"], run["run_id"])
        self.assertEqual(feedback.insights()["patterns"], report["patterns"])
        with patch.object(graphflow_tests, "_execute", return_value=("failed", 1, "private-failure-log", False)):
            failed = graphflow_tests.run(self.repo, "unit", plan["approval_sha256"])
        tasks.record_verification(self.repo, first["id"], failed["run_id"])
        pattern, = feedback.insights()["patterns"]
        self.assertEqual((pattern["reports"], pattern["code_reports"], pattern["passed_test_reports"]), (1, 1, 0))
        brief = graphflow_brief.render(self.repo)
        self.assertIn(f"{first['id']} [completed]：Verified. · 关联测试 unit [failed]", brief)
        self.assertNotIn("private-failure-log", brief)
        hit = next(item for item in tasks.find_completed("Verified.") if item["task_id"] == first["id"])
        self.assertEqual(hit["historical_test_verification"]["run_id"], failed["run_id"])
        self.assertEqual(hit["historical_test_verification"]["state"], "failed")
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo2)}):
            self.assertEqual(tasks.backlog()["items"], [])
            self.assertEqual(feedback.insights()["patterns"], [])
            with self.assertRaises(graphflow.GraphFlowError) as outside:
                tasks.detail(first["id"])
            self.assertEqual(outside.exception.status_code, 404)

    def test_feedback_is_read_only_with_no_ledger(self):
        self.assertEqual(feedback.insights()["status"], "not_initialized")
        self.assertFalse(Path(os.environ["LOCALAPPDATA"]).exists())
        self.assertEqual(feedback.guide()["server"], "starwish")
        for args in ({"limit": 0}, {"rootDir": str(self.repo2)}):
            with self.assertRaises(graphflow.GraphFlowError):
                feedback.insights(**args)

    def test_episode_bridge_reuses_ledger_boundaries(self):
        with patch.object(graphflow_tests, "run") as run:
            started = episodes.run("Review marker_not_in_ledger.", rootDir=str(self.repo), requestId="episode-fixture")
            self.assertFalse(started["executed"])
            self.assertEqual(started["state"], "pending")
            ident = started["episodeId"]
            with self.assertRaises(graphflow.GraphFlowError):
                episodes.run("Review marker_not_in_ledger.", requestId="episode-fixture")
            self.assertEqual(tasks.backlog()["counts"]["pending"], 1)
            for params in ({"references": ["code:source.py:1-2"]}, {"configPath": "outside"},
                           {"rootDir": str(self.repo2)}, {"lessons": ["bad\nline"]}):
                with self.assertRaises(graphflow.GraphFlowError):
                    episodes.report(ident, True, **params)
            blocked = episodes.report(ident, False)
            self.assertEqual(blocked["state"], "blocked")
            with self.assertRaises(graphflow.GraphFlowError) as stale:
                episodes.report(ident, True, revision=0)
            self.assertEqual(stale.exception.status_code, 409)
            done = episodes.report(ident, True, ["episode_fixture_result"], revision=1,
                                   references=["code:source.py:1-1"])
            self.assertEqual(done["state"], "completed")
            self.assertFalse(done["current_verification"])
            self.assertEqual(tasks.find_completed("episode_fixture_result")[0]["task_id"], ident)
            with self.assertRaises(graphflow.GraphFlowError):
                episodes.report(ident, True)
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo2)}):
                with self.assertRaises(graphflow.GraphFlowError) as outside:
                    episodes.report(ident, True)
                self.assertEqual(outside.exception.status_code, 404)
            run.assert_not_called()

    def test_macro_task_preview_replay_and_report_stay_separate(self):
        first = self.create()
        request = {"id": first["id"], "rootDir": str(self.repo)}
        with patch.object(graphflow_workflows, "replay", wraps=graphflow_workflows.replay) as replay, \
             patch.object(graphflow_tests, "run") as run_tests:
            listed = json.loads(gf_macro("task-list"))
            self.assertEqual(listed["items"][0]["id"], first["id"])
            pending = json.loads(gf_macro("task-run", **request))
            self.assertEqual(pending["state"], "preview")
            self.assertFalse(pending["task_updated"])
            replay.assert_not_called()
            with self.assertRaises(graphflow.GraphFlowError) as stale:
                gf_macro("task-run", **request, plan_sha256="0" * 64)
            self.assertEqual(stale.exception.status_code, 409)
            with self.assertRaises(graphflow.GraphFlowError):
                gf_macro("task-run", **request, args=["not-saved.py"])
            digest = pending["plan"]["plan_sha256"]
            report = json.loads(gf_macro("task-run", **request, plan_sha256=digest))
            self.assertEqual(report["state"], "completed")
            self.assertEqual(report["task"]["state"], "pending")
            self.assertFalse(report["task_updated"] or report["test_executed"] or report["files_written"])
            self.assertEqual(len(tasks.detail(first["id"])["events"]), 1)
            run_tests.assert_not_called()
        tasks.report(first["id"], 0, "completed", "Reviewed.", ["code:source.py:1-1"])
        self.assertEqual(json.loads(gf_macro("task-list"))["items"][0]["state"], "completed")
        preview = json.loads(gf_macro("task-run", **request))
        self.assertEqual(preview["latest_event"]["evidence"][0]["path"], "source.py")
        self.assertNotIn("marker_not_in_ledger", json.dumps(preview))
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo2)}):
            self.assertEqual(json.loads(gf_macro("task-list"))["items"], [])
            with self.assertRaises(graphflow.GraphFlowError) as outside:
                gf_macro("task-run", id=first["id"])
            self.assertEqual(outside.exception.status_code, 404)
        with patch.dict(os.environ, {tasks.ENV: ""}):
            with self.assertRaises(graphflow.GraphFlowError) as disabled:
                gf_macro("task-list")
            self.assertEqual(disabled.exception.status_code, 503)

    def test_macro_task_revalidates_saved_workflow(self):
        def spec(title):
            return json.dumps({"custom": {"title": title, "steps": [{"op": "changes"}]}})

        with patch.dict(os.environ, {graphflow_workflows.ENV: spec("Original")}):
            plan = graphflow_workflows.preview("custom")
            item = tasks.create("changed-macro", "custom", plan["plan_sha256"])
        with patch.dict(os.environ, {graphflow_workflows.ENV: spec("Changed")}):
            with patch.object(graphflow_tests, "_changed") as git_changes:
                with self.assertRaises(graphflow.GraphFlowError) as stale:
                    gf_macro("task-run", id=item["id"], plan_sha256=plan["plan_sha256"])
                self.assertEqual(stale.exception.status_code, 409)
                git_changes.assert_not_called()

    def test_http_origin_opt_in_and_backlog_age(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
        base = "/api/graphflow/tasks/"
        body = {"request_id": "test-http-task", "name": "change-review",
                "plan_sha256": graphflow_workflows.preview("change-review")["plan_sha256"]}
        self.assertEqual(client.post(base + "create", json=body, headers={"Origin": "http://other.test"}).status_code, 403)
        created = client.post(base + "create", json=body)
        self.assertEqual(created.status_code, 200, created.text)
        with patch.object(tasks.time, "time", return_value=created.json()["created"] + 86401):
            self.assertEqual(client.get(base + "backlog").json()["older_than_24h"], 1)
        with patch.dict(os.environ, {tasks.ENV: ""}):
            self.assertEqual(client.get(base + "backlog").status_code, 503)

    def test_context_combines_live_code_and_completed_outcome(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.skipTest("GraphFlow engine not installed")
        self.source.write_text("def calculate_total():\n    return 'private_snippet_marker'\n", encoding="utf-8")
        first = self.create()
        tasks.report(first["id"], 0, "completed", "calculate_total: verify the rounding policy.",
                     ["code:source.py:1-2"])
        unrelated = self.create("different-request")
        tasks.report(unrelated["id"], 0, "completed", "The cache is refreshed.", [])
        packet = graphflow_context.build("calculate_total", 750, str(self.repo))
        self.assertEqual(packet["code_status"], "ready")
        anchor, = [item for item in packet["anchors"] if item["symbol"] == "calculate_total"]
        self.assertEqual(anchor["read_target"], "source.py:1-2")
        self.assertEqual([row["task_id"] for row in packet["outcomes"]], [first["id"]])
        self.assertNotIn("private_snippet_marker", json.dumps(packet))
        self.assertIn("private_snippet_marker", graphflow.read(target=anchor["read_target"])["text"])
        smaller = graphflow_context.build("calculate_total", 256)
        self.assertLessEqual(estimate_tokens(json.dumps(smaller, ensure_ascii=False)), 256)
        with self.assertRaises(graphflow.GraphFlowError) as outside:
            graphflow_context.build("calculate_total", rootDir=str(self.repo2))
        self.assertEqual(outside.exception.status_code, 403)
        with patch.object(graphflow, "read", side_effect=graphflow.GraphFlowError("offline", 503)):
            degraded = graphflow_context.build("calculate_total")
        self.assertEqual(degraded["code_status"], "unavailable_503")
        self.assertEqual(degraded["outcomes"][0]["task_id"], first["id"])
