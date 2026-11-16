"""Verify GraphFlow's declarative workflow preview and read-only replay."""
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

from app import agent, graphflow, graphflow_edit_queue, graphflow_tests, graphflow_workflows as workflows
from app.graphflow_routes import router
from mcp_server import gf_macro


@unittest.skipUnless(shutil.which("git"), "Git is required")
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.file = self.root / "sample.js"
        self.original = b"export function calculate() { return 2; }\n"
        self.file.write_bytes(self.original)
        subprocess.run(["git", "add", "sample.js"], cwd=self.root, check=True, capture_output=True)
        env = patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.root), workflows.ENV: "", workflows.LEGACY_ENV: "",
                                      workflows.COMMAND_MAP_ENV: "",
                                      graphflow_tests.PROFILES_ENV: json.dumps({
                                          "unit": {"argv": [sys.executable, "-m", "unittest"], "paths": ["*.js"]}
                                      })})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(graphflow._clear_snapshot)

    def test_preview_is_inert_and_replay_rejects_changed_parameters(self):
        with patch.object(graphflow, "read") as read, patch.object(graphflow, "impact") as impact:
            plan = workflows.preview("symbol-review", target="sample.js#calculate")
            self.assertEqual([step["op"] for step in plan["steps"]], ["read", "impact", "test_plan"])
            with self.assertRaises(graphflow.GraphFlowError) as exc:
                workflows.replay("symbol-review", plan["plan_sha256"], target="sample.js#another")
            self.assertEqual(exc.exception.status_code, 409)
            read.assert_not_called()
            impact.assert_not_called()
        with self.assertRaises(graphflow.GraphFlowError):
            workflows.preview("symbol-review", target="../sample.js#calculate")

    def test_http_review_only_hands_off_tests(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
        path = "/api/graphflow/workflow/"
        self.assertEqual(client.get(path + "catalog").status_code, 200)
        body = {"name": "change-review"}
        self.assertEqual(client.post(path + "preview", json={**body, "steps": [{"op": "exec"}]}).status_code, 422)
        preview = client.post(path + "preview", json=body).json()
        replay = {**body, "plan_sha256": preview["plan_sha256"]}
        self.assertEqual(client.post(path + "replay", json=replay, headers={"Origin": "http://other.test"}).status_code, 403)
        with patch.object(graphflow_tests, "run") as run:
            result = client.post(path + "replay", json=replay).json()
            self.assertEqual(result["state"], "completed")
            self.assertFalse(result["files_written"])
            self.assertFalse(result["test_executed"])
            self.assertEqual(json.loads(result["steps"][0]["text"])["paths"], ["sample.js"])
            self.assertEqual(json.loads(result["steps"][1]["text"])["profiles"], ["unit"])
            run.assert_not_called()
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_configuration_changes_and_rejects_unrecognized_steps(self):
        def config(title, op="changes"):
            return json.dumps({"custom": {"title": title, "steps": [{"op": op}]}})
        with patch.dict(os.environ, {workflows.ENV: config("First")}):
            first = workflows.preview("custom")
        with patch.dict(os.environ, {workflows.ENV: config("Second")}):
            with self.assertRaises(graphflow.GraphFlowError) as exc:
                workflows.replay("custom", first["plan_sha256"])
            self.assertEqual(exc.exception.status_code, 409)
        with patch.dict(os.environ, {workflows.ENV: config("Blocked", "test_run")}):
            with self.assertRaises(graphflow.GraphFlowError):
                workflows.catalog()

    def test_real_symbol_review_and_failure_stop(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.skipTest("GraphFlow engine not installed")
        args = {"name": "symbol-review", "target": "sample.js#calculate"}
        plan = workflows.preview(**args)
        report = workflows.replay(**args, plan_sha256=plan["plan_sha256"], budget=5000)
        self.assertEqual(report["state"], "completed")
        self.assertIn("function calculate", report["steps"][0]["text"])
        self.assertEqual(json.loads(report["steps"][1]["text"])["impacts"][0]["symbol"], "calculate")
        self.file.unlink()
        report = workflows.replay(**args, plan_sha256=plan["plan_sha256"])
        self.assertEqual(report["state"], "blocked")
        self.assertEqual([step["op"] for step in report["steps"]], ["read"])

    def test_opt_in_import_previews_and_replays_only_code_targets(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.skipTest("GraphFlow engine not installed")
        store = self.root / ".graphflow" / "macros.json"
        store.parent.mkdir()
        (self.root / "second.js").write_text("export function compute() { return 7; }\n", encoding="utf-8")
        subprocess.run(["git", "add", "second.js"], cwd=self.root, check=True, capture_output=True)
        bundle = {"id": "bundle_abc12345", "kind": "context-bundle", "scriptPath": "DO_NOT_RUN_SCRIPT",
                  "steps": ["sample.js#calculate", "second.js:1-1"]}
        unsupported = [
            {"id": "shell", "kind": "command-pipeline", "steps": ["DO_NOT_RUN_SCRIPT"]},
            {"id": "edit", "kind": "edit-codemod", "steps": ["before", "after"]},
            {**bundle, "id": "escape", "steps": ["../outside.js"]},
            {**bundle, "id": "duplicated"}, {**bundle, "id": "duplicated"},
            {**bundle, "id": "change-review"},
        ]
        payload = json.dumps({"macros": [bundle, *unsupported], "habits": {"command": "DO_NOT_RUN_SCRIPT"}})
        store.write_text(payload, encoding="utf-8")
        key = bundle["id"]
        self.assertNotIn(key, {item["name"] for item in workflows.catalog()})
        with patch.dict(os.environ, {workflows.LEGACY_ENV: "1", "STARWISH_GRAPHFLOW_ASSISTANT": "1"}):
            listed = json.loads(gf_macro("list"))["macros"]
            self.assertEqual({row["name"] for row in listed}, {key, "symbol-review", "change-review", "history-review"})
            with patch.object(graphflow, "read_batch", wraps=graphflow.read_batch) as read:
                pending = json.loads(gf_macro("run", id=key))
                self.assertEqual(pending["state"], "preview")
                self.assertEqual(pending["steps"], [{"op": "read_batch", "targets": bundle["steps"]}])
                self.assertNotIn("DO_NOT_RUN_SCRIPT", json.dumps(pending))
                self.assertEqual(workflows.preview("change-review")["steps"][0]["op"], "changes")
                read.assert_not_called()
                result = agent.run_tool("gf_macro", {"action": "run", "id": key, "plan_sha256": pending["plan_sha256"]})
                read.assert_called_once()
            self.assertEqual(result["state"], "completed")
            self.assertIn("return 2", result["steps"][0]["text"])
            self.assertIn("return 7", result["steps"][0]["text"])
            self.assertFalse(result["files_written"] or result["test_executed"])
            self.assertEqual(store.read_text(encoding="utf-8"), payload)
            self.assertEqual(self.file.read_bytes(), self.original)
            store.write_text(json.dumps({"macros": [{**bundle, "steps": ["second.js"]}]}), encoding="utf-8")
            with self.assertRaises(graphflow.GraphFlowError) as stale:
                gf_macro("run", id=key, plan_sha256=pending["plan_sha256"])
            self.assertEqual(stale.exception.status_code, 409)
            for action in ("save", "mine", "task-run"):
                with self.assertRaises(graphflow.GraphFlowError):
                    gf_macro(action, id=key)
            with self.assertRaises(graphflow.GraphFlowError):
                gf_macro("list", rootDir=str(self.root.parent))
            store.write_text("{invalid", encoding="utf-8")
            self.assertEqual({row["name"] for row in workflows.catalog()}, {"symbol-review", "change-review", "history-review"})

    def test_codemod_rechecks_macro_and_sources_before_approval(self):
        store = self.root / ".graphflow" / "macros.json"
        store.parent.mkdir()
        second = self.root / "second.js"
        second.write_bytes(self.original)
        subprocess.run(["git", "add", "second.js"], cwd=self.root, check=True, capture_output=True)
        old = self.original.decode().strip()
        new = old.replace("return 2", "return 7")

        def payload(replacement):
            return json.dumps({"macros": [
                {"id": "batch", "kind": "edit-codemod", "steps": [old, replacement], "scriptPath": "never-run.js"},
                {"id": "command", "kind": "command-pipeline", "steps": ["never-run"]},
            ]})

        store.write_text(payload(new), encoding="utf-8")
        paths = ["sample.js", "second.js"]
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {
                "LOCALAPPDATA": state, workflows.CODEMOD_ENV: "0", "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1"}):
            self.assertNotIn("edit_macros", json.loads(gf_macro("list")))
            with patch.dict(os.environ, {workflows.CODEMOD_ENV: "1"}):
                listed = json.loads(gf_macro("list"))
                self.assertEqual([item["id"] for item in listed["edit_macros"]], ["batch"])
                for args in ([], ["../sample.js"], ["sample.js", "./sample.js"]):
                    with self.assertRaises(graphflow.GraphFlowError):
                        gf_macro("preview", id="batch", args=args)
                preview = json.loads(gf_macro("run", id="batch", args=paths))
                self.assertEqual(preview["state"], "preview")
                self.assertFalse(preview["queued"])
                self.assertNotIn("never-run", json.dumps(preview))
                self.assertEqual(graphflow_edit_queue.list_items(self.root), [])
                digest = preview["plan_sha256"]
                store.write_text(payload(new + "\n// changed"), encoding="utf-8")
                with self.assertRaises(graphflow.GraphFlowError) as changed:
                    gf_macro("run", id="batch", args=paths, plan_sha256=digest)
                self.assertEqual(changed.exception.status_code, 409)
                store.write_text(payload(new), encoding="utf-8")
                second.write_bytes(b"// unrelated change\n" + self.original)
                with self.assertRaises(graphflow.GraphFlowError) as stale:
                    gf_macro("run", id="batch", args=paths, plan_sha256=digest)
                self.assertEqual(stale.exception.status_code, 409)
                second.write_bytes(self.original)
                self.assertEqual(graphflow_edit_queue.list_items(self.root), [])
                with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_EDIT_QUEUE": "0"}):
                    with self.assertRaises(graphflow.GraphFlowError) as disabled:
                        gf_macro("run", id="batch", args=paths, plan_sha256=digest)
                    self.assertEqual(disabled.exception.status_code, 503)
                result = json.loads(gf_macro("run", id="batch", args=paths[::-1], plan_sha256=digest))
                self.assertTrue(result["queued"])
                self.assertEqual((self.file.read_bytes(), second.read_bytes()), (self.original, self.original))
                key = "test-macro-approval-key-012345"
                app = FastAPI()
                app.include_router(router)
                with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_OPERATOR_KEY": key}), TestClient(
                        app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as client:
                    base = "/api/graphflow/edit/"
                    body = {"preview_id": result["preview_id"], "confirmed_sha256": result["result_sha256"]}
                    self.assertEqual(client.post(base + "apply", json=body).status_code, 403)
                    auth = {"X-StarWish-Operator-Key": key}
                    self.assertEqual(client.post(base + "apply", json=body, headers=auth).status_code, 200)
                    self.assertEqual(self.file.read_bytes(), (new + "\n").encode())
                    self.assertEqual(second.read_bytes(), self.file.read_bytes())
                    body["confirmed_sha256"] = result["base_sha256"]
                    self.assertEqual(client.post(base + "rollback", json=body, headers=auth).status_code, 200)
                self.assertEqual((self.file.read_bytes(), second.read_bytes()), (self.original, self.original))
                self.assertEqual(store.read_text(encoding="utf-8"), payload(new))

    def test_command_macro_only_hands_off_configured_profiles(self):
        store = self.root / ".graphflow" / "macros.json"
        store.parent.mkdir()
        rows = [{"id": "verify", "kind": "command-pipeline", "steps": ["old secret command", "another legacy step"]},
                {"id": "template", "kind": "command-template", "steps": ["old {{arg1}}"]},
                {"id": "unmapped", "kind": "command-template", "steps": ["old"]}]
        store.write_text(json.dumps({"macros": rows}), encoding="utf-8")
        self.assertNotIn("test_macros", json.loads(gf_macro("list")))
        profiles = {"unit": {"argv": [sys.executable, "-m", "unittest"]},
                    "smoke": {"argv": [sys.executable, "-c", "print('not in macro output')"]}}
        env = {workflows.COMMAND_MAP_ENV: json.dumps({"verify": ["smoke", "unit"], "template": ["missing"]}),
               graphflow_tests.PROFILES_ENV: json.dumps(profiles)}
        with patch.dict(os.environ, env), patch.object(graphflow_tests, "run") as run:
            self.assertEqual([row["id"] for row in json.loads(gf_macro("list"))["test_macros"]], ["template", "verify"])
            preview = json.loads(gf_macro("run", id="verify"))
            digest = preview["plan_sha256"]
            self.assertEqual(preview["state"], "preview")
            self.assertEqual(preview["steps"], [{"profile": "smoke", "available": True},
                                                {"profile": "unit", "available": True}])
            self.assertNotIn("old secret command", json.dumps(preview))
            self.assertNotIn("not in macro output", json.dumps(preview))
            self.assertFalse(preview["test_executed"])
            self.assertEqual(json.loads(gf_macro("run", id="verify", plan_sha256=digest))["state"], "awaiting_operator")
            unavailable = json.loads(gf_macro("preview", id="template"))
            self.assertFalse(unavailable["steps"][0]["available"])
            for kwargs in ({"id": "template", "args": ["dynamic"]},
                           {"id": "template", "plan_sha256": unavailable["plan_sha256"]}, {"id": "unmapped"}):
                with self.assertRaises(graphflow.GraphFlowError):
                    gf_macro("run", **kwargs)
            profiles["unit"]["timeout"] = 45
            with patch.dict(os.environ, {graphflow_tests.PROFILES_ENV: json.dumps(profiles)}):
                with self.assertRaises(graphflow.GraphFlowError) as stale:
                    gf_macro("run", id="verify", plan_sha256=digest)
                self.assertEqual(stale.exception.status_code, 409)
            rows[0]["steps"][0] = "changed legacy text"
            store.write_text(json.dumps({"macros": rows}), encoding="utf-8")
            with self.assertRaises(graphflow.GraphFlowError) as stale:
                gf_macro("run", id="verify", plan_sha256=digest)
            self.assertEqual(stale.exception.status_code, 409)
            run.assert_not_called()
