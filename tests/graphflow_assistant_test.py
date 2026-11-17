"""Assistant HTTP, model tool loop and project history boundaries."""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import agent, config, context, core, graphflow, jobs
from app.server import app


def model_reply(content):
    return {"choices": [{"message": {"content": content}}]}


def model_call(name, args):
    return {"choices": [{"message": {"tool_calls": [
        {"type": "function", "id": name, "function": {"name": name, "arguments": json.dumps(args)}}
    ]}}]}


class GraphFlowAssistantTest(unittest.TestCase):
    def enter(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def setUp(self):
        temp = self.enter(tempfile.TemporaryDirectory())
        self.root = Path(temp).resolve()
        self.soul = self.root / "soul"
        self.env = self.enter(patch.dict(os.environ, STARWISH_GRAPHFLOW_ASSISTANT="",
                                         STARWISH_GRAPHFLOW_WORKSPACE=""))
        self.enter(patch.object(config, "SOUL_DIR", self.soul))
        self.enter(patch.object(config, "load_config", side_effect=lambda: copy.deepcopy(config.DEFAULT_CONFIG)))
        self.enter(patch.object(core, "core_text", return_value=""))
        self.enter(patch.object(context, "main_ready", return_value=True))
        self.audit = []
        self.enter(patch.object(jobs, "run", self.record_job))
        # No lifespan: do not start background workers in this HTTP test.
        self.client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
        self.addCleanup(self.client.close)
        self.addCleanup(graphflow._clear_snapshot)

    @contextmanager
    def record_job(self, kind, title):
        item = {"kind": kind, "title": title}
        self.audit.append(item)
        yield item

    def chat(self, prompt, complete):
        with patch.object(context, "main_complete", side_effect=complete):
            response = self.client.post("/api/agent/chat", json={"text": prompt})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def history(self):
        response = self.client.get("/api/agent/history")
        self.assertEqual(response.status_code, 200)
        return response.json()["history"]

    def authorize(self, root):
        self.env.update(STARWISH_GRAPHFLOW_WORKSPACE=str(root), STARWISH_GRAPHFLOW_ASSISTANT="1")

    def repo(self, location, marker):
        root = self.root / location / "same-name"
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        (root / "calc.py").write_text(f'def fixture_value():\n    return "{marker}"\n', encoding="utf-8")
        subprocess.run(["git", "add", "calc.py"], cwd=root, check=True, capture_output=True)
        return root

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_context_and_source_in_authorized_assistant_only(self):
        marker = "ONLY_IN_PROJECT_A"
        a = self.repo("a", marker)
        b = self.repo("b", "ONLY_IN_PROJECT_B")
        self.authorize(a)
        if not graphflow.status()["available"]:
            if self.env.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.skipTest(graphflow.status()["reason"])
        self.env["STARWISH_GRAPHFLOW_ASSISTANT"] = ""
        self.assertTrue(self.chat("memory prompt", lambda *_a, **_kw: model_reply("ordinary reply"))["ok"])
        self.authorize(a)
        self.assertEqual(self.history(), [])

        def code_model(messages, _cfg, *, tools, **_kw):
            names = {t["function"]["name"] for t in tools}
            self.assertTrue({"graphflow_context", "gf_read"} <= names)
            self.assertNotIn("ordinary reply", json.dumps(messages))
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not results:
                return model_call("graphflow_context", {"query": "fixture_value"})
            if len(results) == 1:
                self.assertNotIn(marker, json.dumps(results))
                self.assertEqual(results[0]["code_status"], "ready")
                anchor = next(a for a in results[0]["anchors"] if a["symbol"] == "fixture_value")
                self.assertEqual(anchor["file"], "calc.py")
                return model_call("gf_read", {"targets": [anchor["read_target"]]})
            self.assertIn(marker, results[1]["text"])
            return model_reply("Quoted source: " + marker)

        result = self.chat("find fixture_value", code_model)
        self.assertEqual(result["reply"], "Quoted source: " + marker)
        self.assertIn(marker, json.dumps(self.history()))
        self.assertNotIn(marker, json.dumps(self.audit))

        self.authorize(b)
        self.assertEqual(self.history(), [])

        def workspace_b_model(messages, *_a, **_kw):
            self.assertNotIn(marker, json.dumps(messages))
            return model_reply("B reply")

        self.assertTrue(self.chat("new workspace", workspace_b_model)["ok"])

        self.env["STARWISH_GRAPHFLOW_ASSISTANT"] = ""
        self.assertIn("ordinary reply", json.dumps(self.history()))
        self.assertNotIn(marker, json.dumps(self.history()))

        def revoked_model(messages, _cfg, *, tools, **_kw):
            self.assertNotIn(marker, json.dumps(messages))
            self.assertIn("ordinary reply", json.dumps(messages))
            self.assertNotIn("gf_read", {t["function"]["name"] for t in tools})
            if messages[-1]["role"] == "tool":
                self.assertIn("error", json.loads(messages[-1]["content"]))
                return model_reply("Denied as expected")
            return model_call("gf_read", {"targets": ["calc.py"]})

        self.assertEqual(self.chat("try a project read", revoked_model)["reply"], "Denied as expected")
        self.authorize(a)
        self.assertIn(marker, json.dumps(self.history()))
        self.assertEqual(self.client.delete("/api/agent/history").status_code, 200)
        self.assertEqual(self.history(), [])
        self.authorize(b)
        self.assertIn("B reply", json.dumps(self.history()))

    def test_legacy_history_migration_preserves_files_without_reusing_project_data(self):
        folder = self.soul / "agent"
        folder.mkdir(parents=True)
        path = folder / "chat.json"
        ordinary = [{"role": "user", "content": "old memory prompt"},
                    {"role": "assistant", "content": "old ordinary reply"}]
        for evidence in ({"role": "assistant", "content": "PROJECT_SECRET\n（已执行：get_core、gf_read）"},
                         {"role": "assistant", "content": "PROJECT_SECRET\n（已执行：graphflow_context）"},
                         {"role": "system", "content": context.SUMMARY_TAG + "PROJECT_SECRET"}):
            with self.subTest(evidence=evidence):
                path.write_text(json.dumps(ordinary + [evidence]), encoding="utf-8")
                self.assertEqual(self.history(), [])
                self.assertIn("PROJECT_SECRET", path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(ordinary), encoding="utf-8")
        self.assertEqual(self.history(), ordinary)
        self.assertTrue(self.chat("/clear", lambda *_a, **_kw: self.fail("offline command called the model"))["ok"])
        self.assertEqual(self.history(), [])
        self.assertTrue(path.exists())
        agent.reset_history()
        self.assertFalse(path.exists())

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_turn_does_not_dispatch_or_persist_after_workspace_changes(self):
        a = self.repo("a", "PROJECT_A")
        b = self.repo("b", "PROJECT_B")
        self.authorize(a)

        def change_workspace(*_a, **_kw):
            self.authorize(b)
            return model_call("gf_read", {"targets": ["calc.py"]})

        with patch.object(agent, "run_tool") as dispatch:
            result = self.chat("pending request", change_workspace)
        dispatch.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(self.history(), [])
        self.authorize(a)
        self.assertEqual(self.history(), [])
