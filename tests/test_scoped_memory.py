import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from app import fusion, stars


class ScopedMemoryTest(unittest.TestCase):
    def test_auto_summary_preserves_inline_identifiers(self):
        text = "- 虚构档案编号 STAR-82A7；规则 a > b。"
        summary = stars.auto_summary(text)
        self.assertTrue(summary.startswith("虚构档案编号 STAR-82A7"))
        self.assertEqual(stars.auto_summary("## Setup key: build-7"), "Setup key: build-7")
        self.assertEqual(stars.auto_summary("若值 a > b，则使用分支 #2。"), "若值 a > b，则使用分支 #2。")

    def test_recall_keeps_only_target_leaves_and_unanimous_fusions(self):
        with sqlite3.connect(":memory:") as c:
            c.row_factory = sqlite3.Row
            c.execute("CREATE TABLE stars (id INTEGER, source TEXT, session TEXT, fused_from TEXT, "
                      "summary TEXT, content TEXT, created_at INTEGER, tags TEXT, deleted_at, fused_into)")
            rows = [
                (1, "watch:codex:delegated", "target", []),
                (2, "watch:codex", "other", []),
                (3, "watch:codex:delegated", "target", []),
                (4, "fuse:1,3", "", [1, 3]),
                (5, "fuse:4,2", "", [4, 2]),
                (6, "fuse:4,1", "", [4, 1]),
                (7, "fuse:7,3", "", [7, 3]),
                (8, "fuse:8,404", "", [8, 404]),
            ]
            c.executemany("INSERT INTO stars VALUES (?, ?, ?, ?, 'Brief', 'Content', 100, '[]', NULL, NULL)",
                          [(i, source, sid, json.dumps(parents)) for i, source, sid, parents in rows])
            ranking = {i: float(i) for i, *_ in rows}
            with patch.object(stars, "get_conn", return_value=c), \
                 patch.object(stars, "_candidates", return_value=ranking), \
                 patch.object(stars, "brightness", return_value=1), \
                 patch.object(stars.config, "load_config", return_value={"memory": {}}), \
                 patch.dict(os.environ, {"STARWISH_WATCH_CODEX_ONLY_SESSION": "target"}):
                got = stars.recall("fixture", 500, bump=False)
            self.assertEqual([x["id"] for x in got["items"]], [6, 4, 3, 1])

    def test_current_revision_only_breaks_close_same_subject_ties(self):
        old = {"id": 1, "summary": "蓝檐读书会", "content": "读完再划线",
               "created_at": 100, "cat_major": "阅读"}
        new = {"id": 2, "summary": "蓝檐读书会", "content": "仍带原样纸页，但不用等，可以随手划线",
               "created_at": 101, "cat_major": "阅读"}
        newer_other = {**new, "id": 3, "summary": "纸灯读书会", "created_at": 102}
        base = [(1.33, 1.0, old), (1.31, 1.0, newer_other), (1.29, 1.0, new)]
        for query, lead in [
            ("蓝檐读书会现在读到一半允许画线吗？", 2),
            ("蓝檐读书会以前允许画线吗？", 1),
            ("比较蓝檐读书会现在和以前的规则", 1),
            ("蓝檐读书会有哪些规则？", 1),
        ]:
            scored = base[:]
            stars._prefer_current_revision(query, scored)
            self.assertEqual(scored[0][2]["id"], lead)
        far = [(1.33, 1.0, old), (0.8, 1.0, new)]
        stars._prefer_current_revision("蓝檐读书会现在读到一半允许画线吗？", far)
        self.assertEqual(far[0][2]["id"], 1)

    def test_explicit_works_do_not_match_similarly_named_other_works(self):
        main = {"summary": "", "content": "《松汀邮局》中的蓝绳"}
        sequel = {"summary": "", "content": "《松汀邮局外传》中的白绳"}
        untitled = {"summary": "", "content": "许檐喝不加糖的麦茶"}
        asked = stars._work_titles("《松汀邮局》里的绳子是什么？")
        self.assertTrue(stars._matches_work_titles(asked, main))
        self.assertFalse(stars._matches_work_titles(asked, sequel))
        self.assertTrue(stars._matches_work_titles(asked, untitled))
        both = stars._work_titles("比较《松汀邮局》和《松汀邮局外传》")
        self.assertTrue(stars._matches_work_titles(both, main))
        self.assertTrue(stars._matches_work_titles(both, sequel))

    def test_recall_asks_about_the_work_only_when_hits_are_competing(self):
        lead = {"summary": "", "content": "《松汀邮局外传》里，回执系白绳"}
        second = {"summary": "", "content": "《松汀邮局》里，未送出的信件系蓝绳"}
        near = [(1.33, 1.0, lead), (1.32, 1.0, second)]
        far = [(1.33, 1.0, lead), (1.24, 1.0, second)]
        query = "邮局信件上的绳子是什么意思？"
        expected = ["松汀邮局", "松汀邮局外传"]
        self.assertEqual(stars._competing_work_titles(query, near), expected)
        self.assertEqual(stars._competing_work_titles(query, far), [])
        self.assertEqual(stars._competing_work_titles("松汀邮局外传的绳子是什么意思？", near), [])
        self.assertEqual(stars._competing_work_titles("对比两部作品的绳结含义", near), [])
        for budget in [8, 30, 80]:
            self.assertLessEqual(stars.estimate_tokens(stars._clarification_for_works(expected, budget)), budget)

    def test_synthetic_scope_does_not_search_for_a_real_profile(self):
        with patch.object(stars, "get_conn") as conn, \
             patch.object(stars.config, "load_config", return_value={"memory": {}}), \
             patch.dict(os.environ, {"STARWISH_WATCH_CODEX_ONLY_SESSION": "fixture"}):
            got = stars.recall("真实用户对麦茶的喜好是什么？", 180, bump=False)
        self.assertEqual(got["reason"], "synthetic_session_scope")
        self.assertEqual(got["items"], [])
        conn.assert_not_called()

    def test_recall_can_expose_the_answer_hidden_after_the_first_clause(self):
        summary = "《松汀邮局外传》设定在另一个年代；…"
        content = "《松汀邮局外传》设定在另一个年代；邮差许岚只给已经投递的回执系白绳，没送到的一律不系。"
        expanded = stars._excerpt_for_recall("《松汀邮局外传》里的许岚何时给回执系绳？", summary, content)
        self.assertIn("给已经投递的回执系白绳", expanded)
        self.assertEqual(stars._excerpt_for_recall("改规则前给什么回执系白绳？", summary, content), summary)

    def test_automatic_fuse_prefers_the_established_parent_category(self):
        old = {"id": 1, "created_at": 100, "cat_major": "写作", "cat_minor": "小说设定"}
        new = {"id": 2, "created_at": 101, "cat_major": "工作", "cat_minor": "电台"}
        third = {"id": 3, "created_at": 102, "cat_major": "写作", "cat_minor": "叙事"}
        self.assertEqual(fusion.inherited_category([new, old]), ("写作", "小说设定"))
        self.assertEqual(fusion.inherited_category([new, third, old]), ("写作", "小说设定"))

    def test_historical_query_can_surface_the_prior_fused_parent(self):
        with sqlite3.connect(":memory:") as c:
            c.row_factory = sqlite3.Row
            c.execute("CREATE TABLE stars (id INTEGER, created_at, deleted_at, summary, content)")
            c.executemany("INSERT INTO stars VALUES (?, ?, NULL, ?, ?)", [
                (7, 100, "读完才划线", "蓝檐读书会约定读完才划线"),
                (8, 101, "边读边划线", "蓝檐读书会改为边读边划线"),
            ])
            fused = {"fused_from": "[7,8]"}
            got = stars._historical_parent(c, "蓝檐读书会改版前如何划线？", fused)
            self.assertEqual(got["id"], 7)
            self.assertIsNone(stars._historical_parent(c, "蓝檐读书会现在如何划线？", fused))
            self.assertIsNone(stars._historical_parent(c, "纸灯读书会以前如何划线？", fused))

    def test_naive_fusion_keeps_a_specific_rule_instead_of_a_generic_mention(self):
        useful = {"created_at": 100, "summary": "街上无门牌，送信人凭门框木刻鸟辨认住户",
                  "content": "街上无门牌，送信人凭门框木刻鸟辨认住户。"}
        generic = {"created_at": 101, "summary": "有一只木刻鸟", "content": "有一只木刻鸟。"}
        self.assertEqual(fusion._naive([useful, generic])[0], useful["summary"])
        revised = {**generic, "summary": "改为看门牌", "content": "规则改为看门牌，不再根据木刻鸟送信。"}
        self.assertEqual(fusion._naive([useful, revised])[0], revised["summary"])

    def test_revision_fusion_distinguishes_retired_rule_from_latest_exception(self):
        with sqlite3.connect(":memory:") as c:
            c.row_factory = sqlite3.Row
            c.execute("CREATE TABLE stars (id INTEGER, created_at INTEGER, deleted_at, summary TEXT, content TEXT)")
            c.executemany("INSERT INTO stars VALUES (?, ?, NULL, ?, ?)", [
                (12, 100, "《灯塔笔记》林洛听到铃才点灯；…",
                 "《灯塔笔记》林洛听到铃才点灯；原先每天六点点灯的设定作废。"),
                (15, 101, "《灯塔笔记》林洛听到铃才点灯；…",
                 "《灯塔笔记》林洛听到铃才点灯；封港那天即使铃响也不点灯。"),
            ])
            row = {"summary": "《灯塔笔记》林洛听到铃才点灯；…",
                   "content": "（由 2 颗相似的记忆融合）\n合并", "fused_from": "[12,15]"}
            history = stars._historical_parent(c, "《灯塔笔记》最早作废的版本里林洛几点点灯？", row)
            self.assertEqual(history["id"], 12)
            self.assertIn("六点", history["summary"])
            self.assertIn("作废", history["summary"])
            now = "《灯塔笔记》林洛现在封港时会点灯吗？"
            self.assertIsNone(stars._historical_parent(c, now, row))
            excerpt, parent = stars._fallback_fusion_evidence(c, now, row)
            self.assertEqual(parent, 15)
            self.assertIn("封港", excerpt)
            self.assertNotIn("六点", excerpt)

    def test_scoped_fallback_recall_uses_nested_parent_and_fits_a_small_budget(self):
        with sqlite3.connect(":memory:") as c:
            c.row_factory = sqlite3.Row
            c.execute("CREATE TABLE stars (id INTEGER, summary TEXT, content TEXT, fused_from TEXT, deleted_at, created_at INTEGER DEFAULT 0)")
            c.executemany("INSERT INTO stars (id, summary, content, fused_from, deleted_at) VALUES (?, ?, ?, ?, NULL)", [
                (1, "有一只木刻鸟", "（由 2 颗相似的记忆融合）\n合并", "[2,3]"),
                (2, "小说里有木刻鸟", "（由 2 颗相似的记忆融合）\n旧的合并", "[4]"),
                (3, "木刻鸟", "书里写过木刻鸟", "[]"),
                (4, "街上不使用门牌号，送信人凭门框上的木刻鸟辨认住户。", "小说规则", "[]"),
            ])
            row = c.execute("SELECT * FROM stars WHERE id=1").fetchone()
            query = "送信人凭什么辨认住户？"
            summary, evidence = stars._fallback_fusion_evidence(c, query, row)
            self.assertEqual(evidence, 4)
            self.assertIn("木刻鸟", summary)
            self.assertEqual(stars._fallback_fusion_evidence(c, "现在的规则？", row), (row["summary"], None))
        prefix = "#1 2026-11-23 "
        short = stars._clip_recall_summary(prefix, summary, 29, query)
        self.assertIn("送信人", short)
        self.assertIn("木刻鸟", short)
        self.assertLessEqual(stars.estimate_tokens(prefix + short) + 1, 29)


if __name__ == "__main__":
    unittest.main()
