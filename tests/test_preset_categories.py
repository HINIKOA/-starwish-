import json
import unittest
from unittest.mock import Mock, patch

from app import organize
from app.watch import extract


class PresetCategoriesTest(unittest.TestCase):
    def test_empty_corpus_offers_presets_without_adding_stars(self):
        with patch.object(organize, "categories", return_value=[]):
            catalog = organize.category_text()
        self.assertEqual(len(organize.PRESET_CATEGORIES), 9)
        for name in organize.PRESET_CATEGORIES:
            self.assertIn(f"- {name}：", catalog)
        self.assertIn("（还没有）", catalog)

    def test_model_reuses_presets_and_can_create_new_categories(self):
        fixture = {"llm": {"timeout": 120}}
        with patch.object(organize, "categories", return_value=[
            {"major": "工艺档案", "minor": "装帧", "count": 1}
        ]), patch.object(organize.maintenance, "llm_ready", return_value=True), \
             patch.object(organize.maintenance, "chat") as chat:
            chat.side_effect = [
                '{"major":"\u79d1\u5b66\u5b9e\u9a8c","minor":"\u7269\u7406\u5b9e\u9a8c","tags":["\u6d6e\u529b"]}',
                '{"major":"\u91cd\u8981\u8282\u65e5","minor":"\u4eea\u5f0f","tags":[]}',
            ]
            self.assertEqual(organize.classify_new("盐水浮力对照", "", [], fixture)[:2], ("科学", "物理实验"))
            self.assertEqual(organize.classify_new("地方节日传承档案", "", [], fixture)[:2], ("重要节日", "仪式"))
        sent = json.loads(chat.call_args_list[0].args[0][1]["content"])
        self.assertIn("工艺档案 / 装帧", sent["categories"])
        self.assertIn("先", chat.call_args_list[0].args[0][0]["content"])

    def test_heuristic_prefers_a_recognized_preset_and_protects_root(self):
        self.assertEqual(organize.heuristic(["浮力", "科学"]), ("科学", "浮力"))
        self.assertEqual(organize.heuristic(["偏好"]), ("个人偏好", ""))
        c = Mock()
        self.assertEqual(organize._apply_merge(c, {"from_major": "科学", "to_major": "物理"}), 0)
        c.execute.assert_not_called()

    def test_direct_conversation_can_extract_reusable_science(self):
        self.assertIn("值得复用的可靠知识和方法", extract.PROMPT)
        self.assertIn("不记孤立问答", extract.PROMPT)


if __name__ == "__main__":
    unittest.main()
