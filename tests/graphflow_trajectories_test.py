"""Replayable read-only bundles from legacy GraphFlow trajectory fixtures."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import graphflow, graphflow_trajectories as trails, graphflow_workflows
from mcp_server import gf_macro


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
                                      graphflow_workflows.ENV: "", graphflow_workflows.LEGACY_ENV: ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(graphflow._clear_snapshot)

    def event(self, items, ts, **kwargs):
        return json.dumps({"sessionId": "private-session", "key": "private-key", "kind": "read", "ok": True,
                           "ts": ts, "items": items, **kwargs})

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
