"""Verify controlled test execution with a disposable Git worktree."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, graphflow, graphflow_legacy_observations, graphflow_tests as runner
from app.graphflow import GraphFlowError
from app.graphflow_routes import router
from mcp_server import TOOLS, gf_exec, graphflow_test_log


@unittest.skipUnless(shutil.which("git"), "Git is required")
class ControlledTestRun(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "app.py").write_text("a = 1\n", encoding="utf-8")
        self.git("add", "app.py")
        self.config = {
            "unit": {"argv": [sys.executable, "-c", "import os; print('ok', 'STARWISH_GRAPHFLOW_OPERATOR_KEY' in os.environ)"],
                     "paths": ["app.py"], "timeout": 10},
            "other": {"argv": [sys.executable, "-c", "pass"], "paths": ["web/*"]},
        }
        self.key = "disposable-test-key-0123456789"
        engine_base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
        engine_prefix = os.environ.get("STARWISH_GRAPHFLOW_PREFIX") or str(engine_base / "StarWish" / f"graphflow-{graphflow.VERSION}")
        env = patch.dict(os.environ, {
            "LOCALAPPDATA": str(root / "state"), "STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo),
            "STARWISH_GRAPHFLOW_EDIT_QUEUE": "", "STARWISH_GRAPHFLOW_OPERATOR_KEY": self.key,
            runner.PROFILES_ENV: json.dumps(self.config), "STARWISH_GRAPHFLOW_TEST_AGENT_LOGS": "",
            graphflow_legacy_observations.ENV: "",
            "STARWISH_GRAPHFLOW_PREFIX": engine_prefix,
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(graphflow._clear_snapshot)

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def plan(self, name="unit"):
        return next(item for item in runner.plan(self.repo)["profiles"] if item["profile"] == name)

    def test_log_reads_do_not_create_or_change_database(self):
        path = runner.state_path(self.repo, "-tests", create=False)
        unknown = "f" * 32
        self.assertEqual(runner.records(self.repo), [])
        self.assertIsNone(runner.latest_finished(self.repo))
        for lookup in (runner.logs, runner.evidence_record):
            with self.assertRaises(GraphFlowError) as missing:
                lookup(self.repo, unknown)
            self.assertEqual(missing.exception.status_code, 404)
        self.assertFalse(path.parent.exists())
        now = runner.time.time()
        expired, completed, interrupted, running = ("a" * 32, "b" * 32, "c" * 32, "d" * 32)
        with runner._db(self.repo) as db:
            db.executemany("""INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)""", [
                (expired, "unit", "passed", now - 90000, now - 80000, 1, 0, "expired", 0, "fixture"),
                (completed, "unit", "passed", now - 300, now - 200, 1, 0, "previous output", 0, "fixture"),
                (interrupted, "unit", "running", now - 30, now - 5, None, None, "", 0, "fixture"),
                (running, "unit", "running", now - 3, now + 60, None, None, "", 0, "fixture"),
            ])
        before = path.read_bytes()
        self.assertEqual([row["state"] for row in runner.records(self.repo)], ["running", "interrupted", "passed"])
        self.assertEqual(runner.latest_finished(self.repo)["state"], "interrupted")
        self.assertEqual(runner.logs(self.repo, completed)["text"], "previous output")
        self.assertEqual(runner.evidence_record(self.repo, interrupted)["state"], "interrupted")
        with patch.object(runner, "_RETENTION_RUNS", 2):
            with self.assertRaises(GraphFlowError) as over_limit:
                runner.logs(self.repo, completed)
            self.assertEqual(over_limit.exception.status_code, 404)
        for hidden in (expired, running):
            with self.assertRaises(GraphFlowError) as missing:
                runner.evidence_record(self.repo, hidden)
            self.assertEqual(missing.exception.status_code, 404)
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_TEST_AGENT_LOGS": "1"}):
            with self.assertRaises(GraphFlowError) as active:
                gf_exec(handle="gfo:" + running)
            self.assertEqual(active.exception.status_code, 409)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT state FROM runs WHERE id=?", (interrupted,)).fetchone(), ("running",))
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone(), (4,))

    def test_matching_plan_and_stale_approval(self):
        original, = runner.plan(self.repo)["profiles"]
        self.assertEqual(original["matched"], ["app.py"])
        self.assertNotIn("argv", original)
        (self.repo / "new.py").write_text("b = 1\n", encoding="utf-8")
        self.assertEqual(self.plan()["approval_sha256"], original["approval_sha256"])
        self.git("add", "new.py")
        with self.assertRaises(GraphFlowError) as stale:
            runner.run(self.repo, "unit", original["approval_sha256"])
        self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(runner.records(self.repo), [])

    def test_same_path_content_and_index_changes_invalidate_approval(self):
        source = self.repo / "app.py"
        original = self.plan()
        info = source.stat()
        source.write_bytes(b"a = 2\n")
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        modified = self.plan()
        self.assertEqual(modified["matched"], original["matched"])
        self.assertNotEqual(modified["approval_sha256"], original["approval_sha256"])
        self.git("add", "app.py")
        staged = self.plan()
        self.assertEqual(staged["matched"], modified["matched"])
        self.assertNotEqual(staged["approval_sha256"], modified["approval_sha256"])
        with patch.object(runner, "_execute") as execute:
            for obsolete in (original, modified):
                with self.assertRaises(GraphFlowError) as stale:
                    runner.run(self.repo, "unit", obsolete["approval_sha256"])
                self.assertEqual(stale.exception.status_code, 409)
            execute.assert_not_called()
        self.assertFalse(runner.state_path(self.repo, "-tests", create=False).exists())
        source.unlink()
        deleted = self.plan()
        self.assertEqual(deleted["matched"], staged["matched"])
        self.assertNotEqual(deleted["approval_sha256"], staged["approval_sha256"])

    def test_http_approval_and_mcp_read_boundary(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
        prefix = "/api/graphflow/test/"
        auth = {"X-StarWish-Operator-Key": self.key}
        plan, = client.get(prefix + "plan", headers=auth).json()["profiles"]
        self.assertIn(sys.executable, plan["argv"])
        body = {k: plan[k] for k in ("profile", "approval_sha256")}
        self.assertEqual(client.post(prefix + "run", json=body).status_code, 403)
        self.assertEqual(client.post(prefix + "run", json=body, headers={**auth, "Origin": "http://other.test"}).status_code, 403)
        self.assertEqual(client.post(prefix + "run", json={**body, "argv": ["bad"]}, headers=auth).status_code, 422)
        response = client.post(prefix + "run", json=body, headers=auth)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["state"], result["exit_code"]), ("passed", 0))
        page = client.get(prefix + "log", params={"run_id": result["run_id"]}, headers=auth)
        self.assertIn("ok False", page.json()["text"])
        self.assertNotIn("graphflow_test_run", {tool.__name__ for tool in TOOLS})
        with self.assertRaises(GraphFlowError):
            graphflow_test_log(result["run_id"])
        with self.assertRaises(GraphFlowError):
            gf_exec(handle=result["log_handle"], range=[1, 1])
        with self.assertRaises(GraphFlowError) as not_authorized:
            gf_exec(handle=result["observation_handle"], range=[1, 1])
        self.assertEqual(not_authorized.exception.status_code, 403)
        legacy_dir = self.repo / ".graphflow" / "observations"
        legacy_dir.mkdir(parents=True)
        fake_id = "f" * 32
        (legacy_dir / f"{fake_id}.log").write_text("old archive must not be read", encoding="utf-8")
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_TEST_AGENT_LOGS": "1"}):
            self.assertIn("ok False", json.loads(graphflow_test_log(result["run_id"]))["text"])
            self.assertIn("1 | ok False", gf_exec(handle=result["log_handle"], range=[1, 1]))
            self.assertEqual(runner.records(self.repo)[0]["log_handle"], result["log_handle"])
            alias = result["observation_handle"]
            self.assertEqual(alias, "gfo:" + result["run_id"])
            self.assertEqual(runner.records(self.repo)[0]["observation_handle"], alias)
            self.assertIn("1 | ok False", gf_exec(handle=alias, range=[1, 1]))
            self.assertNotIn("ok False", gf_exec(handle=alias, range=[2, 2]))
            with self.assertRaises(GraphFlowError) as not_found:
                gf_exec(handle="gfo:" + fake_id)
            self.assertEqual(not_found.exception.status_code, 404)
            other = self.repo.parent / "other"
            other.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=other, check=True, capture_output=True)
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(other)}):
                with self.assertRaises(GraphFlowError) as wrong_project:
                    gf_exec(handle=alias)
                self.assertEqual(wrong_project.exception.status_code, 404)
            for args in ({"handle": "gfo:0123456789ab"}, {"handle": result["log_handle"], "range": [1, 121]},
                         {"handle": alias, "run": True}, {"handle": alias, "cmd": "status"},
                         {"handle": alias, "rootDir": str(self.repo.parent)}):
                with self.subTest(args=args), self.assertRaises(GraphFlowError):
                    gf_exec(**args)

    def test_legacy_observation_requires_opt_in_and_matching_unlinked_log(self):
        raw = b"first\r\nsecond\nthird"
        digest = hashlib.sha256(raw).hexdigest()[:12]
        handle = f"gfo:{digest}"
        directory = self.repo / ".graphflow" / "observations"
        directory.mkdir(parents=True)
        path = directory / f"{digest}.log"
        path.write_bytes(raw)
        before = path.stat().st_mtime_ns
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_TEST_AGENT_LOGS": "1"}):
            with self.assertRaises(GraphFlowError) as denied:
                gf_exec(handle=handle)
            self.assertEqual(denied.exception.status_code, 403)
        with patch.dict(os.environ, {graphflow_legacy_observations.ENV: "1"}):
            result = gf_exec(handle=handle, range=[2, 2])
            self.assertEqual(result, f"# Observation {handle} (lines 2-2 of 3)\n   2: second")
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertFalse(runner.state_path(self.repo, "-tests", create=False).exists())
            for options in ({"run": True}, {"cmd": "status"}, {"rootDir": str(self.repo.parent)},
                            {"range": [1, 121]}):
                with self.assertRaises(GraphFlowError):
                    gf_exec(handle=handle, **options)
            for bad_handle in ("gfo:../" + digest, f"gfo:{digest}.log"):
                with self.assertRaises(GraphFlowError):
                    gf_exec(handle=bad_handle)
            exterior = self.repo.parent / "original.log"
            exterior.write_bytes(raw)
            path.unlink()
            try:
                path.hardlink_to(exterior)
            except OSError:
                pass  # Some filesystems do not support hard links.
            else:
                with self.assertRaises(GraphFlowError) as linked:
                    gf_exec(handle=handle)
                self.assertEqual(linked.exception.status_code, 403)
                path.unlink()
            for content, status in ((b"forged body", 409), (b"x" * (runner.MAX_LOG_BYTES + 1), 413)):
                path.write_bytes(content)
                with self.assertRaises(GraphFlowError) as rejected:
                    gf_exec(handle=handle)
                self.assertEqual(rejected.exception.status_code, status)

    def test_legacy_observation_matches_pinned_upstream_archive_slice(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status())
            self.skipTest("GraphFlow engine not installed")
        node, entry, _ = graphflow._engine()
        script = r"""
            const d = require(process.argv[1]);
            const rootDir = process.argv[2];
            const stdout = Array.from({length: 105}, (_, i) => 'upstream line ' + (i + 1)).join('\r\n');
            const result = d.dehydrateCommandOutput({
                rootDir, stdout, stderr: '', exitCode: 0, durationMs: 1, command: 'fixture'
            });
            console.log(JSON.stringify({handle: result.handle,
                expected: d.recallArchivedOutput(rootDir, result.handle, 5, 7),
                firstPage: d.recallArchivedOutput(rootDir, result.handle, 1, 101)}));
        """
        upstream = subprocess.run([node, "-e", script, str(entry.with_name("dehydrator.js")), str(self.repo)],
                                  check=True, capture_output=True, encoding="utf-8", timeout=15)
        fixture = json.loads(upstream.stdout)
        self.assertRegex(fixture["handle"], r"^gfo:[0-9a-f]{12}$")
        with patch.dict(os.environ, {graphflow_legacy_observations.ENV: "1"}):
            self.assertEqual(gf_exec(handle=fixture["handle"], range=[5, 7]), fixture["expected"])
            self.assertEqual(gf_exec(handle=fixture["handle"]), fixture["firstPage"])

    def test_affected_tests_follow_imports_and_keep_operator_gate(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.skipTest("GraphFlow engine not installed")
        sources = {
            "source.js": "export const number = 1;\n",
            "middle.js": "import { number } from './source.js';\nexport const result = number;\n",
            "middle.test.js": "import { result } from './middle.js';\nconsole.log(result);\n",
            "unrelated.test.js": "console.log(0);\n",
            "untracked.test.js": "import { number } from './source.js';\nconsole.log(number);\n",
        }
        for name, text in sources.items():
            (self.repo / name).write_text(text, encoding="utf-8")
        self.git("add", "source.js", "middle.js", "middle.test.js", "unrelated.test.js")
        self.git("-c", "user.name=StarWish", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=.git", "commit", "-qm", "fixture")
        (self.repo / "source.js").write_text("export const number = 2;\n", encoding="utf-8")
        profile = {"indirect": {"argv": [sys.executable, "-c", "print('approved')"], "paths": ["middle.test.js"]}}
        with patch.dict(os.environ, {runner.PROFILES_ENV: json.dumps(profile), "STARWISH_GRAPHFLOW_ASSISTANT": "1"}):
            with patch.object(runner, "_execute") as execute:
                report = gf_exec(cmd="affected-tests", changedFiles=["./source.js"])
                self.assertIn("- middle.test.js", report)
                self.assertNotIn("unrelated.test.js", report)
                self.assertNotIn("untracked.test.js", report)
                self.assertIn("PASS [graphflow-status]", gf_exec(cmd="status"))
                self.assertIn("- middle.test.js", agent.run_tool("gf_exec", {})["text"])
                item, = runner.plan(self.repo)["profiles"]
                self.assertTrue(item["via_impact"])
                self.assertEqual(item["matched"], ["middle.test.js"])
                execute.assert_not_called()
            (self.repo / "middle.js").write_text("export const result = 0;\n", encoding="utf-8")
            self.git("add", "middle.js")
            self.git("-c", "user.name=StarWish", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
                     "-c", "core.hooksPath=.git", "commit", "-qm", "remove dependency")
            with self.assertRaises(GraphFlowError) as stale:
                runner.run(self.repo, "indirect", item["approval_sha256"])
            self.assertEqual(stale.exception.status_code, 409)
        self.assertFalse((self.repo / ".graphflow").exists())

    def test_gf_exec_rejects_execution_and_outside_paths(self):
        for args in ({"run": True}, {"cmd": "run-affected-tests"}, {"cmd": "python -m unittest"},
                     {"timeoutMs": 100}, {"changedFiles": ["../source.js"]},
                     {"rootDir": str(self.repo.parent)}, {"range": [1, 2]}):
            with self.subTest(args=args), patch.object(graphflow, "affected_tests") as route, \
                 patch.object(runner, "_execute") as execute, self.assertRaises(GraphFlowError):
                gf_exec(**args)
            route.assert_not_called()
            execute.assert_not_called()

    def test_timeout_failure_and_log_cap(self):
        profiles = {
            "noisy": {"argv": [sys.executable, "-c", "print('x' * 200000); raise SystemExit(9)"]},
            "slow": {"argv": [sys.executable, "-c", "import time; time.sleep(8)"], "timeout": 1},
        }
        with patch.dict(os.environ, {runner.PROFILES_ENV: json.dumps(profiles)}):
            fail = runner.run(self.repo, "noisy", self.plan("noisy")["approval_sha256"])
            self.assertEqual((fail["state"], fail["exit_code"]), ("failed", 9))
            self.assertTrue(fail["truncated"])
            size, cursor = 0, 0
            while cursor is not None:
                page = runner.logs(self.repo, fail["run_id"], cursor)
                self.assertLessEqual(len(page["text"]), runner.MAX_PAGE_CHARS)
                size += len(page["text"])
                cursor = page["next_offset"]
            self.assertEqual(size, runner.MAX_LOG_BYTES)
            slow = runner.run(self.repo, "slow", self.plan("slow")["approval_sha256"])
            self.assertEqual(slow["state"], "timed_out")
            self.assertLess(slow["duration_ms"], 7000)
