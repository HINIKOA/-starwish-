import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tools.graphflow_approval_rehearsal import ORIGINAL, UPDATED, evidence


class RehearsalEvidenceTest(unittest.TestCase):
    def test_task_link_must_match_an_in_window_run_and_preserve_task_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            edit_db, test_db, task_db, source = [root / path for path in ("edit.db", "test.db", "task.db", "fixture.py")]
            source.write_bytes(ORIGINAL)
            with closing(sqlite3.connect(edit_db)) as edits, edits:
                edits.execute("CREATE TABLE edit_events (seq INTEGER PRIMARY KEY, preview_id TEXT, state TEXT, at REAL)")
                edits.execute("CREATE TABLE edits (id TEXT, path TEXT, base TEXT, result TEXT, state TEXT)")
                edits.execute("INSERT INTO edits VALUES('preview','fixture.py',?,?,'rolled_back')",
                              (hashlib.sha256(ORIGINAL).hexdigest(), hashlib.sha256(UPDATED).hexdigest()))
                edits.executemany("INSERT INTO edit_events(preview_id,state,at) VALUES('preview',?,?)",
                                  [("pending", 1), ("applying", 4), ("applied", 5), ("rolling_back", 9), ("rolled_back", 15)])
            with closing(sqlite3.connect(test_db)) as runs, runs:
                runs.execute("CREATE TABLE runs (id TEXT,profile TEXT,state TEXT,exit_code INT,started REAL,duration_ms INT)")
                runs.execute("INSERT INTO runs VALUES ('in-window','rehearsal','passed',0,7,300)")
            with closing(sqlite3.connect(task_db)) as tasks, tasks:
                tasks.execute("CREATE TABLE tasks (id TEXT,state TEXT,revision INT)")
                tasks.execute("INSERT INTO tasks VALUES ('task','pending',0)")
                tasks.execute("""CREATE TABLE task_verifications
                    (run_id TEXT,task_id TEXT,profile TEXT,state TEXT,exit_code INT,recorded_at REAL)""")
                tasks.execute("INSERT INTO task_verifications VALUES ('other-run','task','rehearsal','passed',0,8)")
                tasks.commit()
                audit = lambda: evidence(edit_db, test_db, "preview", source, task=(task_db, "task"))
                with self.assertRaisesRegex(RuntimeError, "not linked"):
                    audit()
                tasks.execute("UPDATE task_verifications SET run_id='in-window'")
                tasks.commit()
                self.assertTrue(audit())
                tasks.execute("UPDATE tasks SET state='completed',revision=1")
                tasks.commit()
                with self.assertRaisesRegex(RuntimeError, "changed state or revision"):
                    audit()

    def test_requires_passing_test_during_applied_window_and_exact_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            edit_db, test_db, source = [root / path for path in ("edit.db", "test.db", "fixture.py")]
            source.write_bytes(ORIGINAL)
            with closing(sqlite3.connect(edit_db)) as edits, edits, closing(sqlite3.connect(test_db)) as tests, tests:
                edits.execute("CREATE TABLE edit_events (seq INTEGER PRIMARY KEY, preview_id TEXT, state TEXT, at REAL)")
                edits.execute("CREATE TABLE edits (id TEXT, path TEXT, base TEXT, result TEXT, state TEXT)")
                edits.execute("INSERT INTO edits VALUES('preview','fixture.py',?,?,'rolled_back')",
                              (hashlib.sha256(ORIGINAL).hexdigest(), hashlib.sha256(UPDATED).hexdigest()))
                edits.executemany("INSERT INTO edit_events(preview_id,state,at) VALUES('preview',?,?)",
                                  [("pending", 1), ("applying", 4), ("applied", 5)])
                edits.commit()
                self.assertFalse(evidence(edit_db, test_db, "preview", source))
                edits.executemany("INSERT INTO edit_events(preview_id,state,at) VALUES('preview',?,?)",
                                  [("rolling_back", 9), ("rolled_back", 15)])
                edits.commit()
                tests.execute("CREATE TABLE runs (profile TEXT,state TEXT,exit_code INT,started REAL,duration_ms INT)")
                tests.executemany("INSERT INTO runs VALUES ('rehearsal',?,?,?,?)",
                                  [("passed", 0, 2, 100), ("failed", 1, 6, 200), ("passed", 0, 8, 2000)])
                tests.commit()
                with self.assertRaisesRegex(RuntimeError, "No passing controlled test"):
                    evidence(edit_db, test_db, "preview", source)
                tests.execute("INSERT INTO runs VALUES ('rehearsal','passed',0,7,300)")
                tests.commit()
                self.assertTrue(evidence(edit_db, test_db, "preview", source))
                edits.execute("UPDATE edits SET result='wrong'")
                edits.commit()
                with self.assertRaisesRegex(RuntimeError, "expected hashes"):
                    evidence(edit_db, test_db, "preview", source)
                edits.execute("UPDATE edits SET result=?", (hashlib.sha256(UPDATED).hexdigest(),))
                edits.commit()
                source.write_bytes(ORIGINAL + b"\n")
                with self.assertRaisesRegex(RuntimeError, "original bytes"):
                    evidence(edit_db, test_db, "preview", source)
                edits.execute("DELETE FROM edit_events WHERE state='applying'")
                edits.commit()
                with self.assertRaisesRegex(RuntimeError, "audit order"):
                    evidence(edit_db, test_db, "preview", source)
