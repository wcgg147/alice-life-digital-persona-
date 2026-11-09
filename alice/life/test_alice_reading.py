# -*- coding: utf-8 -*-
"""她自己读东西：确定性部分的单测（不调模型、不联网）。"""
from __future__ import annotations

import unittest
from unittest import mock

import alice_reading


class CleanTitleTests(unittest.TestCase):
    def test_strips_cdata_and_tags(self):
        self.assertEqual(alice_reading._clean_title("<![CDATA[标题<em>加粗</em>]]>"), "标题加粗")

    def test_collapses_whitespace(self):
        self.assertEqual(alice_reading._clean_title("  a \n  b  "), "a b")

    def test_truncates_long(self):
        self.assertLessEqual(len(alice_reading._clean_title("字" * 500)), 120)


class CleanBodyTests(unittest.TestCase):
    def test_drops_site_noise_prefix(self):
        raw = "任何用户都可使用写作功能。成功发布 3 篇符合基本规则的内容，可成为正式作者。真正的正文从这里开始。"
        out = alice_reading._clean_body(raw)
        self.assertNotIn("任何用户都可使用写作功能", out)
        self.assertIn("真正的正文从这里开始", out)

    def test_keeps_normal_body(self):
        self.assertEqual(alice_reading._clean_body("  正常 的 正文 "), "正常 的 正文")


class BuildLifeItemTests(unittest.TestCase):
    def test_prefers_recall_and_joins_with_period(self):
        item = alice_reading.build_life_item(
            {"title": "一个很长的标题！带感叹号"},
            {"recall": "刷到个扒童年BGM的", "view": "数字太唬人了"},
        )
        self.assertEqual(item["text"], "刷到个扒童年BGM的。数字太唬人了")
        self.assertEqual(item["kind"], "reading")
        self.assertEqual(item["source"], "reading")

    def test_no_double_punctuation(self):
        item = alice_reading.build_life_item(
            {"title": "x"}, {"recall": "有人做了个模组。", "view": "挺对我胃口"})
        self.assertNotIn("。，", item["text"])
        self.assertNotIn("。。", item["text"])
        self.assertEqual(item["text"], "有人做了个模组。挺对我胃口")

    def test_without_recall_never_uses_full_title(self):
        """没有 recall 时退化成短标题，绝不把完整长标题塞进去（会变播报）。"""
        long_title = "中国90%的人都听过的童年阴影BGM！20年后我终于扒出了它的真身……SCARSONG终极溯源"
        item = alice_reading.build_life_item({"title": long_title}, {"recall": "", "view": ""})
        self.assertNotIn("终极溯源", item["text"])
        self.assertLessEqual(len(item["text"]), 40)

    def test_text_capped(self):
        item = alice_reading.build_life_item(
            {"title": "t"}, {"recall": "长" * 300, "view": "看" * 300})
        self.assertLessEqual(len(item["text"]), 160)

    def test_aliases_present(self):
        item = alice_reading.build_life_item({"title": "蓝色大肥鱼"}, {"recall": "r", "view": "v"})
        self.assertIn("蓝色大肥鱼", item["aliases"])
        self.assertIn("bilibili", item["aliases"])


class BilibiliParseTests(unittest.TestCase):
    def test_fetch_bilibili_parses_items(self):
        fake = {"code": 0, "data": {"list": [
            {"bvid": "BV1xx", "title": "视频一", "stat": {"view": 1234}},
            {"bvid": "", "title": "无bv跳过"},
            {"bvid": "BV2yy", "title": "视频二", "stat": {"view": 99}},
        ]}}
        with mock.patch.object(alice_reading, "_get_json", return_value=fake):
            got = alice_reading.fetch_bilibili(limit=5, pool=10)
        self.assertTrue(got)
        self.assertTrue(all(x["url"].startswith("https://www.bilibili.com/video/BV") for x in got))
        self.assertTrue(all("views" in x for x in got))

    def test_fetch_bilibili_empty_on_bad_response(self):
        with mock.patch.object(alice_reading, "_get_json", return_value={}):
            self.assertEqual(alice_reading.fetch_bilibili(), [])


class CandidateFallbackTests(unittest.TestCase):
    def test_uses_bilibili_when_available(self):
        fake = [{"title": "B站条", "url": "https://www.bilibili.com/video/BV1", "bvid": "BV1",
                 "views": 1, "source": "bilibili"}]
        with mock.patch.object(alice_reading, "fetch_bilibili", return_value=fake):
            got = alice_reading.fetch_candidates()
        self.assertEqual(got, fake)


if __name__ == "__main__":
    unittest.main()
