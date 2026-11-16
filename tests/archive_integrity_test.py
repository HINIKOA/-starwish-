"""Archive progress, transactional retries, and provenance use an isolated store."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="starwish-archive-") as root:
        os.environ.update(STARWISH_SOUL_DIR=root, STARWISH_NO_DAILY="1", STARWISH_NO_WATCH="1",
                          STARWISH_NO_EMBED_BOOT="1")
        from fastapi.testclient import TestClient
        from app import archive, config, core, maintenance, stars
        from app.db import get_conn
        from app.server import app
        from mcp_server import recall_detail

        client = TestClient(app)
        day = "2026-11-01"
        archive.append_messages([{"content": "old completed marker"}], "old", "test", day)
        rel = archive.append_messages([{"content": "marker A only"}], "A", "test", day)
        archive.append_messages([{"content": "marker B only"}], "B", "test", day)
        config.update_state(consolidated={rel: 1})
        seen = []

        def model(messages, cfg):
            prompt = messages[0]["content"]
            assert "old completed marker" not in prompt
            assert ("marker A only" in prompt) != ("marker B only" in prompt)
            a = "marker A only" in prompt
            seen.append("A" if a else "B")
            return json.dumps({
                "stars": [{"content": "observe saturn rings" if a else "make fresh apple juice",
                           "tags": ["test"], "major": "hobby"}],
                "core_proposals": [{"section": "preferences", "content": "proposal A" if a else "proposal B"}],
            })

        with patch.object(maintenance, "llm_ready", return_value=True), patch.object(maintenance, "chat", side_effect=model):
            assert maintenance.consolidate(max_chunks=1)["added"] == 1
            assert maintenance.pending_work()[rel] == (2, 3)
            with patch.object(core, "create_proposal", side_effect=RuntimeError("injected commit failure")):
                failed = maintenance.consolidate(max_chunks=1)
                assert not failed["ok"] and failed["chunks"] == 0 and failed["added"] == 0
            assert maintenance.pending_work()[rel] == (2, 3)
            assert [s["session"] for s in stars.list_stars()] == ["A"]
            assert len(core.list_proposals()) == 1
            assert maintenance.consolidate(max_chunks=1)["added"] == 1
            assert maintenance.pending_work() == {}
            assert maintenance.consolidate(max_chunks=1)["chunks"] == 0
            assert seen == ["A", "B", "B"]

        assert len(core.list_proposals()) == 2
        sid = next(s["id"] for s in stars.list_stars() if s["session"] == "B")
        detail = client.get(f"/api/stars/{sid}").json()
        assert detail["raw"] == "user: marker B only" and detail["recall_count"] == 0
        assert "marker B only" in recall_detail(sid) and "marker A only" not in recall_detail(sid)
        assert stars.get_star(sid)["recall_count"] == 2
        legacy = stars.add_star("legacy star with unreliable session", source=f"archive:{rel}", session="B", classify=False)
        assert client.get(f"/api/stars/{legacy['id']}").json()["raw"] == ""
        forged = stars.add_star("untrusted source string", source="archive:config.json", classify=False)
        assert stars.recall_detail(forged["id"], bump=False)["raw"] == ""

        stars.soft_delete(sid)
        assert client.get(f"/api/recall/detail/{sid}").status_code == 404
        assert "make fresh apple juice" not in recall_detail(sid)
        assert stars.get_star(sid)["recall_count"] == 2
        assert client.get(f"/api/stars/{sid}").json()["deleted_at"] is not None
        remote = TestClient(app, client=("192.0.2.10", 50000))
        assert remote.get(f"/api/stars/{sid}").status_code == 403
        assert remote.get("/api/stars?include_deleted=true").status_code == 403
        assert remote.patch(f"/api/stars/{sid}", json={"importance": 4}).status_code == 403
        assert remote.post(f"/api/stars/{sid}/brighten").status_code == 403
        assert remote.post(f"/api/stars/{sid}/restore").status_code == 403
        assert client.post(f"/api/stars/{sid}/restore").status_code == 200
        assert client.get(f"/api/recall/detail/{sid}").status_code == 200
        with get_conn() as c:
            c.execute("UPDATE stars SET fused_into=? WHERE id=?", (legacy["id"], sid))
        assert client.get(f"/api/recall/detail/{sid}").status_code == 404

        with get_conn() as c:
            c.execute("UPDATE stars SET fused_into=NULL WHERE id=?", (sid,))
        p = config.SOUL_DIR / rel
        p.write_text(p.read_text("utf-8").replace("marker B only", "changed archive"), "utf-8")
        assert stars.recall_detail(sid, bump=False)["raw"] == ""

        # Same-session messages may still span multiple chunks.
        raw = json.dumps({"content": "x" * 3200, "session": "long", "source": "test"})
        assert [(start, end) for start, end, *_ in maintenance._chunks([raw, raw, raw], 7)] == [
            (7, 8), (8, 9), (9, 10),
        ]
    print("Archive checkpoint, atomic retry, exact provenance, and recall visibility passed")


if __name__ == "__main__":
    main()
