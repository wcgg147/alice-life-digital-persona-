# -*- coding: utf-8 -*-
"""记忆衰退与槽位多样性：降权不判死，同一段对话不霸占人格包。"""
import sys
import time
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # alice/
for extra in (ROOT, ROOT / "config"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import memory.companion_persona as cp  # noqa: E402
from memory.companion_persona import (  # noqa: E402
    _DECAY_FLOOR, fact_decay_weight, rank_facts,
)


def _fact(kind, days_ago, text, facet=None, source=None, fid=None):
    stamp = datetime.fromtimestamp(time.time() - days_ago * 86400, timezone.utc).isoformat()
    return {
        "fact_id": fid or f"{kind}:{text[:8]}",
        "kind": kind,
        "value_text": text,
        "facet_key": facet or f"{kind}:{text[:6]}",
        "observed_at": stamp,
        "recorded_at": stamp,
        "source_conv_id": source or "conv-1",
        "status": "active",
        "confidence": 0.8,
    }


class DecayWeightTest(unittest.TestCase):
    def test_fresh_fact_weighs_more_than_old_one(self):
        fresh = fact_decay_weight(_fact("experience", 0, "今天读到老全死了"))
        old = fact_decay_weight(_fact("experience", 90, "很久以前读过一段"))
        self.assertGreater(fresh, old)

    def test_identity_decays_much_slower_than_experience(self):
        ident = fact_decay_weight(_fact("identity", 365, "她就是嘴硬心软"))
        exp = fact_decay_weight(_fact("experience", 365, "一年前读过一段"))
        self.assertGreater(ident, exp)
        self.assertGreater(ident, 0.8)

    def test_weight_never_reaches_zero(self):
        ancient = fact_decay_weight(_fact("experience", 3650, "十年前的一句话"))
        self.assertGreaterEqual(ancient, _DECAY_FLOOR)

    def test_missing_timestamp_is_neutral_not_zero(self):
        fact = _fact("opinion", 1, "没有时间戳")
        fact.pop("observed_at")
        fact.pop("recorded_at")
        self.assertAlmostEqual(fact_decay_weight(fact), 0.5)


class RankDecayTest(unittest.TestCase):
    def test_decay_is_what_orders_equally_relevant_facts(self):
        # 两条命中数相同的经历、recorded_at 也相同：关掉衰退时按输入顺序（旧的在前），
        # 打开衰退后必须翻过来。这样断言才对功能有无敏感。
        same_ts = datetime.now(timezone.utc).isoformat()
        stale = _fact("experience", 200, "上次也读到，活着，同一段", "book:h:old", "conv-old")
        fresh = _fact("experience", 0, "今天读了，活着，第二十一块", "book:h:new", "conv-book")
        stale["recorded_at"] = fresh["recorded_at"] = same_ts

        with mock.patch.object(cp, "_DECAY_BONUS", 0.0):
            off = cp.rank_facts([stale, fresh], "活着", 2)
        on = cp.rank_facts([stale, fresh], "活着", 2)

        self.assertEqual(len(on), 2)
        self.assertEqual(off[0]["fact_id"], stale["fact_id"], "关掉衰退时旧的应该在前")
        self.assertEqual(on[0]["fact_id"], fresh["fact_id"], "打开衰退后新的必须在前")


class SlotDiversityTest(unittest.TestCase):
    def test_one_conversation_cannot_fill_the_packet(self):
        facts = [_fact("trait", 0, f"同一场电影的第{i}个看法", f"growth:value:v{i}", "conv-film")
                 for i in range(6)]
        facts += [_fact("preference", 3, "我在读余华《活着》", "book:huozhe:progress", "conv-book")]
        picked = rank_facts(facts, "", 12)
        film = [f for f in picked if f["source_conv_id"] == "conv-film"]
        self.assertLessEqual(len(film), 3)
        self.assertTrue(any(f["source_conv_id"] == "conv-book" for f in picked))

    def test_near_duplicate_facets_are_thinned(self):
        facts = [_fact("trait", 0, f"近义看法{i}", f"growth:value:same-{i}", f"conv-{i}")
                 for i in range(5)]
        picked = rank_facts(facts, "", 12)
        self.assertLessEqual(len(picked), 2)


class PacketCompositionTest(unittest.TestCase):
    def test_trait_slices_are_capped_at_six(self):
        facts = [_fact("trait", 0, f"性格切片{i}", f"growth:fam{i}:x", f"conv-{i}") for i in range(10)]
        facts += [_fact("preference", 1, f"喜好{i}", f"pref:item{i}", f"pconv-{i}") for i in range(4)]
        picked = rank_facts(facts, "", 12)
        traits = [f for f in picked if f["kind"] == "trait"]
        self.assertLessEqual(len(traits), 6)
        self.assertGreater(len(picked), 6)

    def test_recent_experience_keeps_a_slot(self):
        # 12 条新鲜性格切片 + 1 条今天的经历：经历类不该被类型排序挤出去。
        facts = [_fact("trait", 0, f"性格切片{i}", f"growth:fam{i}:x", f"conv-{i}") for i in range(12)]
        facts.append(_fact("experience", 0, "今天读到老全死了", "book:huozhe:c21", "conv-book"))
        picked = rank_facts(facts, "", 12)
        self.assertTrue(any(f["kind"] == "experience" for f in picked))


if __name__ == "__main__":
    unittest.main()
