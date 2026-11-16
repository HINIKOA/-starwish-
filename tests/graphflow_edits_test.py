"""Exercise preview receipts against real Git worktrees without writing source via the API."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, graphflow_edit_queue as queue, graphflow_edits as edits
from app.graphflow_routes import router


@unittest.skipUnless(shutil.which("git"), "Git is required")
class EditPreviewTest(unittest.TestCase):
    def setUp(self):
        tmp = self.enter_temp()
        self.root = Path(tmp.name).resolve()
        self.git("init", "-q")
        self.file = self.root / "sample.py"
        self.original = b"\xef\xbb\xbfhead = 1\r\nvalue = 2\r\nend = 3"
        self.file.write_bytes(self.original)
        self.git("add", "sample.py")
        env = patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.root),
                                      "STARWISH_GRAPHFLOW_EDIT_QUEUE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(edits._receipts.clear)
        self.addCleanup(edits.graphflow._clear_snapshot)

    def enter_temp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def test_diff_preserves_original_and_detects_same_timestamp_change_elsewhere(self):
        proposed = edits.preview("sample.py", 2, 2, "value = 2", "value = 7")
        self.assertFalse(proposed["written"])
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertIn("-value = 2\n+value = 7\n", proposed["diff"])
        self.assertIn("\\ No newline at end of file", proposed["diff"])
        self.assertEqual(proposed["result_sha256"], hashlib.sha256(self.original.replace(b"value = 2", b"value = 7")).hexdigest())
        self.assertTrue(edits.check(proposed["preview_id"])["current"])
        old_stat = self.file.stat()
        self.file.write_bytes(self.original.replace(b"head = 1", b"head = 9"))
        os.utime(self.file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        with self.assertRaises(edits.GraphFlowError) as stale:
            edits.check(proposed["preview_id"])
        self.assertEqual(stale.exception.status_code, 409)
        with self.assertRaises(edits.GraphFlowError) as invalid:
            edits.check(proposed["preview_id"])
        self.assertEqual(invalid.exception.status_code, 404)

    def test_diff_only_never_creates_receipt_or_initializes_enabled_queue(self):
        args = {"file": "sample.py", "startLine": 2, "endLine": 2, "oldCode": "value = 2",
                "newCode": "value = 7", "preview_only": True}
        for enable in ("", "1"):
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_EDIT_QUEUE": enable}), patch.object(queue, "_db") as db:
                proposed = edits.compat_preview(**args)
                self.assertEqual(proposed["state"], "preview_only")
                self.assertFalse(proposed["written"] or proposed["queued"])
                self.assertNotIn("preview_id", proposed)
                self.assertIn("-value = 2\n+value = 7\n", proposed["diff"])
                self.assertEqual(proposed["base_sha256"], hashlib.sha256(self.original).hexdigest())
                self.assertFalse(edits._receipts)
                self.assertEqual(self.file.read_bytes(), self.original)
                db.assert_not_called()
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_ASSISTANT": "1", "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1"}), \
                patch.object(queue, "_db") as db:
            self.assertEqual(agent.run_tool("gf_edit", args)["state"], "preview_only")
            db.assert_not_called()
        with self.assertRaises(edits.GraphFlowError):
            edits.compat_preview(**{**args, "oldCode": "value = 0"})
        with self.assertRaises(edits.GraphFlowError):
            edits.compat_preview(**{**args, "preview_only": "true"})

    def test_stale_expected_untracked_path_and_removal(self):
        (self.root / "pending.py").write_text("pending = 2", encoding="utf-8")
        for path, original, status in (
            ("sample.py", "value = 3", 409), ("pending.py", "pending = 2", 404),
            ("../sample.py", "value = 2", 400),
        ):
            with self.subTest(path=path), self.assertRaises(edits.GraphFlowError) as raised:
                edits.preview(path, 2, 2, original, "value = 7")
            self.assertEqual(raised.exception.status_code, status)
        proposed = edits.preview("sample.py", 2, 2, "value = 2", "")
        self.assertIn("-value = 2", proposed["diff"])
        self.git("rm", "--cached", "sample.py")
        with self.assertRaises(edits.GraphFlowError) as stale:
            edits.check(proposed["preview_id"])
        self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_compat_batch_uses_original_line_numbers_and_one_receipt(self):
        batch = [
            {"oldCode": "head = 1", "newCode": "head = 10\nextra = 1"},
            {"startLine": 3, "endLine": 3, "oldCode": "end = 3", "newCode": "end = 30"},
        ]
        proposal = edits.compat_preview(file="./sample.py", edits=batch, rootDir=str(self.root))
        self.assertEqual(proposal["spans"], [{"start_line": 1, "end_line": 1}, {"start_line": 3, "end_line": 3}])
        desired = b"\xef\xbb\xbfhead = 10\r\nextra = 1\r\nvalue = 2\r\nend = 30"
        self.assertEqual(proposal["result_sha256"], hashlib.sha256(desired).hexdigest())
        self.assertTrue(edits.check(proposal["preview_id"])["current"])
        self.assertEqual(self.file.read_bytes(), self.original)
        for bad in ({"edits": [batch[0], batch[0]]}, {"edits": [batch[0], {**batch[1], "file": "other.py"}]},
                    {"oldCode": "value", "newCode": "bad"}, {"oldCode": "value = 2"},
                    {"renameTo": "name"}, {"rootDir": str(self.root.parent), **batch[0]}):
            with self.subTest(bad=bad), self.assertRaises(edits.GraphFlowError):
                edits.compat_preview(file="sample.py", **bad)
        self.assertEqual(len(edits._receipts), 1)
        self.file.write_bytes(b"value = 2\nvalue = 2\n")
        with self.assertRaises(edits.GraphFlowError) as duplicate:
            edits.compat_preview(file="sample.py", oldCode="value = 2", newCode="value = 3")
        self.assertEqual(duplicate.exception.status_code, 409)

    def test_compat_symbol_must_be_unique_and_match_source(self):
        if not edits.graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(edits.graphflow.status()["reason"])
            self.skipTest("GraphFlow engine not installed")
        target = self.root / "symbols.js"
        body = "class A {\n  example() { return 1; }\n}\nclass B {\n  example() { return 1; }\n}\n"
        target.write_bytes(body.encode("utf-8"))
        self.git("add", "symbols.js")
        args = {"file": "symbols.js", "oldCode": "  example() { return 1; }", "newCode": "  example() { return 9; }"}
        with self.assertRaises(edits.GraphFlowError) as ambiguous:
            edits.compat_preview(symbol="example", **args)
        self.assertEqual(ambiguous.exception.status_code, 409)
        with self.assertRaises(edits.GraphFlowError) as stale:
            edits.compat_preview(symbol="B.example", **{**args, "oldCode": "  example() { return 0; }"})
        self.assertEqual(stale.exception.status_code, 409)
        result = edits.compat_preview(symbol="B::example", **args)
        self.assertEqual(result["spans"], [{"start_line": 5, "end_line": 5}])
        self.assertEqual(target.read_bytes(), body.encode("utf-8"))

    def test_rename_preview_uses_disposable_snapshot_and_preserves_bytes(self):
        if not edits.graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(edits.graphflow.status()["reason"])
            self.skipTest("GraphFlow engine not installed")
        source = b"export function computeValue() { return 42; }\r\n"
        caller = b"import { computeValue } from './decl.js';\nexport const result = computeValue();\n"
        decl = self.root / "decl.js"
        use = self.root / "use.js"
        decl.write_bytes(source)
        use.write_bytes(caller)
        (self.root / "untracked.js").write_bytes(caller)
        self.git("add", "decl.js", "use.js")
        args = {"file": "decl.js", "symbol": "computeValue", "renameTo": "calculateValue"}
        edits.graphflow.read(target="decl.js#computeValue")
        snapshot = edits.graphflow._active_snapshot
        with patch.object(edits.graphflow, "_call_bridge", wraps=edits.graphflow._call_bridge) as bridge:
            proposal = edits.compat_preview(**args)
            scratch = bridge.call_args.args[0]
            self.assertIsNot(scratch, snapshot)
            self.assertFalse(scratch.root.exists())
        self.assertEqual((decl.read_bytes(), use.read_bytes()), (source, caller))
        self.assertIn("computeValue", edits.graphflow.read(target="decl.js#computeValue")["text"])
        self.assertEqual(proposal["mode"], "rename")
        self.assertEqual(proposal["files"], [{"path": "decl.js"}, {"path": "use.js"}])
        self.assertIn("+export const result = calculateValue();", proposal["diff"])
        self.assertFalse((self.root / ".graphflow").exists())
        self.assertTrue(edits.check(proposal["preview_id"])["current"])
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_EDIT_QUEUE": "1"}), patch.object(queue, "_db") as db:
            only = edits.compat_preview(**args, preview_only=True)
            self.assertEqual(only["diff"], proposal["diff"])
            self.assertNotIn("preview_id", only)
            db.assert_not_called()
        for bad in ({"newCode": "x"}, {"symbol": "notFound"}, {"renameTo": "x.y"}, {"file": "untracked.js"}):
            with self.subTest(bad=bad), self.assertRaises(edits.GraphFlowError):
                edits.compat_preview(**{**args, **bad})
        with patch.object(edits, "MAX_EDIT_FILES", 1), self.assertRaises(edits.GraphFlowError) as over_limit:
            edits.compat_preview(**args)
        self.assertEqual(over_limit.exception.status_code, 413)
        self.assertEqual(len(edits._receipts), 1)
        real_rename = edits.graphflow.rename_candidates

        def change_during_preview(*params):
            result = real_rename(*params)
            use.write_bytes(caller + b"// concurrent edit\n")
            return result

        with patch.object(edits.graphflow, "rename_candidates", side_effect=change_during_preview):
            with self.assertRaises(edits.GraphFlowError) as stale:
                edits.compat_preview(**args)
            self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(decl.read_bytes(), source)
        self.assertEqual(len(edits._receipts), 1)

    def test_receipt_expires_and_assistant_opt_in_applies(self):
        with patch.object(edits.time, "monotonic", return_value=1000):
            proposed = edits.preview("sample.py", 2, 2, "value = 2", "value = 7")
        with patch.object(edits.time, "monotonic", return_value=1000 + edits.TTL):
            with self.assertRaises(edits.GraphFlowError) as expired:
                edits.check(proposed["preview_id"])
        self.assertEqual(expired.exception.status_code, 410)
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_ASSISTANT": ""}):
            self.assertIn("error", agent.run_tool("graphflow_edit_check", {"preview_id": proposed["preview_id"]}))

    def test_local_http_contract_and_no_write_route(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
        body = {"path": "sample.py", "start_line": 2, "end_line": 2, "expected": "value = 2", "replacement": "value = 7"}
        endpoint = "/api/graphflow/edit/preview"
        self.assertEqual(client.post(endpoint, json=body, headers={"Origin": "http://127.0.0.1:9000"}).status_code, 403)
        self.assertEqual(client.post(endpoint, json={**body, "rootDir": "."}).status_code, 422)
        result = client.post(endpoint, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        receipt = {"preview_id": result.json()["preview_id"]}
        self.assertTrue(client.post("/api/graphflow/edit/check", json=receipt).json()["current"])
        approval = {**receipt, "confirmed_sha256": result.json()["result_sha256"]}
        self.assertEqual(client.post("/api/graphflow/edit/apply", json=approval).status_code, 503)
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_operator_status_is_read_only_and_reports_no_secret(self):
        state = self.enter_temp()
        app = FastAPI()
        app.include_router(router)
        with TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as client:
            url = "/api/graphflow/status"
            self.assertEqual(client.get(url, headers={"Origin": "http://other.test"}).status_code, 403)
            for enabled, key, ready in (("", "", False), ("1", "short-key", False),
                                        ("1", "test-operator-key-0123456789", True)):
                with self.subTest(enabled=enabled, ready=ready), patch.dict(os.environ, {
                    "LOCALAPPDATA": state.name, "STARWISH_GRAPHFLOW_EDIT_QUEUE": enabled,
                    "STARWISH_GRAPHFLOW_OPERATOR_KEY": key,
                }):
                    response = client.get(url)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.json()["edit_queue"], enabled == "1")
                    self.assertEqual(response.json()["operator_key_configured"], ready)
                    if key:
                        self.assertNotIn(key, response.text)
                    self.assertFalse(list(Path(state.name).iterdir()))
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_operator_apply_and_conflict_safe_rollback(self):
        state = self.enter_temp()
        key = "test-operator-key-0123456789"
        with patch.dict(os.environ, {"LOCALAPPDATA": state.name, "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1",
                                     "STARWISH_GRAPHFLOW_OPERATOR_KEY": key}):
            proposal = edits.preview("sample.py", 2, 2, "value = 2", "value = 7")
            self.assertTrue(proposal["queued"])
            self.assertFalse(edits._receipts)
            app = FastAPI()
            app.include_router(router)
            client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
            base = "/api/graphflow/edit/"
            auth = {"X-StarWish-Operator-Key": key}
            body = {"preview_id": proposal["preview_id"], "confirmed_sha256": proposal["result_sha256"]}
            self.assertEqual(client.get(base + "queue").status_code, 403)
            self.assertEqual(client.post(base + "apply", json=body).status_code, 403)
            self.assertEqual(client.post(base + "apply", json=body, headers={**auth, "Origin": "http://other.test"}).status_code, 403)
            self.assertEqual(client.post(base + "apply", json={**body, "confirmed_sha256": "a" * 64}, headers=auth).status_code, 409)
            view = client.get(base + "queue", headers=auth)
            self.assertEqual(view.headers["Cache-Control"], "no-store")
            self.assertEqual(view.json()[0]["diff"], proposal["diff"])
            self.assertEqual(self.file.read_bytes(), self.original)
            applied = client.post(base + "apply", json=body, headers=auth)
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertEqual(applied.json()["state"], "applied")
            self.assertEqual(edits.check(body["preview_id"])["state"], "applied")
            changed = self.original.replace(b"value = 2", b"value = 7")
            self.assertEqual(self.file.read_bytes(), changed)
            self.assertEqual(client.post(base + "apply", json=body, headers=auth).status_code, 409)
            outside = changed.replace(b"head = 1", b"head = 9")
            self.file.write_bytes(outside)
            rollback = {**body, "confirmed_sha256": proposal["base_sha256"]}
            self.assertEqual(client.post(base + "rollback", json=rollback, headers=auth).status_code, 409)
            self.assertEqual(self.file.read_bytes(), outside)
            self.file.write_bytes(changed)
            restored = client.post(base + "rollback", json=rollback, headers=auth)
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json()["state"], "rolled_back")
            self.assertEqual(self.file.read_bytes(), self.original)
            with queue._db(self.root) as db:
                events = db.execute("SELECT state FROM edit_events WHERE preview_id=? ORDER BY seq",
                                    (body["preview_id"],)).fetchall()
            self.assertEqual([event["state"] for event in events],
                             ["pending", "applying", "applied", "rolling_back", "rolled_back"])

    def test_queue_stale_apply_and_recovery_without_rewriting(self):
        state = self.enter_temp()
        with patch.dict(os.environ, {"LOCALAPPDATA": state.name, "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1"}):
            proposal = edits.preview("sample.py", 2, 2, "value = 2", "value = 7")
            receipt = proposal["preview_id"]
            outside = self.original.replace(b"head = 1", b"head = 9")
            self.file.write_bytes(outside)
            with self.assertRaises(edits.GraphFlowError) as stale:
                queue.mutate(self.root, receipt, proposal["result_sha256"], "apply")
            self.assertEqual(stale.exception.status_code, 409)
            self.assertEqual(self.file.read_bytes(), outside)
            self.assertEqual(queue.list_items(self.root)[0]["state"], "stale")
            proposal = edits.preview("sample.py", 2, 2, "value = 2", "value = 7")
            receipt = proposal["preview_id"]
            with patch.object(queue.os, "replace", side_effect=OSError("test interruption")):
                with self.assertRaises(edits.GraphFlowError):
                    queue.mutate(self.root, receipt, proposal["result_sha256"], "apply")
            self.assertFalse(list(self.root.glob(".starwish-edit-*")))
            self.assertEqual(queue.list_items(self.root)[0]["state"], "applying")
            with patch.object(queue.time, "time", return_value=queue.time.time() + 31):
                self.assertEqual(queue.recover(self.root, receipt)["state"], "failed")
            self.assertEqual(self.file.read_bytes(), outside)

    def group_preview(self):
        other = self.root / "z.py"
        other.write_bytes(b"tail = 4\n")
        self.git("add", "z.py")
        proposal = edits.compat_preview(edits=[
            {"file": "sample.py", "oldCode": "value = 2", "newCode": "value = 7"},
            {"file": "z.py", "oldCode": "tail = 4", "newCode": "tail = 8"},
        ])
        return proposal, other

    def test_group_approval_preflights_every_file_and_restores(self):
        state = self.enter_temp()
        key = "test-group-operator-key-0123456789"
        with patch.dict(os.environ, {"LOCALAPPDATA": state.name, "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1",
                                     "STARWISH_GRAPHFLOW_OPERATOR_KEY": key}):
            proposal, other = self.group_preview()
            paths = ["sample.py", "z.py"]
            self.assertEqual([part["path"] for part in proposal["files"]], paths)
            self.assertIn("+++ b/z.py", proposal["diff"])
            self.assertEqual(queue.list_items(self.root)[0]["paths"], paths)
            app = FastAPI()
            app.include_router(router)
            client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
            base = "/api/graphflow/edit/"
            auth = {"X-StarWish-Operator-Key": key}
            apply = {"preview_id": proposal["preview_id"], "confirmed_sha256": proposal["result_sha256"]}
            rollback = {**apply, "confirmed_sha256": proposal["base_sha256"]}
            one_hash = hashlib.sha256(b"tail = 8\n").hexdigest()
            self.assertEqual(client.post(base + "apply", json={**apply, "confirmed_sha256": one_hash}, headers=auth).status_code, 409)
            self.assertEqual(client.post(base + "apply", json=apply).status_code, 403)
            other.write_bytes(b"tail = 99\n")
            self.assertEqual(client.post(base + "apply", json=apply, headers=auth).status_code, 409)
            self.assertEqual(self.file.read_bytes(), self.original)
            other.write_bytes(b"tail = 4\n")
            response = client.post(base + "apply", json=apply, headers=auth)
            self.assertEqual(response.status_code, 200, response.text)
            updated = self.original.replace(b"value = 2", b"value = 7")
            self.assertEqual((self.file.read_bytes(), other.read_bytes()), (updated, b"tail = 8\n"))
            other.write_bytes(b"tail = 99\n")
            self.assertEqual(client.post(base + "rollback", json=rollback, headers=auth).status_code, 409)
            self.assertEqual(self.file.read_bytes(), updated)
            other.write_bytes(b"tail = 8\n")
            restored = client.post(base + "rollback", json=rollback, headers=auth)
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual((self.file.read_bytes(), other.read_bytes()), (self.original, b"tail = 4\n"))
            with queue._db(self.root) as db:
                files = db.execute("SELECT path,before,after FROM edit_files ORDER BY path").fetchall()
                audit = db.execute("SELECT path FROM edit_events WHERE state='rolled_back' ORDER BY path").fetchall()
            self.assertEqual([tuple(part) for part in files], [(path, None, None) for path in paths])
            self.assertEqual([part["path"] for part in audit], paths)

    def test_group_interruption_locks_paths_and_only_restores_recognized_bytes(self):
        state = self.enter_temp()
        with patch.dict(os.environ, {"LOCALAPPDATA": state.name, "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1"}):
            proposal, other = self.group_preview()
            receipt = proposal["preview_id"]
            replace = queue._replace

            def interrupt(workspace, path, expected, data):
                if path == "z.py":
                    raise OSError("test interruption")
                return replace(workspace, path, expected, data)

            with patch.object(queue, "_replace", side_effect=interrupt), self.assertRaises(edits.GraphFlowError) as halted:
                queue.mutate(self.root, receipt, proposal["result_sha256"], "apply")
            self.assertEqual(halted.exception.status_code, 503)
            updated = self.original.replace(b"value = 2", b"value = 7")
            self.assertEqual((self.file.read_bytes(), other.read_bytes()), (updated, b"tail = 4\n"))
            with patch.object(queue.time, "time", return_value=queue.time.time() + 31):
                recovered = queue.recover(self.root, receipt)
            self.assertEqual(recovered["state"], "recovery_needed")
            self.assertEqual(self.file.read_bytes(), updated)
            overlap = edits.preview("z.py", 1, 1, "tail = 4", "tail = 10")
            with self.assertRaises(edits.GraphFlowError):
                queue.mutate(self.root, overlap["preview_id"], overlap["result_sha256"], "apply")
            other.write_bytes(b"tail = 99\n")
            with self.assertRaises(edits.GraphFlowError):
                queue.mutate(self.root, receipt, proposal["base_sha256"], "rollback")
            self.assertEqual((self.file.read_bytes(), other.read_bytes()), (updated, b"tail = 99\n"))
            other.write_bytes(b"tail = 4\n")
            self.assertEqual(queue.mutate(self.root, receipt, proposal["base_sha256"], "rollback")["state"], "rolled_back")
            self.assertEqual((self.file.read_bytes(), other.read_bytes()), (self.original, b"tail = 4\n"))
