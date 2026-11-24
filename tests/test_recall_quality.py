import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, db, semantic, stars


class RecallQualityTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="starwish-recall-quality-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        paths = patch.multiple(config, SOUL_DIR=root, CONFIG_PATH=root / "config.json", DB_PATH=root / "soul.db",
                               STATE_PATH=root / "state.json", EVENTS_PATH=root / "events.jsonl",
                               ARCHIVE_DIR=root / "archive", SNAPSHOT_DIR=root / "snapshots")
        paths.start()
        self.addCleanup(paths.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("STARWISH_WATCH_CODEX_ONLY_SESSION", None)
        config.CONFIG_PATH.write_text(json.dumps({"memory": {"semantic": False}}), encoding="utf-8")

    def add(self, text, **kw):
        return stars.add_star(text, classify=False, **kw)["id"]

    def test_low_evidence_does_not_recall_or_bump(self):
        ids = [self.add("云杉书院每天校对手稿，译本按编号复核。"),
               self.add("鸭子常在水边觅食。"),
               self.add("纸灯读书会制定了边读边划线的规则。")]
        for q in ("云像鸭子吗？", "鸭子会做光合作用吗？", "木卫三的轨道周期是多少？"):
            with self.subTest(query=q):
                r = stars.recall(q, 180)
                self.assertEqual((r["items"], r["used_tokens"], r["text"]), ([], 0, ""))
        self.assertTrue(stars.search("云像鸭子吗？"), "Panel search still allows exploratory fuzzy hits.")
        self.assertEqual([stars.get_star(i)["recall_count"] for i in ids], [0, 0, 0])

    def test_exact_mixed_and_focused_cjk_queries_still_match(self):
        coffee = self.add("主人每天早上喝冰美式咖啡，不加糖。")
        atlas = self.add("Project Atlas 的上线日期定在 12 月 15 日。")
        coriander = self.add("主人讨厌香菜。")
        self.add("星际导航会有新规则。")
        for q, expected in (("咖啡", coffee), ("Atlas 上线", atlas), ("主人讨厌什么菜", coriander)):
            with self.subTest(query=q):
                self.assertEqual(stars.recall(q, 180, bump=False)["items"][0]["id"], expected)

    def test_work_boundary_and_ambiguous_subject(self):
        main = self.add("《松汀邮局》约定：雨夜回执系蓝绳。")
        side = self.add("《松汀邮局外传》约定：雨夜回执系白绳。")
        for q, expected in (("《松汀邮局》回执系什么绳？", main),
                            ("《松汀邮局外传》回执系什么绳？", side)):
            r = stars.recall(q, 160, bump=False)
            self.assertEqual([x["id"] for x in r["items"]], [expected])
        ambiguous = stars.recall("雨夜回执系什么绳？", 150, bump=False)
        self.assertEqual(ambiguous.get("reason"), "competing_work_titles")
        self.assertEqual(ambiguous["items"], [])
        self.assertEqual(stars.recall("《松汀邮局》冷核聚变推进器是什么？", 180, bump=False)["items"], [])

    def test_semantic_only_hit_stays_but_at_floor_is_not_enough(self):
        coat = self.add("我在低温天气穿羽绒服。")
        distractor = self.add("新书上周正式出版。")
        with (patch.object(semantic, "active", return_value=("small", "fixture", "")),
              patch.object(semantic, "min_sim", return_value=0.50)):
            with patch.object(semantic, "search", return_value={coat: 0.71, distractor: 0.505}):
                r = stars.recall("保暖外套推荐", 180, bump=False)
                self.assertEqual([x["id"] for x in r["items"]], [coat])
            with patch.object(semantic, "search", return_value={distractor: 0.505}):
                self.assertEqual(stars.recall("保暖外套推荐", 180, bump=False)["items"], [])

    def test_budget_checks_actual_emitted_text_including_explicit_zero(self):
        for i in range(5):
            self.add(f"青石测温实验 #{i}：用同一支探针在校准后重复测水温，记录误差和每一轮的对照条件。")
        for budget in (0, 1, 8, 45, 70, 120, 220):
            with self.subTest(budget=budget):
                r = stars.recall("青石测温实验", budget, bump=False)
                self.assertEqual(r["budget_tokens"], budget)
                self.assertEqual(r["used_tokens"], db.estimate_tokens(r["text"]))
                self.assertLessEqual(r["used_tokens"], budget)
        self.assertTrue(stars.recall("青石测温实验", 70, bump=False)["items"])

    def test_no_fts_fallback_does_not_treat_sql_wildcards_as_terms(self):
        self.add("Project Atlas 的上线日期定在 12 月 15 日。")
        with patch.object(db, "FTS_OK", False):
            self.assertEqual(stars.recall("%_", 180, bump=False)["items"], [])
            self.assertTrue(stars.recall("Project Atlas", 180, bump=False)["items"])

    def test_mcp_fallback_is_also_within_the_explicit_budget(self):
        from mcp_server import recall

        self.add("青石测温实验使用校准过的热电偶。")
        for limit in (0, 1, 12, 70):
            for query in ("青石测温实验", "无相关档案"):
                with self.subTest(query=query, limit=limit):
                    self.assertLessEqual(db.estimate_tokens(recall(query, limit)), limit)
