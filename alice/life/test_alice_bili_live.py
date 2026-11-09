# -*- coding: utf-8 -*-
"""Unit tests for Alice Bilibili Live (audio-only + free ASR + danmaku) — no network."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from agent import life_line
import alice_bili_live, alice_loop


class AliceBiliLiveTests(unittest.TestCase):
    def test_parse_bcut_result(self) -> None:
        raw = json.dumps({"utterances": [{"transcript": "C9这波大局观没毛病"}, {"transcript": ""}, {"transcript": "架了一个二打二"}]})
        self.assertEqual(alice_bili_live.parse_bcut_result(raw), "C9这波大局观没毛病，架了一个二打二")
        self.assertEqual(alice_bili_live.parse_bcut_result("not json"), "")

    def test_summarize_danmaku_dedupes_aggregates_and_strips_emotes(self) -> None:
        rows = [
            {"ts": "1", "user": "a", "text": "奶"},
            {"ts": "1", "user": "a", "text": "奶"},          # 同一条重复拉取，应去重
            {"ts": "2", "user": "b", "text": "奶"},
            {"ts": "3", "user": "c", "text": "奶[笑哭]"},
            {"ts": "4", "user": "d", "text": "绿龙真的完蛋了，这把经济崩了"},
        ]
        dm = alice_bili_live.summarize_danmaku(rows)
        self.assertEqual(dm["total"], 4)
        self.assertIn(("奶", 3), dm["top"])
        self.assertIn("绿龙真的完蛋了，这把经济崩了", dm["samples"])

    def test_live_item_platform_cognition_and_no_visual_claims_in_meta(self) -> None:
        room = {"room_id": 1883358196, "title": "Spirit vs Navi", "anchor": "CS-advent", "area": "CS2"}
        written = {"recall": "昨晚挂着B站听了会儿Major的解说", "view": "解说节奏很稳。",
                   "topic": "CS2 赛事", "detail": "解说提到C9架烟二打二。", "aliases": "PGL Major CS2"}
        item = alice_bili_live.build_live_life_item(room, written, asr_ok=True)
        self.assertEqual(item["source"], "bili_live")
        self.assertTrue(item["ref"].startswith("https://live.bilibili.com/1883358196#"))
        self.assertEqual(life_line.detect_item_platform(item), "bili_live")
        rendered = life_line.render_line([{**item, "_matched": True}], asked=True)
        self.assertIn("来源：B站主播「CS-advent」直播《Spirit vs Navi》", rendered)
        self.assertIn("没发弹幕，只挂着听", rendered)
        self.assertNotIn("投币", rendered)
        self.assertNotIn("B站UP主", rendered)

    def test_live_query_prefers_live_item_and_loop_never_sends_live_to_video_api(self) -> None:
        live = {"item_id": "live_x", "source": "bili_live", "kind": "reading", "text": "挂着听了会儿CS2 Major直播",
                "ref": "https://live.bilibili.com/1883358196#2026-10-29", "title": "Spirit vs Navi"}
        video = {"item_id": "read_v", "source": "bilibili", "kind": "reading", "text": "B站刷到一个CS2教学视频",
                 "ref": "https://www.bilibili.com/video/BV1abc", "title": "CS2 投掷物教学"}
        q = "你最近看直播了吗？"
        self.assertGreater(life_line.score_item(live, q), life_line.score_item(video, q))
        self.assertFalse(alice_loop._is_bili_item(live))
        self.assertTrue(alice_loop._is_bili_item(video))

    def test_body_marks_audio_only(self) -> None:
        room = {"room_id": 1, "title": "t", "anchor": "a", "area": "CS2", "parent_area": "网游", "online": 10}
        body = alice_bili_live.build_live_body(room, "", {"top": [], "samples": ["好家伙这波"]}, 90)
        self.assertIn("没看画面", body)
        self.assertIn("只看了弹幕", body)


if __name__ == "__main__":
    unittest.main()
