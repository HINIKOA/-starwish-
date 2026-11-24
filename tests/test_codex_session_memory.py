import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tools import graphflow_codex


class CodexSessionMemoryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="starwish-session-memory-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace, self.soul = self.root / "repo", self.root / "soul"
        (self.workspace / ".git").mkdir(parents=True)
        self.soul.mkdir()
        self.event = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(self.workspace)}
        self.core = self.soul / "core.json"
        self.core.write_text(json.dumps({"sections": {
            "identity": {"content": "IDENTITY-FIXTURE"},
            "owner": {"content": "OWNER-NOT-SELECTED"},
            "preferences": {"content": "PREFERENCES-NOT-SELECTED"},
        }}), encoding="utf-8")
        self.db = self.soul / "graphflow-workspaces.sqlite3"
        self.key = hashlib.sha256(os.fsencode(os.path.normcase(str(self.workspace)))).hexdigest()

    def grant(self, mode):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, path TEXT, approved INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS workspace_grant_modes(id TEXT PRIMARY KEY, mode INTEGER)")
            db.execute("INSERT OR REPLACE INTO workspaces VALUES (?, ?, 1)", (self.key, str(self.workspace)))
            db.execute("INSERT OR REPLACE INTO workspace_grant_modes VALUES (?, ?)", (self.key, mode))

    def hint(self, event=None, *, sections=("identity",), client_root=None):
        out = graphflow_codex.session_hint(self.workspace, event or self.event, client_root,
                                          soul=self.soul, core_sections=sections)
        return out["hookSpecificOutput"]["additionalContext"] if out else ""

    def test_default_and_missing_grant_do_not_inject_or_create_files(self):
        self.assertNotIn("IDENTITY-FIXTURE", self.hint())
        self.assertFalse(self.db.exists())
        self.grant(1)
        self.assertNotIn("IDENTITY-FIXTURE", self.hint(sections=()))
        default = graphflow_codex.preview(self.workspace, self.soul, include_hook=True)
        self.assertNotIn("--core-section", default)

    def test_manual_grant_allows_only_selected_data_without_writes(self):
        self.grant(1)
        before = self.core.read_bytes(), self.db.read_bytes()
        value = self.hint()
        self.assertIn("IDENTITY-FIXTURE", value)
        self.assertNotIn("OWNER-NOT-SELECTED", value)
        self.assertNotIn("PREFERENCES-NOT-SELECTED", value)
        self.assertNotIn(str(self.soul), value)
        self.assertEqual((self.core.read_bytes(), self.db.read_bytes()), before)
        configured = graphflow_codex.preview(self.workspace, self.soul, include_hook=True,
                                             core_sections=("identity",))
        self.assertIn("--core-section", configured)
        self.assertIn("--soul", configured)

    def test_automatic_and_revoked_grants_never_inject(self):
        for mode in (2, -1):
            self.grant(mode)
            self.assertNotIn("IDENTITY-FIXTURE", self.hint())

    def test_resume_parent_sibling_and_nested_git_do_not_inject(self):
        self.grant(1)
        self.assertNotIn("IDENTITY-FIXTURE", self.hint(dict(self.event, source="resume")))
        self.assertNotIn("IDENTITY-FIXTURE", self.hint(dict(self.event, cwd=str(self.root)), client_root=self.root))
        sibling = self.root / "sibling"
        sibling.mkdir()
        self.assertEqual(self.hint(dict(self.event, cwd=str(sibling)), client_root=self.root), "")
        child = self.workspace / "nested"
        (child / ".git").mkdir(parents=True)
        self.assertNotIn("IDENTITY-FIXTURE", self.hint(dict(self.event, cwd=str(child))))

    def test_invalid_settings_and_oversized_core_are_safe(self):
        self.grant(1)
        for sections in (("identity", "identity"), ("secret",)):
            with self.assertRaises(ValueError):
                graphflow_codex.preview(self.workspace, self.soul, include_hook=True, core_sections=sections)
        with self.assertRaises(ValueError):
            graphflow_codex.preview(self.workspace, self.soul, core_sections=("identity",))
        self.core.write_text(json.dumps({"sections": {"identity": {"content": "b" * 1000}}}), encoding="utf-8")
        injected = json.loads(self.hint().rsplit("\n", 1)[1])
        self.assertLessEqual(len(injected["identity"]), 600)
        self.core.write_bytes(b"a" * (64 * 1024 + 1))
        self.assertNotIn("User-approved StarWish", self.hint())
