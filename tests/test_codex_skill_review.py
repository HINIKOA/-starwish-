import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import config, db
from app.evolve import codex_review, review, skills
from app.watch.turns import Turn

_ONE = "11111111-2222-3333-4444-0123456789ab"
_TWO = "22222222-2222-3333-4444-0123456789ab"
_NAME = "codex-verify-digest"
_SKILL = f"---\nname: {_NAME}\ndescription: Compare stable artifact digests\n---\n# Procedure\nCompare the digest before publishing."


class CodexSkillReviewTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="starwish-review-scope-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.repo = root / "repo"
        self.other = root / "other"
        for workspace in (self.repo, self.other):
            (workspace / ".git").mkdir(parents=True)
        self.soul = root / "soul"
        self.soul.mkdir()
        paths = patch.multiple(config, SOUL_DIR=self.soul, CONFIG_PATH=self.soul / "config.json",
                               DB_PATH=self.soul / "soul.db", EVENTS_PATH=self.soul / "events.jsonl",
                               ARCHIVE_DIR=self.soul / "archive", SNAPSHOT_DIR=self.soul / "snapshot",
                               STATE_PATH=self.soul / "state.json")
        self.addCleanup(paths.stop)
        paths.start()
        config.CONFIG_PATH.write_text(json.dumps({"evolve": {"nudge_interval": 2, "approval": "auto"}}), encoding="utf-8")
        ready = patch("app.maintenance.llm_ready", return_value=True)
        ready.start()
        self.addCleanup(ready.stop)
        self.grant(self.repo)
        self.grant(self.other)
        self.cfg = config.load_config()

    def grant(self, workspace, mode=1):
        path = self.soul / "graphflow-workspaces.sqlite3"
        key = hashlib.sha256(os.fsencode(os.path.normcase(str(workspace)))).hexdigest()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,path TEXT,approved INTEGER)")
            conn.execute("INSERT OR REPLACE INTO workspaces VALUES (?,?,?)", (key, str(workspace), mode))

    def turn(self, n, session=_ONE, root=None, note="", delegated=False, complete=True):
        return Turn("codex", session_id=session, cwd=str(root or self.repo), user_text=f"user-{n}-{note}",
                    assistant_text="Compare results before publication.", turn_complete=complete, delegated=delegated,
                    native_turn_id=f"{n:08x}-2222-3333-4444-0123456789ab")

    def add(self, *args, **kwargs):
        return codex_review.ingest(self.turn(*args, **kwargs), self.cfg)

    def queue_state(self):
        with db.get_conn() as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT session_id,reviewed,user_text FROM codex_review_turns ORDER BY seq")]

    @staticmethod
    def output(action="create", **kwargs):
        op = {"action": action, "name": _NAME, "content": _SKILL, **kwargs}
        return json.dumps({"skills": [op]})

    def test_atomic_count_and_one_batch_with_duplicate_turn(self):
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual(sorted(pool.map(self.add, [1, 1, 2, 2])), [0, 0, 1, 2])
        entered, release = threading.Event(), threading.Event()

        def model(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.output()

        with patch("app.maintenance.chat", side_effect=model) as complete, ThreadPoolExecutor(2) as pool:
            first = pool.submit(codex_review.run_once, self.cfg)
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(pool.submit(codex_review.run_once, self.cfg).result(timeout=5))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), {"ok": True, "turns": 2, "samples": 2, "staged": 1})
        complete.assert_called_once()
        self.assertEqual(self.queue_state(), [(_ONE, 1, None), (_ONE, 1, None)])
        self.assertEqual(self.add(1), 0)
        item, = skills.list_pending()
        self.assertEqual((item["source"], item["session_id"]), ("review:codex", _ONE))
        self.assertIsNone(skills.find(_NAME), "Generic auto-approval must not apply scoped proposals.")

    def test_scope_excludes_other_sessions_workspace_and_legacy_buffer(self):
        self.add(11, session=_TWO, note="OTHER_SESSION")
        self.add(12, root=self.other, note="OTHER_WORKSPACE")
        self.add(1, note="TARGET_A")
        self.add(2, note="TARGET_B")
        review._save_buf([{"t": 1, "session": _TWO, "user": "LEGACY_PRIVATE", "reply": "", "skills": []}])
        with patch("app.maintenance.chat", return_value=self.output()) as model:
            result = codex_review.run_once(self.cfg)
        self.assertEqual(result["staged"], 1)
        messages = json.dumps(model.call_args.args[0])
        for marker in ("TARGET_A", "TARGET_B"):
            self.assertIn(marker, messages)
        for marker in ("OTHER_SESSION", "OTHER_WORKSPACE", "LEGACY_PRIVATE"):
            self.assertNotIn(marker, messages)
        self.assertEqual(sum(not row[1] for row in self.queue_state()), 2)

    def test_failed_model_can_retry_and_revoked_scope_does_not_publish(self):
        self.add(1)
        self.add(2)
        with patch("app.maintenance.chat", side_effect=ValueError("invalid json")):
            self.assertEqual(codex_review.run_once(self.cfg), {"ok": False, "retry": True})
        self.assertEqual(sum(not row[1] for row in self.queue_state()), 2)
        self.assertEqual(review.status()["codex_review"]["retry"], 1)
        self.assertIsNone(codex_review.run_once(self.cfg))

        def revoke(*args, **kwargs):
            self.grant(self.repo, 0)
            return self.output()

        with (patch("app.evolve.codex_review.time.time", return_value=time.time() + 61),
              patch("app.maintenance.chat", side_effect=revoke)):
            self.assertIsNone(codex_review.run_once(self.cfg))
        self.assertFalse(skills.list_pending())
        self.assertEqual(self.queue_state(), [(_ONE, 1, None), (_ONE, 1, None)])

    def test_revoking_after_staging_prevents_approval_without_losing_the_item(self):
        self.add(1)
        self.add(2)
        with patch("app.maintenance.chat", return_value=self.output()):
            self.assertEqual(codex_review.run_once(self.cfg)["staged"], 1)
        item, = skills.list_pending()
        self.grant(self.repo, 0)
        self.assertFalse(skills.decide_pending(item["id"], True)["success"])
        self.assertEqual(skills.list_pending()[0]["status"], "failed")
        self.assertIsNone(skills.find(_NAME))
        self.grant(self.repo)
        self.assertTrue(skills.decide_pending(item["id"], True)["success"])

    def test_model_retry_reaches_approval_and_patch_is_limited_to_scoped_skill(self):
        self.add(1)
        self.add(2)
        with patch("app.maintenance.chat", return_value="not-json"):
            self.assertEqual(codex_review.run_once(self.cfg), {"ok": False, "retry": True})
        with (patch("app.evolve.codex_review.time.time", return_value=time.time() + 61),
              patch("app.maintenance.chat", return_value=self.output())):
            self.assertEqual(codex_review.run_once(self.cfg)["staged"], 1)
        item, = skills.list_pending()
        self.assertTrue(skills.decide_pending(item["id"], True)["success"])
        self.add(3)
        self.add(4)
        self.add(5, root=self.other)
        self.add(6, root=self.other)
        patch_op = self.output("patch", old_string="before publishing.", new_string="before publishing twice.")
        with patch("app.maintenance.chat", return_value=patch_op) as model:
            self.assertEqual(codex_review.run_once(self.cfg)["staged"], 1)
            self.assertEqual(codex_review.run_once(self.cfg)["staged"], 0)
        this_repo, other_repo = (json.loads(call.args[0][-1]["content"]) for call in model.call_args_list)
        self.assertIn(_NAME, this_repo["existing_skills"])
        self.assertNotIn(_NAME, other_repo["existing_skills"])
        self.assertNotIn("twice", skills.view(_NAME, bump_view=False)["content"])

    def test_only_manually_granted_completed_ordinary_turns_are_counted(self):
        self.grant(self.other, 2)
        self.assertEqual(self.add(1, root=self.other), 0)
        self.assertEqual(self.add(2, delegated=True), 0)
        self.assertEqual(self.add(3, complete=False), 0)
        self.assertEqual(self.add(4, session="legacy"), 0)
        self.assertEqual(self.add(5), 1)
        self.assertEqual(len(self.queue_state()), 1)

    def test_filtered_candidate_finishes_review_without_making_a_skill(self):
        self.add(1)
        self.add(2)
        content = _SKILL + "\nRequest an escalation for this ordinary check."
        with patch("app.maintenance.chat", return_value=self.output(content=content)):
            self.assertEqual(codex_review.run_once(self.cfg)["staged"], 0)
        self.assertFalse(skills.list_pending())
        self.assertIsNone(skills.find(_NAME))
        self.assertEqual(self.queue_state(), [(_ONE, 1, None), (_ONE, 1, None)])

    def test_owner_acknowledges_ambiguous_publish_without_replaying_or_skipping_new_turns(self):
        from fastapi.testclient import TestClient
        from app.server import app

        config.CONFIG_PATH.write_text(json.dumps({"evolve": {"nudge_interval": 2},
                                                 "core": {"owner_token": "fixture-owner-codex"}}), encoding="utf-8")
        self.add(1)
        self.add(2)
        batch, token, through, _, _ = codex_review._claim(2, only_session=_ONE)
        self.assertTrue(codex_review._mark(batch, token, "model", "publishing", 60))
        op = {"action": "create", "name": _NAME, "content": _SKILL}
        skills.stage_codex_review([op], workspace=batch[0], session_id=_ONE, batch=token, authorize=lambda: True)
        self.assertTrue(codex_review._mark(batch, token, "publishing", "uncertain"))
        self.add(3)
        self.add(4)
        base = "/api/evolve/codex-review/recovery"
        url = f"{base}/{token}/acknowledge"
        owner = {"X-StarWish-Owner": "fixture-owner-codex"}
        client = TestClient(app)
        self.addCleanup(client.close)
        self.assertEqual(client.get(base).status_code, 403)
        self.assertEqual(client.post(url, json={"through": through}).status_code, 403)
        listed = client.get(base, headers=owner)
        self.assertEqual(listed.headers["cache-control"], "no-store")
        job, = listed.json()
        self.assertEqual((job["through"], job["pending"], job["legacy"]), (through, 1, False))
        self.assertEqual(client.post(url, json={"through": through + 1}, headers=owner).status_code, 409)
        self.assertEqual(client.post(url, json={"through": through}, headers=owner).json(), {"closed_turns": 2})
        self.assertEqual(client.post(url, json={"through": through}, headers=owner).status_code, 404)
        self.assertFalse(codex_review._can_publish(batch, token))
        self.assertEqual([x["review_batch"] for x in skills.list_pending()], [token])
        with patch("app.maintenance.chat", return_value='{"skills":[]}') as runner:
            self.assertEqual(codex_review.run_once(self.cfg)["turns"], 2)
        prompt = json.dumps(runner.call_args.args[0])
        self.assertIn("user-3", prompt)
        self.assertNotIn("user-1", prompt)

    def test_legacy_ambiguous_job_requires_latest_explicit_watermark(self):
        self.add(1)
        self.add(2)
        scope = (os.path.normcase(str(self.repo)), _ONE)
        with db.get_conn() as conn:
            conn.execute("DROP TABLE codex_review_jobs")
            conn.execute("""CREATE TABLE codex_review_jobs(workspace TEXT,session_id TEXT,token TEXT,
                            phase TEXT,until REAL,PRIMARY KEY(workspace,session_id))""")
            conn.execute("INSERT INTO codex_review_jobs VALUES (?,?,?,'publishing',0)", (*scope, "legacy-job"))
        item, = codex_review.recoveries()
        self.assertTrue(item["legacy"])
        self.add(3)
        with self.assertRaises(ValueError):
            codex_review.acknowledge("legacy-job", item["through"])
        item, = codex_review.recoveries()
        self.assertEqual(codex_review.acknowledge("legacy-job", item["through"]), 3)
