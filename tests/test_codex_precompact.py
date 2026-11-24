import hashlib
import io
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

from app import config, db, stars
from app.evolve import codex_review
from app.watch import compact
from app.watch import codex_capture
from app.watch.watcher import Watcher
from tools import graphflow_codex

_SESSION = "11111111-2222-3333-4444-0123456789ab"
_DONE = "22222222-2222-3333-4444-0123456789ab"
_ACTIVE = "33333333-2222-3333-4444-0123456789ab"
_ABORTED = "44444444-2222-3333-4444-0123456789ab"
_SUMMARY = "云杉书院使用双盲校对核验译稿。"


class CodexPrecompactTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="starwish-precompact-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.workspace, self.soul, self.home = (self.root / x for x in ("repo", "soul", "codex"))
        (self.workspace / ".git").mkdir(parents=True)
        self.soul.mkdir()
        self.rollout = self.home / "sessions" / "2026" / "11" / f"rollout-2026-11-24T01-02-03-{_SESSION}.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.event = {"hook_event_name": "PreCompact", "trigger": "manual", "turn_id": _ACTIVE,
                      "session_id": _SESSION, "cwd": str(self.workspace), "transcript_path": str(self.rollout)}
        self.queue = self.soul / compact.QUEUE
        self.head = self.row("session_meta", id=_SESSION, cwd=str(self.workspace), source="vscode")
        self.done = self.turn(_DONE)
        self.rollout.write_bytes(self.head + self.done + self.turn(_ABORTED, end="turn_aborted"))

    @staticmethod
    def row(kind, **payload):
        return (json.dumps({"type": kind, "payload": payload}) + "\n").encode()

    def turn(self, tid, end="task_complete", cwd=None):
        def event(name, **values):
            return self.row("event_msg", type=name, turn_id=tid, **values)
        return (event("task_started")
                + self.row("turn_context", turn_id=tid, cwd=str(cwd or self.workspace))
                + event("user_message", message="PRIVATE BODY")
                + event(end, last_agent_message="PRIVATE ANSWER"))

    def stage(self, event=None):
        return compact.stage(self.soul, self.workspace, self.home, event or self.event, graphflow_codex._in_workspace)

    def worker(self):
        paths = patch.multiple(config, SOUL_DIR=self.soul, CONFIG_PATH=self.soul / "config.json",
                               DB_PATH=self.soul / "soul.db", EVENTS_PATH=self.soul / "events.jsonl",
                               ARCHIVE_DIR=self.soul / "archive", SNAPSHOT_DIR=self.soul / "snapshot")
        paths.start()
        self.addCleanup(paths.stop)
        env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "STARWISH_CODEX_COMPACT_WRITEBACK": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.grant()
        return config.load_config()

    def grant(self, approved=1):
        path = self.soul / "graphflow-workspaces.sqlite3"
        key = hashlib.sha256(os.fsencode(os.path.normcase(str(self.workspace)))).hexdigest()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,path TEXT,approved INTEGER)")
            conn.execute("INSERT OR REPLACE INTO workspaces VALUES (?,?,?)", (key, str(self.workspace), approved))

    def reply(self):
        return json.dumps({"items": [{"kind": "fact", "text": _SUMMARY}, {"kind": "fact", "text": "PRIVATE BODY"},
                                     {"kind": "task", "text": "generate a task"}]})

    def test_worker_writes_only_summary_and_normal_capture_does_not_repeat(self):
        self.worker()
        self.assertTrue(self.stage())
        item, = compact.pending(self.soul)
        _, turn = compact.verified_turn(self.soul, self.home, item)
        self.assertEqual(codex_capture.ordinary_key(turn), item.key)
        watcher = Watcher()
        watcher.enqueue("compact", "codex")
        with (patch("app.maintenance.llm_ready", return_value=True),
              patch("app.maintenance.chat", return_value=self.reply()) as model):
            self.assertEqual(watcher.drain(), 1)
            model.assert_called_once()
        self.assertFalse(self.stage(), "A finalized turn must not reopen the queue.")
        with patch("app.watch.extract.collect_items", side_effect=AssertionError("duplicate model call")):
            self.assertEqual(Watcher()._process_turns([turn], None), 0)
        with db.get_conn() as conn:
            self.assertEqual([tuple(row) for row in conn.execute("SELECT content,source FROM stars")],
                             [(_SUMMARY, "watch:codex:precompact")])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 0)
        self.assertIn(_SUMMARY, json.dumps(stars.recall("云杉书院 双盲校对", bump=False), ensure_ascii=False))

    def test_normal_capture_first_retires_compact_reference_without_model(self):
        cfg = self.worker()
        self.assertTrue(self.stage())
        item, = compact.pending(self.soul)
        _, turn = compact.verified_turn(self.soul, self.home, item)
        with (patch("app.watch.extract.collect_items", return_value=[]),
              patch("app.watch.extract.apply_items", return_value={"stars": 0}) as applied,
              patch("app.maintenance.chat", side_effect=AssertionError("duplicate model call"))):
            self.assertEqual(Watcher()._process_turns([turn], None), 1)
            self.assertEqual(codex_capture.drain(cfg), 0)
            applied.assert_called_once()
        self.assertFalse(compact.pending(self.soul))

    def test_in_flight_compact_claim_blocks_normal_capture(self):
        cfg = self.worker()
        self.assertTrue(self.stage())
        item, = compact.pending(self.soul)
        _, turn = compact.verified_turn(self.soul, self.home, item)
        entered, release = threading.Event(), threading.Event()

        def summarize(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.reply()

        with (patch("app.maintenance.chat", side_effect=summarize),
              patch("app.watch.extract.collect_items", side_effect=AssertionError("duplicate analysis")),
              ThreadPoolExecutor(1) as pool):
            result = pool.submit(codex_capture.writeback, item, turn, cfg)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(codex_capture.Busy):
                    codex_capture.normal(turn, cfg, item.key)
            finally:
                release.set()
            self.assertEqual(result.result(timeout=5), 1)
            self.assertIsNone(codex_capture.normal(turn, cfg, item.key))

    def test_worker_retries_rolled_back_write_after_restart(self):
        cfg = self.worker()
        self.assertTrue(self.stage())
        real_write = stars.add_star

        def fail_after_insert(*args, **kw):
            real_write(*args, **kw)
            raise OSError("simulated interrupted transaction")

        with patch("app.maintenance.chat", return_value=self.reply()):
            with patch("app.stars.add_star", side_effect=fail_after_insert):
                self.assertEqual(codex_capture.drain(cfg), 0)
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM stars").fetchone()[0], 0)
            self.assertEqual(len(compact.pending(self.soul)), 1)
            self.assertEqual(codex_capture.drain(cfg), 0, "Failed writes must back off.")
            with (patch("app.maintenance.llm_ready", return_value=True),
                  patch("app.watch.compact.time.time", return_value=time.time() + 301)):
                restarted = Watcher()
                restarted.enqueue("compact", "codex")
                self.assertEqual(restarted.drain(), 1)

    def test_worker_rechecks_grant_after_model_and_rejects_changed_transcript(self):
        cfg = self.worker()
        self.assertTrue(self.stage())
        item, = compact.pending(self.soul)
        original = self.rollout.read_bytes()
        self.rollout.write_bytes(original.replace(b"PRIVATE BODY", b"ALTERED BODY"))
        self.assertIsNone(compact.verified_turn(self.soul, self.home, item))
        self.rollout.write_bytes(original)

        def revoke(*args, **kw):
            self.grant(0)
            return self.reply()

        with patch("app.maintenance.chat", side_effect=revoke):
            self.assertEqual(codex_capture.drain(cfg), 0)
        self.assertFalse(compact.pending(self.soul))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM stars").fetchone()[0], 0)

    def test_review_ingest_failure_preserves_only_its_cursor_and_retries_after_restart(self):
        self.worker()
        self.rollout.write_bytes(self.head + self.done)
        path = str(self.rollout)
        with patch.dict(os.environ, {"STARWISH_CODEX_COMPACT_WRITEBACK": "0",
                                     "STARWISH_WATCH_CODEX_ONLY_SESSION": _SESSION}):
            watcher = Watcher()
            watcher.state.files[path] = {"c": "codex", "o": 0}
            with (patch("app.maintenance.llm_ready", return_value=True),
                  patch("app.watch.extract.process_turn", return_value={}) as normal,
                  patch.object(codex_review, "ingest", side_effect=sqlite3.OperationalError("fixture"))):
                self.assertEqual(watcher.process_file(path), 1)
                self.assertEqual(watcher.drain(), 0)
                normal.assert_called_once()
            fs = watcher.state.files[path]
            self.assertEqual(fs["o"], len(self.head + self.done))
            self.assertEqual(fs["review"]["o"], 0)
            self.assertGreater(fs["review"]["retry"], time.time())
            restarted = Watcher()
            now = time.time() + 61
            with (patch("app.watch.watcher.time.time", return_value=now),
                  patch("app.watch.adapters.discover", return_value=[]),
                  patch.object(codex_review, "spawn", return_value=False),
                  patch("app.watch.extract.process_turn", side_effect=AssertionError("replayed memory capture"))):
                restarted.poll_once(now)
                self.assertEqual(restarted.drain(), 0)
            restored = restarted.state.files[path]
            self.assertEqual(restored["review"]["o"], restored["o"])
            with db.get_conn() as conn:
                row = conn.execute("SELECT turn_id FROM codex_review_turns").fetchone()
            self.assertEqual(tuple(row), (_DONE,))
    def test_preview_is_separate_and_disabled_by_default(self):
        self.assertNotIn("PreCompact", graphflow_codex.preview(self.workspace, self.soul, include_hook=True))
        preview = graphflow_codex.preview(self.workspace, self.soul, include_precompact=True)
        self.assertIn("[[hooks.PreCompact]]", preview)
        self.assertIn("timeout = 2", preview)
        self.assertNotIn("SessionStart", preview)

    def test_only_completed_reference_is_durable_deduplicated_and_scoped(self):
        nested = self.workspace / "other"
        (nested / ".git").mkdir(parents=True)
        with self.rollout.open("ab") as stream:
            stream.write(self.turn("55555555-2222-3333-4444-0123456789ab", cwd=nested))
            stream.write(self.turn(_ACTIVE))
            stream.write(b'{"type":"event_msg","payload":')
        self.assertTrue(self.stage())
        self.assertFalse(self.stage(dict(self.event, trigger="auto")))
        with closing(sqlite3.connect(self.queue)) as db:
            row, = db.execute("SELECT session_id, turn_id, start_byte, end_byte, fingerprint FROM intents").fetchall()
        self.assertEqual(row, (_SESSION, _DONE, len(self.head), len(self.head + self.done),
                               hashlib.sha256(self.done).hexdigest()))
        self.assertNotIn(b"PRIVATE", self.queue.read_bytes())

    def test_newer_typed_user_event_is_limited_to_the_same_thread(self):
        for thread in (_ACTIVE, _SESSION):
            body = self.turn(_DONE).replace(
                self.row("event_msg", type="user_message", turn_id=_DONE, message="PRIVATE BODY"),
                self.row("event_msg", type="item_completed", turn_id=_DONE, thread_id=thread,
                         item={"type": "UserMessage"}))
            self.rollout.write_bytes(self.head + body)
            self.assertEqual(self.stage(), thread == _SESSION)

    def test_only_one_real_insert_when_parallel(self):
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual(sum(pool.map(lambda _: self.stage(), range(4))), 1)

    def test_no_file_for_unknown_incomplete_outside_or_delegated_rollout(self):
        inside_event = self.event
        for changes in ({"cwd": str(self.root)}, {"agent_id": _ACTIVE}, {"session_id": _DONE},
                        {"trigger": []}, {"transcript_path": str(self.root / self.rollout.name)}):
            self.assertFalse(self.stage(dict(inside_event, **changes)))
        self.rollout.write_bytes(self.head + self.turn(_ABORTED, end="turn_aborted"))
        self.assertFalse(self.stage())
        subagent = self.row("session_meta", id=_SESSION, cwd=str(self.workspace), source={"subagent": {}})
        self.rollout.write_bytes(subagent + self.done)
        self.assertFalse(self.stage())
        self.rollout.write_bytes(self.head + self.done + b" " * compact._TAIL_LIMIT)
        self.assertFalse(self.stage())
        self.assertFalse(self.queue.exists())

    def test_hook_fail_open_without_grant_or_when_disk_write_fails(self):
        def hook():
            stream = io.TextIOWrapper(io.BytesIO(json.dumps(self.event).encode()))
            with patch("sys.stdin", stream), patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
                self.assertIsNone(graphflow_codex._compact_hook(self.workspace, self.soul))
        hook()
        self.assertFalse(self.queue.exists())
        with patch.object(graphflow_codex, "_manual_grant", return_value=True):
            with patch.object(compact, "stage", side_effect=sqlite3.OperationalError("simulated disk failure")):
                hook()
            self.assertFalse(self.queue.exists())
            hook()
        self.assertTrue(self.queue.is_file())
