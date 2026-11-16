"""Portable manifests are checked against local source before warming."""
import base64
import gzip
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import graphflow, graphflow_artifacts as artifacts
from app.graphflow_routes import router


class ArtifactTest(unittest.TestCase):
    def test_bounded_decode_and_no_paths(self):
        with patch.object(graphflow, "checked_workspace") as workspace:
            for kwargs in ({"inputPath": "private.json"}, {"outputPath": "settings.json"}, {"compression": "zip"}):
                with self.assertRaises(graphflow.GraphFlowError):
                    artifacts.exchange("export", **kwargs)
            workspace.assert_not_called()
        for data in ("invalid", base64.b64encode(b"not gzip").decode(), base64.b64encode(gzip.compress(b"{}")).decode()):
            with self.assertRaises(graphflow.GraphFlowError):
                artifacts._decode(data)
        payload = base64.b64encode(gzip.compress(b"x" * 1024)).decode()
        with patch.object(artifacts, "MAX_PAYLOAD", 512), self.assertRaises(graphflow.GraphFlowError) as over:
            artifacts._decode(payload)
        self.assertEqual(over.exception.status_code, 413)

    @unittest.skipUnless(shutil.which("git"), "Git required")
    def test_local_round_trip_and_source_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            target = repo / "sample.py"
            target.write_bytes(b"def check_sample():\n    return 'source_only_marker'\n")
            (repo / "untracked.py").write_bytes(b"untracked content\n")
            subprocess.run(["git", "add", "sample.py"], cwd=repo, check=True, capture_output=True)
            self.addCleanup(graphflow._clear_snapshot)
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(repo)}):
                if not graphflow.status()["available"]:
                    if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                        self.fail(graphflow.status()["reason"])
                    self.skipTest("GraphFlow engine not installed")
                app = FastAPI()
                app.include_router(router)
                with TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as client:
                    url = "/api/graphflow/artifact"
                    self.assertEqual(client.post(url, json={"mode": "export"}, headers={"Origin": "http://other.test"}).status_code, 403)
                    response = client.post(url, json={"mode": "export"})
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    payload = response.json()["artifact"]
                    self.assertEqual(payload, artifacts.exchange("export")["artifact"])
                    self.assertEqual(list(artifacts._decode(payload)["files"]), ["sample.py"])
                    self.assertNotIn(b"source_only_marker", gzip.decompress(base64.b64decode(payload)))
                    graphflow._clear_snapshot()
                    imported = client.post(url, json={"mode": "import", "artifact": payload}).json()
                    self.assertEqual(imported["warmed"], 1)
                    self.assertIn("source_only_marker", graphflow.read(target="sample.py#check_sample")["text"])
                    target.write_bytes(b"def check_sample():\n    return 'new source'\n")
                    with patch.object(graphflow, "_call_bridge") as bridge:
                        self.assertEqual(client.post(url, json={"mode": "import", "artifact": payload}).status_code, 409)
                        bridge.assert_not_called()
                self.assertFalse((repo / ".graphflow").exists())
