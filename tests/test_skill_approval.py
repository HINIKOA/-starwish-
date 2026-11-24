import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app.evolve import skills
from app.server import app


TOKEN = "fixture-review-owner"
PENDING = "/api/skills/pending"
OWNER = {"X-StarWish-Owner": TOKEN}


def sample(name):
    return f"---\nname: {name}\ndescription: Document a fictional checklist\n---\n# Checklist\nKeep the original sample.\n"


class SkillApprovalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="starwish-skill-approval-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.assertNotEqual(root.resolve(), config.SOUL_DIR.resolve())
        self.events = root / "events.jsonl"
        paths = patch.multiple(
            config, SOUL_DIR=root, CONFIG_PATH=root / "config.json", EVENTS_PATH=self.events,
            ARCHIVE_DIR=root / "archive", SNAPSHOT_DIR=root / "snapshots", _master_passphrase=None,
        )
        paths.start()
        self.addCleanup(paths.stop)
        self.write_config(core={"owner_token": TOKEN}, evolve={"approval": "always"})
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def write_config(self, **data):
        config.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    def stage(self, name="pending-sample"):
        result = skills.manage("create", name, content=sample(name), source="review", trust="agent-created")
        self.assertTrue(result.get("staged"))
        return result["pending_id"]

    def events_of(self, kind):
        return [event for line in self.events.read_text("utf-8").splitlines()
                if (event := json.loads(line))["type"] == kind]

    def test_pending_contents_and_decisions_require_owner(self):
        pid = self.stage()
        for headers in ({}, {"X-StarWish-Owner": "wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get(PENDING, headers=headers).status_code, 403)
                for action in ("approve", "reject"):
                    self.assertEqual(self.client.post(f"{PENDING}/{pid}/{action}", headers=headers).status_code, 403)
        response = self.client.get(PENDING, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual([x["id"] for x in response.json()], [pid])
        self.assertIsNone(skills.find("pending-sample"))
        self.assertEqual(self.client.post(f"{PENDING}/{pid}/reject", headers=OWNER).status_code, 200)
        self.assertEqual(skills.list_pending(), [])
        self.assertIsNone(skills.find("pending-sample"))

    def test_protected_settings_cannot_remove_approval_or_reassign_owner(self):
        for body in ({"core": {"owner_token": ""}}, {"core": {"owner_token": "other"}},
                     {"evolve": {"approval": "auto"}}):
            self.assertEqual(self.client.put("/api/settings", json=body).status_code, 403)
        self.assertEqual(self.client.put("/api/settings", json={"core": None}).status_code, 400)
        self.assertEqual(config.load_config()["core"]["owner_token"], TOKEN)
        self.assertEqual(config.load_config()["evolve"]["approval"], "always")
        response = self.client.put("/api/settings", headers=OWNER, json={"evolve": {"nudge_interval": 12}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config.load_config()["evolve"]["nudge_interval"], 12)

    def test_locked_owner_token_does_not_fall_back_to_loopback(self):
        pid = self.stage()
        self.write_config(core={"owner_token": "enc:fixture"}, secrets_encrypted=True)
        self.assertEqual(self.client.get(PENDING).status_code, 403)
        self.assertEqual(self.client.post(f"{PENDING}/{pid}/approve", headers=OWNER).status_code, 403)
        self.assertEqual([x["id"] for x in skills.list_pending()], [pid])

    def test_failed_apply_retains_item_and_can_retry_when_corrected(self):
        name = "stale-review-sample"
        self.assertTrue(skills.manage("create", name, content=sample(name))["success"])
        staged = skills.manage("patch", name, old_string="Blue paper binds the test notebook.",
                               new_string="Green paper binds the test notebook.",
                               source="review", trust="agent-created")
        pid = staged["pending_id"]
        action = f"{PENDING}/{pid}/approve"
        self.assertEqual(self.client.post(action, headers=OWNER).status_code, 409)
        record, = self.client.get(PENDING, headers=OWNER).json()
        self.assertEqual((record["id"], record["status"]), (pid, "failed"))
        self.assertTrue(record["last_error"])
        self.assertEqual(self.events_of("skill_pending_approve"), [])
        self.assertNotIn("Green paper", skills.view(name, bump_view=False)["content"])
        self.assertTrue(skills.manage("edit", name, content=sample(name) + "Blue paper binds the test notebook.\n")["success"])
        self.assertEqual(self.client.post(action, headers=OWNER).status_code, 200)
        self.assertIn("Green paper", skills.view(name, bump_view=False)["content"])
        self.assertEqual(skills.list_pending(), [])
        self.assertEqual(len(self.events_of("skill_pending_approve")), 1)

    def test_parallel_same_decision_applies_once(self):
        pid = self.stage()
        start = threading.Barrier(2)

        def approve():
            start.wait(timeout=5)
            return self.client.post(f"{PENDING}/{pid}/approve", headers=OWNER).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(approve) for _ in range(2)]
            self.assertCountEqual([x.result(timeout=10) for x in results], [200, 404])
        self.assertEqual(len(self.events_of("skill_pending_approve")), 1)
        self.assertEqual(len(self.events_of("skill_change")), 1)
        self.assertTrue(skills.find("pending-sample"))

    def test_uncertain_apply_blocks_automatic_replay(self):
        pid = self.stage()
        action = f"{PENDING}/{pid}/approve"
        with patch.object(skills, "manage", side_effect=OSError("interrupted after write")) as apply:
            self.assertEqual(self.client.post(action, headers=OWNER).status_code, 409)
            self.assertEqual(skills.list_pending()[0]["status"], "applying")
            self.assertEqual(self.client.post(action, headers=OWNER).status_code, 409)
            self.assertEqual(apply.call_count, 1)
        self.assertEqual(self.events_of("skill_pending_approve"), [])
        self.assertEqual(self.client.post(f"{PENDING}/{pid}/reject", headers=OWNER).status_code, 200)
