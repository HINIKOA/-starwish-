"""Exercise history-based macro suggestions against disposable Git repositories."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import graphflow, graphflow_history as history
from app.graphflow_routes import router
from mcp_server import gf_macro


@unittest.skipUnless(shutil.which("git"), "Git is required")
class HistoryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name).resolve() / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        env = patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo), "STARWISH_GRAPHFLOW_WORKFLOWS": ""})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def commit(self, files, value):
        for file in files:
            (self.repo / file).write_text(f"value = {value}\n", encoding="utf-8")
        if files:
            self.git("add", "--", *files)
        self.git("-c", "user.name=StarWish", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=.git", "commit", "--allow-empty", "-qm", f"message-not-in-output-{value}")

    def test_mine_and_workflow_preserve_workspace_and_filter_paths(self):
        shared = ["alpha.py", "\u53f2\u8bb0.py", ".hidden.py"]
        self.commit(shared, 1)
        self.commit([], 2)
        self.commit(shared, 3)
        self.commit([*shared, "bulk.py"], 4)
        self.commit(["alpha.py"], 5)
        self.commit(["retired.py"], 6)
        (self.repo / "retired.py").unlink()
        (self.repo / "untracked.py").write_text("never tracked", encoding="utf-8")
        with patch.dict(os.environ, {"GIT_DIR": str(self.repo / "wrong.git")}), patch.object(history, "MAX_COMMIT_FILES", 3):
            report = json.loads(gf_macro("mine-git", rootDir=str(self.repo)))
            self.assertEqual(report["commits_scanned"], 6)
            self.assertEqual(report["bulk_commits_skipped"], 1)
            self.assertEqual(report["pairs"], [{"files": shared[:2], "co_commit_count": 2, "confidence": 1.0}])
            self.assertFalse(report["persisted"])
            app = FastAPI()
            app.include_router(router)
            client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
            base = "/api/graphflow/workflow/"
            plan = client.post(base + "preview", json={"name": "history-review"}).json()
            self.assertEqual(plan["steps"], [{"op": "cochange"}])
            result = client.post(base + "replay", json={"name": plan["name"], "plan_sha256": plan["plan_sha256"]}).json()
            self.assertEqual(result["state"], "completed")
            self.assertEqual(json.loads(result["steps"][0]["text"]), report)
            self.assertFalse(result["files_written"])
        self.assertFalse((self.repo / ".graphflow").exists())

    def test_empty_history_scoping_and_output_cap(self):
        self.assertEqual(json.loads(gf_macro("mine-git"))["commits_scanned"], 0)
        for invalid in ({"rootDir": str(self.repo.parent)}, {"id": "other"}, {"args": ["--all"]}):
            with self.subTest(invalid=invalid), patch.object(history, "_read_git") as git_read:
                with self.assertRaises(graphflow.GraphFlowError):
                    gf_macro("mine-git", **invalid)
                git_read.assert_not_called()
        self.commit(["long_file_name_for_output_limit.py"], 1)
        with patch.object(history, "MAX_GIT_BYTES", 45), self.assertRaises(graphflow.GraphFlowError) as limited:
            gf_macro("mine-git")
        self.assertEqual(limited.exception.status_code, 413)
