"""Source budget and receipt protocol regressions."""
import json
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import graphflow, graphflow_read_receipts as receipts, graphflow_workspaces
from app.db import estimate_tokens
from mcp_server import graphflow_read


class ReadReceiptTest(unittest.TestCase):
    root = Path("fixture-project").resolve()
    selection = ("", "src/a.py:1-2", False)
    revision = (b"refs/heads/main", b"snapshot-a")

    def setUp(self):
        override = patch.object(receipts, "_RECEIPTS", OrderedDict())
        override.start()
        self.addCleanup(override.stop)

    def prepare(self, *, budget=256, since="", cursor=""):
        selection = ("", "", False) if cursor else self.selection
        return receipts.prepare(self.root, selection, budget, since, cursor)

    def assertBudget(self, result, budget=256):
        count = estimate_tokens(json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["tokenBudget"]["estimated"], count)
        self.assertLessEqual(count, budget)

    def test_pagination_bounds_the_entire_packet_and_preserves_text(self):
        text = 'alpha("\\\\", "\u5b57")\n' * 240
        page = self.prepare().publish(self.revision, text, partial=True, source_truncated=True)
        self.assertTrue(page["truncated"])
        pieces = []
        while True:
            self.assertBudget(page)
            self.assertTrue(page["sourcePartial"])
            self.assertTrue(page["sourceTruncated"])
            pieces.append(page["text"])
            if not page["truncated"]:
                break
            self.assertNotIn("receiptId", page)
            self.assertTrue(page["text"])
            self.assertLess(len(pieces), 100)
            page = self.prepare(cursor=page["nextCursor"]).reuse(self.revision)
        self.assertEqual("".join(pieces), text)
        self.assertIn("receiptId", page)

    def test_delta_uses_the_full_previous_result_and_falls_back_for_other_reads(self):
        before = ("left \u5b57\n" * 80) + "old" + (" right\n" * 80)
        initial = self.prepare(budget=1400).publish(self.revision, before, partial=False, source_truncated=False)
        previous = initial["receiptId"]
        updated = before.replace("old", "NEW")
        next_revision = (self.revision[0], b"snapshot-b")
        request = self.prepare(since=previous)
        self.assertIsNone(request.reuse(next_revision))
        delta = request.publish(next_revision, updated, partial=False, source_truncated=False)
        self.assertEqual(delta["status"], "delta")
        self.assertEqual(delta["baseReceipt"], previous)
        self.assertBudget(delta)
        span = delta["replace"]
        self.assertEqual(before[:span["start"]] + delta["text"] + before[span["end"]:], updated)
        receipt_id = delta["receiptId"]
        current = self.prepare(since=receipt_id)
        unchanged = current.reuse(next_revision)
        self.assertEqual((unchanged["status"], unchanged["text"]), ("unchanged", ""))
        removed = current.publish((next_revision[0], b"snapshot-c"), before.replace("old", ""),
                                  partial=False, source_truncated=False)
        self.assertEqual((removed["status"], removed["text"]), ("delta", ""))
        forked = request.publish((b"refs/heads/other", b"snapshot-b"), updated,
                                 partial=False, source_truncated=False)
        self.assertEqual(forked["status"], "full")
        wrong_target = receipts.prepare(self.root, ("", "src/b.py", False), 256, previous, "")
        self.assertIsNone(wrong_target.reuse(self.revision))
        wrong_root = receipts.prepare(self.root / "other", self.selection, 256, previous, "")
        self.assertIsNone(wrong_root.reuse(self.revision))

    def test_stale_tampered_cross_project_and_expired_cursors_are_rejected(self):
        with patch.object(receipts.time, "monotonic", return_value=100):
            first = self.prepare().publish(self.revision, "line\n" * 1000, partial=False, source_truncated=False)
            cursor = first["nextCursor"]
            with self.assertRaisesRegex(graphflow.GraphFlowError, "STALE_CURSOR"):
                self.prepare(cursor=cursor).reuse((self.revision[0], b"snapshot-changed"))
            with self.assertRaisesRegex(graphflow.GraphFlowError, "STALE_CURSOR"):
                receipts.prepare(self.root / "other", ("", "", False), 256, "", cursor)
            tampered = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
            with self.assertRaisesRegex(graphflow.GraphFlowError, "Invalid source cursor"):
                self.prepare(cursor=tampered)
            with self.assertRaises(graphflow.GraphFlowError):
                receipts.prepare(self.root, self.selection, 256, "", cursor)
        with patch.object(receipts.time, "monotonic", return_value=401):
            with self.assertRaisesRegex(graphflow.GraphFlowError, "STALE_CURSOR"):
                self.prepare(cursor=cursor)

    def test_unchanged_and_continuation_skip_rendering_but_refresh_the_workspace(self):
        snapshot = SimpleNamespace(files={"src/a.py": b"hash"})
        full = "line\n" * 450
        with graphflow.use_read_workspace(self.root), \
                patch.object(graphflow, "_engine", return_value=("node", Path("slicer.js"), "git")), \
                patch.object(graphflow, "_sync_snapshot", return_value=(snapshot, False, {})) as sync, \
                patch.object(graphflow, "_read_version", return_value=self.revision), \
                patch.object(graphflow, "_call_bridge", return_value=full) as render:
            first = graphflow.read_packet(target=self.selection[1], max_output_tokens=256)
            self.assertTrue(first["truncated"])
            final = graphflow.read_packet(cursor=first["nextCursor"], max_output_tokens=1400)
            self.assertFalse(final["truncated"])
            self.assertEqual(first["text"] + final["text"], full)
            unchanged = graphflow.read_packet(target=self.selection[1], since_receipt=final["receiptId"])
            self.assertEqual(unchanged["status"], "unchanged")
            self.assertEqual(render.call_count, 1)
            self.assertEqual(sync.call_count, 3)

    def test_mcp_applies_workspace_authorization_before_cursor_lookup(self):
        with patch.object(graphflow_workspaces, "mcp_scope",
                          side_effect=graphflow_workspaces.WorkspacePending(self.root)), \
                patch.object(graphflow, "read_packet") as read:
            result = json.loads(graphflow_read(rootDir=str(self.root), cursor="ignored"))
            self.assertEqual(result["code_status"], "workspace_authorization_required")
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
