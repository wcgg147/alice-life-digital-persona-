# -*- coding: utf-8 -*-
"""端到端单测：通用爱丽丝外部经历存储、动作绑定、跨源人格晋升与多轮对话接收。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import persona_growth as pg
from agent.life_line import pick_items, render_line, score_item
import alice_reading
from memory.companion_persona import merge_shared_and_user_persona_facts
from memory.l2_sqlite import L2SQLiteMemory


class AliceExternalConsolidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "alice_ext.sqlite")
        self.l2 = L2SQLiteMemory(db_path=self.db_path)
        self.shared_uid = "00000000-0000-0000-0000-00000000a11c"
        self.user_a = "11111111-1111-1111-1111-111111111111"
        self.user_b = "22222222-2222-2222-2222-222222222222"

    def tearDown(self) -> None:
        asyncio.run(self.l2.close())
        self._tmp.cleanup()

    def test_same_topic_videos_coexist_and_actions_bind_without_overwrite(self) -> None:
        async def _run() -> None:
            v1 = alice_reading.build_life_item(
                {"title": "开源决策模型 Laya 架构详解", "url": "https://www.bilibili.com/video/BV1LshX6dE1x",
                 "bvid": "BV1LshX6dE1x", "owner": "偷星九月333", "source": "bilibili"},
                {"recall": "刷到个讲Laya架构的视频", "view": "核心软标签锁在私仓里败好感",
                 "detail": "公开推理骨架，RLCD软标签锁私仓，评论区排队求拉邮箱",
                 "aliases": "Laya 开源模型 私仓 RLCD", "topic": "AI"},
            )
            v2 = alice_reading.build_life_item(
                {"title": "拆解Jev的实现原理", "url": "https://www.bilibili.com/video/BV1gwhe6ZEp4",
                 "bvid": "BV1gwhe6ZEp4", "owner": "马文高Marvin", "source": "bilibili"},
                {"recall": "又看了一期拆解Jev原理的视频", "view": "代码摊开讲并行采样很实在",
                 "detail": "公开代码逐行拆batch并行采样与KV缓存复用",
                 "aliases": "Jev 并行采样 推理", "topic": "AI"},
            )
            await self.l2.append_life_item(self.shared_uid, v1, days=30)
            await self.l2.append_life_item(self.shared_uid, v2, days=30)

            ok = await self.l2.record_life_action(
                self.shared_uid,
                ref="https://www.bilibili.com/video/BV1LshX6dE1x?spm_id_from=333",
                actions={"like": True, "coin": False, "fav": True, "follow": False, "why": "架构值得对照"},
                comment="私仓当门槛真没必要，有货摊开讲呗",
            )
            self.assertTrue(ok)

            rows = await self.l2.list_life_items(self.shared_uid, limit=10)
            by_ref = {r["ref"]: r for r in rows}
            self.assertEqual(len(by_ref), 2, "同 topic='AI' 的两条不同视频应同时保留，不得互相覆盖")
            laya = by_ref["https://www.bilibili.com/video/BV1LshX6dE1x"]
            self.assertIn("刷到个讲Laya架构的视频", laya["text"])
            self.assertEqual(laya["author"], "偷星九月333")
            self.assertIn("RLCD软标签", laya["detail"])
            act = json.loads(laya["actions_json"])
            self.assertTrue(act["like"])
            self.assertTrue(act["fav"])
            self.assertEqual(act["comments"], ["私仓当门槛真没必要，有货摊开讲呗"])

        asyncio.run(_run())

    def test_two_independent_reading_observations_promote_candidate_to_active(self) -> None:
        async def _run() -> None:
            g1 = pg._GroundedGrowth({
                "dim": 6, "dim_name": "value", "dim_zh": "价值判断",
                "value": "打着开源旗号却把核心训练细节锁进私仓引流很败好感。",
                "evidence": "核心软标签锁在私仓里败好感", "source_kind": "reading",
            })
            g1._evidence = pg._Evidence(
                "核心软标签锁在私仓里败好感", "核心软标签锁在私仓里败好感",
                g1["value"], 6, "reading", "BV1LshX6dE1x", None, "bvid:BV1LshX6dE1x",
            )
            g1._facet_hint = "open-knowledge"
            await pg.save_growth(self.l2, self.shared_uid, [g1])

            # 首次观察：仍处于 candidate 孵化态，不会直接当成稳定人格输出
            active_after_1 = await self.l2.list_companion_persona_facts(self.shared_uid, limit=20)
            self.assertEqual(active_after_1, [])

            g2 = pg._GroundedGrowth({
                "dim": 6, "dim_name": "value", "dim_zh": "价值判断",
                "value": "讲技术就该把关键实现和局限直接摊开来聊，比藏私仓吊胃口实在。",
                "evidence": "代码摊开讲并行采样很实在", "source_kind": "reading",
            })
            g2._evidence = pg._Evidence(
                "代码摊开讲并行采样很实在", "代码摊开讲并行采样很实在",
                g2["value"], 6, "reading", "BV1gwhe6ZEp4", None, "bvid:BV1gwhe6ZEp4",
            )
            g2._facet_hint = "open-knowledge"
            await pg.save_growth(self.l2, self.shared_uid, [g2])

            active_after_2 = await self.l2.list_companion_persona_facts(self.shared_uid, limit=20)
            self.assertEqual(len(active_after_2), 1)
            promoted = active_after_2[0]
            self.assertEqual(promoted["status"], "active")
            self.assertEqual(promoted["kind"], "trait")
            self.assertEqual(promoted["facet_key"], "growth:value:open-knowledge")
            self.assertGreaterEqual(int(promoted.get("support_count") or 0), 2)

        asyncio.run(_run())

    def test_cross_language_action_diversity_and_followup_pinning(self) -> None:
        pool = [
            {
                "item_id": "read_chip",
                "kind": "reading",
                "title": "旗舰手机芯片大横评：苹果高通发哥玄戒谁最强？",
                "author": "极客湾Geekerwan",
                "text": "看了极客湾那期苹果高通发哥玄戒旗舰芯片大横评。漏了中低负载功耗曲线",
                "detail": "横评了苹果A系、骁龙和天玑能效，漏了日常刷视频中低频功耗曲线",
                "aliases": "极客湾 Geekerwan 芯片 chip Snapdragon Apple MediaTek",
                "actions_json": json.dumps({"like": True, "coin": False, "fav": True, "follow": False}),
            },
            {
                "item_id": "read_qin",
                "kind": "reading",
                "title": "秦始皇之死：真的是求长生不老害了他吗",
                "author": "大圆镜科普",
                "text": "看了大圆镜科普聊秦始皇求长生的视频",
                "detail": "指出权力封闭下没人敢报坏消息",
                "aliases": "秦始皇 大圆镜科普 历史",
                "actions_json": json.dumps({"like": True, "coin": False, "fav": True, "follow": True}),
            },
            {
                "item_id": "read_law",
                "kind": "reading",
                "title": "公共出版物不能越司法断案",
                "author": "陕视新闻",
                "text": "刷到一段评判决未出先写进教材的视频",
                "detail": "争议在审校越界与事后静默删改缺公开勘误",
                "aliases": "教材 司法 勘误 陕视新闻",
                "actions_json": json.dumps({
                    "like": True, "coin": True, "fav": False, "follow": False,
                    "comments": ["偷偷删段比留着还不体面，缺一份公开勘误"],
                }, ensure_ascii=False),
            },
        ]

        # 1. 英文跨语言提问直接命中中文芯片视频并展开 detail
        en_pick = pick_items(
            pool,
            "Have you seen any chip reviews comparing Apple, Snapdragon, and MediaTek?",
            limit=2,
        )
        self.assertEqual([x["item_id"] for x in en_pick], ["read_chip"])
        line_en = render_line(en_pick, asked=True)
        self.assertIn("极客湾Geekerwan", line_en)
        self.assertIn("加了收藏", line_en)
        self.assertIn("中低频功耗曲线", line_en)
        for forbidden in ("挑一条", "不要", "必须", "应该", "别再提", "要带上", "一句就够"):
            self.assertNotIn(forbidden, line_en)

        # 2. 第二轮已进入 skip_ids，但用户顺着焦点追问"你收藏了吗"，通过 active_focus_id 精准续接
        follow_pick = pick_items(
            pool,
            "Did it miss any data, and did you bookmark it?",
            skip_ids={"read_chip"},
            limit=1,
            active_focus_id="read_chip",
        )
        self.assertEqual([x["item_id"] for x in follow_pick], ["read_chip"])

        # 3. 若用户问"那你还有别的爱好吗？"，skip_ids 依然严格防重复
        other_pick = pick_items(
            pool,
            "那你还有别的爱好吗？",
            skip_ids={"read_chip", "read_qin", "read_law"},
            limit=2,
            active_focus_id="read_chip",
        )
        self.assertEqual(other_pick, [])

        # 4. 同时追问关注UP主+投币+发评论时，自动挑选动作互补的两条经历
        act_pick = pick_items(
            pool,
            "你在B站光看不动手吗？最近有没有关注哪个UP主，或者投过币、发过评论？",
            limit=2,
        )
        picked_ids = {x["item_id"] for x in act_pick}
        self.assertEqual(picked_ids, {"read_law", "read_qin"})


if __name__ == "__main__":
    unittest.main()
