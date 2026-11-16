"""Diagnostics expose aggregate project health without initializing state."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, graphflow, graphflow_diagnostics as diagnostics, graphflow_legacy_journal, graphflow_tasks, graphflow_workflows
from app.graphflow_routes import router
from mcp_server import gf_exec


class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.repo, self.state = root / "repo", root / "state"
        (self.repo / ".git").mkdir(parents=True)
        env = patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo), "LOCALAPPDATA": str(self.state),
                                      "STARWISH_GRAPHFLOW_TASKS": "1", "STARWISH_GRAPHFLOW_ASSISTANT": "1",
                                      graphflow_legacy_journal.ENV: "", graphflow_legacy_journal.TOPIC_ENV: ""})
        env.start()
        self.addCleanup(env.stop)

    def test_cold_no_engine_does_not_create_state(self):
        with patch.object(graphflow, "_engine", side_effect=graphflow.GraphFlowError("not installed", 503)), \
             patch.object(graphflow, "_active_snapshot", None):
            result = agent.run_tool("graphflow_diagnose", {})
            self.assertEqual(result["engine"], "unavailable")
            self.assertEqual(result["index"]["status"], "cold")
            self.assertEqual(result["tasks"]["status"], "not_initialized")
        self.assertFalse(self.state.exists())
        for args in ({"configPath": "outside.toml"}, {"rootDir": str(self.repo.parent)}, {"nodeLimit": -1}):
            with self.assertRaises(graphflow.GraphFlowError):
                diagnostics.inspect(**args)
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": ""}):
            self.assertEqual(diagnostics.inspect()["workspace"], "not_configured")

    def test_exec_doctor_reports_scoped_markers_not_effective_registrations(self):
        def marker(root, name):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private-config-marker", encoding="utf-8")
            return path

        original = marker(self.repo, ".codex/config.toml")
        marker(self.repo, ".cursor/mcp.json")
        outsider = marker(self.repo.parent, ".claude/settings.json")
        try:
            (self.repo / ".mcp.json").hardlink_to(outsider)
        except OSError:
            pass
        home = self.repo.parent / "home"
        marker(home, ".gemini/settings.json")
        snapshot = SimpleNamespace(workspace=self.repo, files={"x.py": 1}, warmed={"x.py": 1})
        ready = {"available": True, "provider": graphflow.PROVIDER, "reason": ""}
        with (
            patch.object(graphflow, "status", return_value=ready),
            patch.object(graphflow, "_active_snapshot", snapshot),
            patch.object(graphflow, "_sync_snapshot") as refresh,
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            status = gf_exec(cmd="status")
            doctor = gf_exec(cmd="graphflow doctor", rootDir=str(self.repo))
            self.assertEqual(agent.run_tool("gf_exec", {"cmd": "doctor"})["text"], doctor)
            refresh.assert_not_called()
        self.assertIn("Index: cached (current process); 1 indexed file(s), 1 warmed", status)
        self.assertNotIn("Project markers", status)
        self.assertIn("Tasks: not_initialized", doctor)
        self.assertIn("effective registration not checked", doctor)
        self.assertIn("codex: config", doctor)
        self.assertIn("cursor: mcp", doctor)
        for value in ("private-config-marker", str(self.repo), "claude-code", "gemini"):
            self.assertNotIn(value, doctor)
        self.assertEqual(original.read_text(encoding="utf-8"), "private-config-marker")
        self.assertFalse(self.state.exists())
        with self.assertRaises(graphflow.GraphFlowError) as wrong_scope:
            gf_exec(cmd="doctor", rootDir=str(self.repo.parent))
        self.assertEqual(wrong_scope.exception.status_code, 403)

    def test_local_report_is_aggregate_and_no_store(self):
        plan = graphflow_workflows.preview("change-review")
        item = graphflow_tasks.create("diagnostics-fixture", "change-review", plan["plan_sha256"])
        graphflow_tasks.report(item["id"], 0, "blocked", "private outcome marker", [])
        app = FastAPI()
        app.include_router(router)
        with TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as client:
            path = "/api/graphflow/diagnose"
            self.assertEqual(client.get(path, headers={"Origin": "http://example.test"}).status_code, 403)
            result = client.get(path)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertEqual(result.json()["tasks"], {"status": "ready", "pending": 0, "blocked": 1, "older_than_24h": 0})
        report = json.dumps(result.json())
        self.assertNotIn(item["id"], report)
        self.assertNotIn("private outcome marker", report)
        self.assertNotIn(json.dumps(str(self.repo))[1:-1], report)

    def test_legacy_journal_is_opt_in_bounded_and_aggregate(self):
        journal = self.repo / ".graphflow" / "session-journal.jsonl"
        journal.parent.mkdir()
        day = 86_400_000
        data = b"\n".join([
            b'{"kind":"pending-episode","createdAt":0,"status":"secret-topic","task":"secret-transcript"}',
            b'{"kind":"pending-episode","createdAt":2592000000}',
            b'{"kind":"pending-episode","createdAt":true}', b'{"kind":"outcome"}', b'[]', b'{corrupt', b' ',
        ])
        journal.write_bytes(data)
        self.assertEqual(diagnostics.inspect()["legacy_journal"], {"status": "disabled"})
        with patch.dict(os.environ, {graphflow_legacy_journal.ENV: "1"}), \
                patch.object(graphflow_legacy_journal.time, "time", return_value=60 * day / 1000):
            result = agent.run_tool("graphflow_diagnose", {})
            self.assertEqual(result["legacy_journal"], {"status": "ready", "total_lines": 6, "parse_errors": 2,
                             "pending": 3, "over_ttl": 1, "unknown_age": 1, "ttl_days": 30,
                             "oldest_pending_age_days": 60.0})
            self.assertNotIn("secret-", json.dumps(result))
            self.assertEqual(journal.read_bytes(), data)
            self.assertFalse(self.state.exists())
            with patch.object(graphflow_legacy_journal, "MAX_BYTES", 8):
                self.assertEqual(diagnostics.inspect()["legacy_journal"], {"status": "too_large"})
            alias = self.repo / "journal-copy.jsonl"
            os.link(journal, alias)
            self.assertEqual(diagnostics.inspect()["legacy_journal"], {"status": "unavailable"})
            alias.unlink()
            journal.unlink()
            self.assertEqual(diagnostics.inspect()["legacy_journal"], {"status": "missing"})

    def test_legacy_resume_requires_exact_field_and_explicit_current_source(self):
        journal = self.repo / ".graphflow" / "session-journal.jsonl"
        journal.parent.mkdir()
        data = (b'{"kind":"pending-episode","episodeId":"turn-12","task":"prefix-trick hidden-text"}\n'
                b'{"kind":"notes","id":"other-11"}\ninvalid legacy-broken\n')
        journal.write_bytes(data)
        with patch.dict(os.environ, {graphflow_legacy_journal.ENV: "1"}), self.assertRaises(graphflow.GraphFlowError):
            graphflow.read_batch(["app.py:1-1"], topicId="turn-12")
        with patch.dict(os.environ, {graphflow_legacy_journal.TOPIC_ENV: "1"}), \
                patch.object(graphflow, "_read", return_value={"text": "active source"}) as read:
            for topic in ("turn-1", "prefix-trick", "other-11", "legacy-broken"):
                with self.subTest(topic=topic), self.assertRaises(graphflow.GraphFlowError) as missing:
                    graphflow.read_batch(["app.py:1-1"], topicId=topic)
                self.assertEqual(missing.exception.status_code, 404)
            for targets in (None, ["@brief"]):
                with self.assertRaisesRegex(graphflow.GraphFlowError, "显式源码"):
                    graphflow.read_batch(targets, topicId="turn-12")
            with self.assertRaises(graphflow.GraphFlowError) as outside:
                graphflow.read_batch(["app.py:1-1"], topicId="turn-12", rootDir=str(self.repo.parent))
            self.assertEqual(outside.exception.status_code, 403)
            read.assert_not_called()
            args = {"targets": ["app.py:1-1"], "resumeFromTurnId": "turn-12"}
            with patch.object(graphflow, "status", return_value={"available": True}):
                with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_ASSISTANT": ""}):
                    self.assertIn("error", agent.run_tool("gf_read", args))
                    read.assert_not_called()
                result = agent.run_tool("gf_read", args)
            self.assertEqual(result["topic_source"], "legacy_journal")
            self.assertIn("active source", result["text"])
            self.assertNotIn("hidden-text", result["text"])
            self.assertEqual(journal.read_bytes(), data)
            self.assertFalse(self.state.exists())
