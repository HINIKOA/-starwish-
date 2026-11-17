"""Replayable read-only bundles from legacy and native trajectory events."""
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

from app import agent, graphflow, graphflow_trajectories as trails, graphflow_workflows
from app.graphflow_routes import router
from mcp_server import gf_macro, gf_read


@unittest.skipUnless(shutil.which("git"), "Git is required")
class TrajectoryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name).resolve() / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True, capture_output=True)
        for name in ("a", "b", "c", "d", "untracked"):
            (self.repo / f"{name}.js").write_text(f"export function {name}() {{ return '{name}'; }}\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.js", "b.js", "c.js", "d.js"], cwd=self.repo, check=True, capture_output=True)
        self.log = self.repo / ".graphflow" / "trajectories.jsonl"
        self.log.parent.mkdir()
        env = patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo), trails.ENV: "",
                                      trails.CAPTURE_ENV: "", graphflow_workflows.ENV: "",
                                      graphflow_workflows.LEGACY_ENV: ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(graphflow._clear_snapshot)

    def event(self, items, ts, **kwargs):
        return json.dumps({"sessionId": "private-session", "key": "private-key", "kind": "read", "ok": True,
                           "ts": ts, "items": items, **kwargs})

    def test_native_reads_mine_and_replay_without_source_or_feedback_loop(self):
        if not graphflow.status()["available"]:
            if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.skipTest(graphflow.status()["reason"])
        session = "fixture-private-session-1"
        inputs = ["a.js#a", "b.js:1-1"]
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.repo.parent / "state"),
                                     "STARWISH_GRAPHFLOW_PREFIX": str(graphflow.engine_prefix())}):
            gf_read(inputs, sessionId=session)
            self.assertFalse(trails._state_file(self.repo).parent.exists())
            with patch.dict(os.environ, {trails.CAPTURE_ENV: "1", "STARWISH_GRAPHFLOW_ASSISTANT": "1"}):
                self.assertEqual(json.loads(gf_macro("mine"))["sources"]["native"], "missing")
                self.assertIn("return 'a'", gf_read(inputs, sessionId=session, dedup=True))
                gf_read(inputs, sessionId=session, dedup=True)  # Duplicate result does not increase the count.
                gf_read(["a.js#missing", "b.js"], sessionId=session)
                gf_read(["a.js:7-8", "b.js"], sessionId=session)
                gf_read(["a.js", "b.js"], q="fixture-private-query", sessionId=session)
                self.assertEqual(json.loads(gf_macro("mine"))["events_scanned"], 1)
                assistant = agent.run_tool("gf_read", {"targets": inputs[::-1], "sessionId": session})
                self.assertIn("return 'a'", assistant["text"])
                report = json.loads(gf_macro("mine"))
                entry, = report["candidates"]
                self.assertEqual((entry["targets"], entry["occurrences"]), (inputs, 2))
                worker = subprocess.run(
                    [sys.executable, "-c", 'from mcp_server import gf_macro; print(gf_macro("mine"))'],
                    cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, timeout=10,
                )
                self.assertEqual(json.loads(worker.stdout)["candidates"], [entry])
                app = FastAPI()
                app.include_router(router)
                with TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as client:
                    catalog = client.get("/api/graphflow/workflow/catalog")
                    self.assertEqual(catalog.status_code, 200)
                    self.assertIn(entry["id"], {row["name"] for row in catalog.json()})
                path = trails._state_file(self.repo)
                for private in (session.encode(), b"return 'a'", b"fixture-private-query"):
                    self.assertNotIn(private, path.read_bytes())
                plan = json.loads(gf_macro("preview", id=entry["id"]))
                replay = json.loads(gf_macro("run", id=entry["id"], plan_sha256=plan["plan_sha256"]))
                self.assertEqual(replay["state"], "completed")
                self.assertIn("return 'a'", replay["steps"][0]["text"])
                self.assertEqual(json.loads(gf_macro("mine"))["events_scanned"], 2)
                self.assertEqual(list(self.log.parent.iterdir()), [])
                with patch.object(trails, "MAX_EVENTS", 2):
                    gf_read(["c.js"], sessionId=session)
                    self.assertEqual(len(trails._read_native(self.repo)[1]), 2)
                    self.assertFalse(trails._state_file(self.repo.parent / "other").exists())
                    self.assertEqual(json.loads(gf_macro("mine"))["candidates"], [])
            self.assertEqual(json.loads(gf_macro("mine"))["status"], "disabled")

    def test_operator_pins_read_bundle_then_can_replay_without_trajectory_source(self):
        key = "fixture-pin-operator-key-567890"
        root = self.repo.parent
        env = {"LOCALAPPDATA": str(root / "state"), "STARWISH_GRAPHFLOW_PREFIX": str(graphflow.engine_prefix()),
               "STARWISH_GRAPHFLOW_OPERATOR_KEY": key, trails.ENV: "1"}
        pair, alternate = ["a.js#a", "b.js#b"], ["c.js#c", "d.js#d"]
        self.log.write_text("\n".join(self.event(items, str(n)) for n, items in enumerate(
            (pair, alternate, pair, alternate))), encoding="utf-8")
        app = FastAPI()
        app.include_router(router)
        with patch.dict(os.environ, env), patch.object(trails, "MAX_PINS", 1), TestClient(
            app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)
        ) as client:
            prefix = "/api/graphflow/workflow/"
            headers = {"X-StarWish-Operator-Key": key}
            ids = {tuple(row["targets"]): row["id"] for row in json.loads(gf_macro("mine"))["candidates"]}
            name = ids[tuple(pair)]
            plan = json.loads(gf_macro("preview", id=name))
            payload = {"name": name, "plan_sha256": plan["plan_sha256"]}
            self.assertEqual(client.post(prefix + "pin", json=payload).status_code, 403)
            self.assertEqual(client.post(prefix + "pin", json=payload,
                                         headers={**headers, "Origin": "http://other.test"}).status_code, 403)
            self.assertEqual(client.post(prefix + "pin", json={**payload, "plan_sha256": "0" * 64},
                                         headers=headers).status_code, 409)
            self.assertFalse(trails._state_file(self.repo, suffix="-macros").exists())
            self.assertEqual(client.post(prefix + "pin", json=payload, headers=headers).json()["persistence"], "pinned")
            self.assertEqual(client.post(prefix + "pin", json=payload, headers=headers).status_code, 200)
            other = ids[tuple(alternate)]
            other_plan = json.loads(gf_macro("preview", id=other))
            self.assertEqual(client.post(prefix + "pin", headers=headers,
                                         json={"name": other, "plan_sha256": other_plan["plan_sha256"]}).status_code, 409)
            with self.assertRaises(graphflow.GraphFlowError):
                gf_macro("pin", id=other)
            path = trails._state_file(self.repo, suffix="-macros")
            self.assertNotIn(key.encode(), path.read_bytes())
            self.assertNotIn(b"return 'a'", path.read_bytes())
            with patch.dict(os.environ, {trails.ENV: ""}):
                self.assertEqual(json.loads(gf_macro("mine"))["candidates"], [])
                rows = json.loads(gf_macro("list"))["macros"]
                self.assertEqual(next(row for row in rows if row["name"] == name)["persistence"], "pinned")
                self.assertEqual(json.loads(gf_macro("preview", id=name)), plan)
                sibling = root / "sibling"
                sibling.mkdir()
                subprocess.run(["git", "init", "-q"], cwd=sibling, check=True, capture_output=True)
                with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(sibling)}):
                    self.assertNotIn(name, {row["name"] for row in graphflow_workflows.catalog()})
                if graphflow.status()["available"]:
                    response = client.post(prefix + "replay", json=payload)
                    self.assertEqual(response.json()["state"], "completed")
                    self.assertIn("return 'a'", response.json()["steps"][0]["text"])
                    subprocess.run(["git", "rm", "--cached", "a.js"], cwd=self.repo, check=True, capture_output=True)
                    response = client.post(prefix + "replay", json=payload)
                    self.assertEqual(response.json()["state"], "blocked")
                    self.assertEqual(response.json()["steps"][0]["status"], 404)
                elif os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                    self.fail(graphflow.status()["reason"])
                self.assertEqual(client.post(prefix + "unpin", json={"name": name}).status_code, 403)
                self.assertEqual(client.post(prefix + "unpin", json={"name": name}, headers=headers).status_code, 200)
                self.assertEqual(client.post(prefix + "preview", json={"name": name}).status_code, 404)

    def test_optional_mining_only_surfaces_repeated_tracked_reads(self):
        pair = ["b.js#b", "a.js#a"]
        source = "\n".join([
            self.event(pair, "private-ts-1"), self.event(pair, "private-ts-1"),
            self.event(pair[::-1], "private-ts-2"),
            self.event(["a.js", "untracked.js"], "1"), self.event(["a.js", "untracked.js"], "2"),
            self.event(["a.js", "../outside.js"], "3"), self.event(["a.js", "../outside.js"], "4"),
            self.event(["a.js", "\ud800.js"], "5"), self.event(["a.js", "\ud800.js"], "6"),
            self.event(["c.js", "d.js"], "7", kind="exec"), self.event(["c.js", "d.js"], "8", ok=False),
            self.event(["c.js"], "9"), "{invalid", self.event(["d.js"], "10"),
            self.event(["c.js"], "11", sessionId="other-private-session"),
        ]) + "\n"
        self.log.write_bytes(source.encode("utf-8"))
        with patch.object(trails, "_counts") as counts:
            self.assertEqual(json.loads(gf_macro("mine"))["status"], "disabled")
            counts.assert_called_once_with([])
        with patch.dict(os.environ, {trails.ENV: "1"}):
            report = json.loads(gf_macro("mine"))
            self.assertEqual(report["status"], "ready")
            item, = report["candidates"]
            self.assertEqual(item["targets"], sorted(pair))
            self.assertEqual(item["occurrences"], 2)
            self.assertNotIn("private-", json.dumps(report))
            self.assertFalse(report["persisted"])
            self.assertIn(item["id"], {row["name"] for row in graphflow_workflows.catalog()})
            with patch.object(graphflow, "read_batch") as read:
                plan = json.loads(gf_macro("run", id=item["id"]))
                self.assertEqual(plan["steps"], [{"op": "read_batch", "targets": sorted(pair)}])
                read.assert_not_called()
            self.assertEqual(self.log.read_bytes(), source.encode("utf-8"))
            self.assertFalse((self.log.parent / "macros.json").exists())
            self.assertEqual(list(self.log.parent.iterdir()), [self.log])
            with patch.object(trails, "MAX_BYTES", 16):
                self.assertEqual(json.loads(gf_macro("mine"))["status"], "too_large")

    def test_adjacent_reads_replay_and_disappearing_candidate_stops(self):
        body = "\n".join(self.event([path], str(i)) for i, path in enumerate(["a.js", "b.js", "a.js", "b.js"]))
        self.log.write_bytes(body.encode("utf-8"))
        with patch.dict(os.environ, {trails.ENV: "1"}):
            item, = json.loads(gf_macro("mine"))["candidates"]
            self.assertEqual(item["occurrences"], 2)
            plan = json.loads(gf_macro("preview", id=item["id"]))
            if graphflow.status()["available"]:
                result = json.loads(gf_macro("run", id=item["id"], plan_sha256=plan["plan_sha256"]))
                self.assertEqual(result["state"], "completed")
                self.assertIn("return 'a'", result["steps"][0]["text"])
                self.assertIn("return 'b'", result["steps"][0]["text"])
                self.assertFalse(result["files_written"] or result["test_executed"])
            elif os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                self.fail(graphflow.status()["reason"])
            self.log.write_bytes(b"")
            with patch.object(graphflow, "read_batch") as read, self.assertRaises(graphflow.GraphFlowError):
                gf_macro("run", id=item["id"], plan_sha256=plan["plan_sha256"])
            read.assert_not_called()
            with self.assertRaises(graphflow.GraphFlowError):
                gf_macro("mine", rootDir=str(self.repo.parent))
