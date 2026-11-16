"""Exercise pre-save and manual classification with an isolated store and fake upstream."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="starwish-classify-") as tmp:
        os.environ.update(STARWISH_SOUL_DIR=tmp, STARWISH_NO_EMBED_BOOT="1",
                          STARWISH_NO_WATCH="1", STARWISH_NO_DAILY="1")
        from fastapi.testclient import TestClient
        from app import archive, config, maintenance, organize, stars, upstream
        from app.server import app

        legacy = stars.add_star("历史记录：周六学习日语", tags=["原有标签"])
        manual = stars.add_star("每周游泳", major="手工分类", minor="个人习惯")
        healthy = stars.add_star("已有完整分类和标签", tags=["保留"], major="归档")
        config.update_config({"llm": {"maint_base_url": "http://127.0.0.1:9/v1",
                                      "maint_api_key": "fixture-maint", "maint_protocol": "responses",
                                      "maintenance_model": "fixture-organizer"}})
        c = TestClient(app)
        seen = []

        def fake_complete(protocol, base, key, body, timeout):
            assert (protocol, base, key, body["model"]) == (
                "responses", "http://127.0.0.1:9/v1", "fixture-maint", "fixture-organizer")
            messages = body["messages"]
            if messages[0]["role"] == "system" and "只为这条记忆分类" in messages[0]["content"]:
                content = json.loads(messages[-1]["content"])["content"]
                assert all(s["content"] != content for s in stars.list_stars(200))
                seen.append("new")
                result = {"major": "阅读", "minor": "科幻", "tags": ["小说"]}
            else:
                assert "待分类的星" in messages[-1]["content"]
                seen.append("batch")
                result = {"assign": [{"id": legacy["id"], "major": "学习", "minor": "语言", "tags": ["日语"]},
                                     {"id": manual["id"], "major": "不应覆盖", "tags": ["游泳"]},
                                     {"id": healthy["id"], "major": "不应写入", "tags": ["不应写入"]}],
                          "merge": [{"from_major": "手工分类", "to_major": "不应覆盖"}]}
            return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}

        with patch.object(upstream, "complete", side_effect=fake_complete):
            r = c.post("/api/stars", json={"content": "读完一部科幻小说", "tags": ["我选的标签"]})
            s = r.json()
            assert r.status_code == 200 and (s["cat_major"], s["cat_minor"]) == ("阅读", "科幻")
            assert s["tags"] == ["我选的标签", "小说"]
            assert c.post("/api/stars", json={"content": s["content"]}).json()["duplicate"]
            assert seen == ["new"]
            status = c.get("/api/maintenance/status").json()
            assert status["llm_ready"] and not status["pending"] and status["pending_classification"] == 2
            r = c.post("/api/maintenance/consolidate?background=false")
            assert r.status_code == 200 and r.json()["ok"], r.text
            assert (r.json()["assigned"], r.json()["tagged"], r.json()["pending"]) == (1, 2, 0)
            assert seen == ["new", "batch"]
            l = stars.get_star(legacy["id"])
            m = stars.get_star(manual["id"])
            assert (l["cat_major"], l["cat_minor"], l["tags"]) == ("学习", "语言", ["原有标签", "日语"])
            assert (m["cat_major"], m["cat_minor"], m["tags"]) == ("手工分类", "个人习惯", ["游泳"])
            assert stars.get_star(healthy["id"])["tags"] == ["保留"]
            assert any(s["id"] == manual["id"] for s in stars.search("游泳"))
            assert organize.pending_classification() == 0

        archive.append_messages([{"role": "user", "content": "我买了一架新的望远镜"}], "fixture-session", "test")
        extracted = {"stars": [{"content": "主人买了一架新望远镜", "major": "兴趣",
                                "minor": "天文", "tags": ["望远镜"]}], "core_proposals": []}
        reply = {"choices": [{"message": {"content": json.dumps(extracted, ensure_ascii=False)}}]}
        with patch.object(upstream, "complete", return_value=reply) as model:
            assert maintenance.consolidate()["added"] == 1
            assert model.call_count == 1
            record = next(s for s in stars.list_stars(200) if s["content"] == extracted["stars"][0]["content"])
            assert (record["cat_major"], record["cat_minor"], record["tags"]) == ("兴趣", "天文", ["望远镜"])

        with patch.object(upstream, "complete", side_effect=RuntimeError("offline")):
            saved = stars.add_star("分类接口故障时仍应保存", tags=["保底"])
            assert not saved["cat_major"] and saved["tags"] == ["保底"]
        print("Pre-save classification, manual backfill, tag search, and fallback passed")


if __name__ == "__main__":
    main()
