# -*- coding: utf-8 -*-
"""Unit tests for Alice Web Novel (Qidian + Fanqie) integration and life_line platform cognition."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from agent import life_line
import persona_growth
import alice_novel
from memory.l2_sqlite import L2SQLiteMemory


class AliceNovelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test_novel_mem.db")
        self.l2 = L2SQLiteMemory(db_path=self.db_path)
        self.shared_uid = "00000000-0000-0000-0000-00000000a11c"

    def tearDown(self) -> None:
        if getattr(self.l2, "_conn", None) is not None:
            self.l2._conn.close()
        self._tmp.cleanup()

    def test_build_qidian_and_fanqie_life_items_and_provenance(self) -> None:
        qd_book = {
            "title": "人族镇守使",
            "author": "白驹易逝",
            "category": "玄幻",
            "latest_chapter": "第三千七百四十七章 断河剑宗上下，一个不留！",
            "progress": "有更新",
            "url": "https://www.qidian.com/book/1026225232/",
        }
        qd_opinion = {
            "recall": "在起点书架追《人族镇守使》最新章",
            "view": "杀伐果断，断河剑宗这段收尾很利落。",
            "topic": "玄幻修真",
            "detail": "沈长青横扫断河剑宗，章评区很多老读者都在催更下界大战与万族博弈。",
            "aliases": "人族镇守使 白驹易逝 沈长青 起点 本章说",
            "action": "track",
        }
        qd_item = alice_novel.build_novel_life_item("qidian", qd_book, qd_opinion)
        self.assertEqual(qd_item["source"], "qidian")
        self.assertEqual(life_line.detect_item_platform(qd_item), "qidian")

        rendered_qd = life_line.render_line([{**qd_item, "_matched": True}], asked=True)
        self.assertIn("来源：起点作家「白驹易逝」作品《人族镇守使》", rendered_qd)
        self.assertIn("加入了书架", rendered_qd)
        self.assertNotIn("B站UP主", rendered_qd)
        self.assertNotIn("未投币", rendered_qd)

        fq_book = {
            "title": "无声1937",
            "author": "潜水的小鱼",
            "progress": "看到第1章",
            "url": "https://fanqienovel.com/page/7643411689316024857",
        }
        fq_opinion = {
            "recall": "在番茄书架看谍战小说《无声1937》",
            "view": "审讯室开局切入点抓得很稳，双面间谍的心理博弈有张力。",
            "topic": "谍战悬疑",
            "detail": "主角在严苛审讯下递出假情报，段评读者都在推测谁是隐藏卧底。",
            "aliases": "无声1937 谍战 审讯室 番茄 段评",
            "action": "track",
        }
        fq_item = alice_novel.build_novel_life_item("fanqie", fq_book, fq_opinion)
        self.assertEqual(fq_item["source"], "fanqie")
        self.assertEqual(life_line.detect_item_platform(fq_item), "fanqie")

        rendered_fq = life_line.render_line([{**fq_item, "_matched": True}], asked=True)
        self.assertIn("来源：番茄作者「潜水的小鱼」作品《无声1937》", rendered_fq)
        self.assertIn("加入了书架", rendered_fq)
        self.assertNotIn("B站UP主", rendered_fq)
        self.assertNotIn("X(Twitter)", rendered_fq)

    def test_novel_intent_scoring_in_life_line(self) -> None:
        qd_item = {
            "item_id": "novel_qd_01",
            "title": "人族镇守使",
            "author": "白驹易逝",
            "text": "在起点书架追《人族镇守使》最新章，杀伐果断",
            "kind": "reading",
            "source": "qidian",
            "ref": "https://www.qidian.com/book/1026225232/",
            "actions_json": '{"like": true, "fav": true}',
        }
        bili_item = {
            "item_id": "read_bili_01",
            "title": "盘点十部封神仙侠小说",
            "author": "网文老书虫",
            "text": "B站刷到老书虫推荐仙侠神作视频",
            "kind": "reading",
            "source": "bilibili",
            "ref": "https://www.bilibili.com/video/BV1novel999",
            "actions_json": '{"like": true, "coin": true}',
        }

        # 问“你在起点书架有在追什么书吗？”必须优先命中起点小说
        score_qd = life_line.score_item(qd_item, "你在起点书架有在追什么书吗？")
        score_bili = life_line.score_item(bili_item, "你在起点书架有在追什么书吗？")
        self.assertGreater(score_qd, score_bili)

        # 问“你看小说吗，最近有什么推书的？”必须同时召回小说条目
        self.assertTrue(life_line.asked_about_her("你平时看小说吗？"))
        score_novel = life_line.score_item(qd_item, "你平时看小说吗，书架上有什么追更的？")
        self.assertGreater(score_novel, 0)

    def test_bottom_line_firewall_blocks_toxic_novel_content(self) -> None:
        toxic_view = "看这种无脑虐女和开盒人肉的反派感觉特别爽，杀光全家才解气"
        self.assertFalse(persona_growth.passes_persona_bottom_line(toxic_view))

        safe_view = "开篇设定很有新意，伏笔埋得深，但后期战力膨胀得有点快。"
        self.assertTrue(persona_growth.passes_persona_bottom_line(safe_view))

    def test_two_stage_qidian_hots_and_fanqie_pua_cleaning(self) -> None:
        raw_qd_chapter = (
            "首页\n书架\n第1章 陈奇\n"
            "1979年，10月末。\n636\n"
            "他叫陈奇，今年20岁，高中毕业没考上大学，在家待业。\n456\n"
            "普通过渡句。\n3\n"
        )
        excerpt, hots = alice_novel._extract_qidian_chapter_and_hots(raw_qd_chapter)
        self.assertIn("他叫陈奇，今年20岁", excerpt)
        self.assertNotIn("636", excerpt)
        self.assertEqual(len(hots), 2)
        self.assertIn("636条本章说热议", hots[0])

        raw_fq = "第1章 审讯\n“姓\ue4ee？”老王拍案\ue525起，在南京夜色里审讯。"
        cleaned_fq = alice_novel.clean_fanqie_pua(raw_fq)
        self.assertNotIn("\ue4ee", cleaned_fq)
        self.assertIn("老王拍案起，在南京夜色里审讯。", cleaned_fq)

        body = alice_novel.format_novel_deep_body({
            "author": "睡觉会变白",
            "category": "都市",
            "intro": "重回1979年的文艺年代。",
            "chapter_title": "第1章 陈奇",
            "chapter_excerpt": excerpt,
            "hot_paragraphs": hots,
        })
        self.assertIn("作品简介：", body)
        self.assertIn("本次点开阅读的章节正文节选（第1章 陈奇）：", body)
        self.assertIn("本章说/段评读者热议最高的段落：", body)


if __name__ == "__main__":
    unittest.main()
