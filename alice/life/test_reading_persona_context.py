"""Reading should use bounded, sourced per-user state without new calls."""
import json
import sys
import types
import unittest
from unittest import mock

import alice_reading


def fact(value="我对煽情说教没耐心", **kwargs):
    return {"persona_key": "alice", "fact_id": "synthetic-source", "status": "active",
            "facet_key": "growth:value:test", "kind": "preference", "source_kind": "conversation",
            "value_text": value, "evidence_quote": value, "recorded_at": "2026-10-28T00:00:00Z", **kwargs}


class OpinionContextTests(unittest.TestCase):
    def test_keeps_disagreeable_sourced_view_with_source_and_time(self):
        context = alice_reading.opinion_persona_context([fact()])
        self.assertEqual(context, [{"source": "conversation", "dimension": "value", "at": "2026-10-28",
            "past_expression": "我对煽情说教没耐心", "supporting_quote": "我对煽情说教没耐心"}])

    def test_does_not_reintroduce_seed_or_unavailable_or_foreign_state(self):
        rows = [fact(status="candidate"), fact(hidden_at="now"), fact(valid_to="yesterday"),
                fact(persona_key="other"), fact(source_kind="persona_seed"), fact(evidence_quote=""),
                fact(source_kind="authored_story"), fact(source_kind="weekly")]
        self.assertEqual(alice_reading.opinion_persona_context(rows), [])

    def test_body_emotion_desire_relation_are_not_long_term_reading_lenses(self):
        for dimension in ("body", "emotion", "desire", "relation"):
            with self.subTest(dimension=dimension):
                self.assertEqual(alice_reading.opinion_persona_context([
                    fact("我现在有点困", facet_key=f"growth:{dimension}:test")]), [])

    def test_reading_reaction_keeps_reading_attribution(self):
        got = alice_reading.opinion_persona_context([fact(source_kind="reading")])
        self.assertEqual(got[0]["source"], "reading")

    def test_bounds_entries_fields_and_duplicates(self):
        rows = [fact(value="very long " * 80, evidence_quote="long quote " * 100)] * 2
        rows.extend(fact(value=f"过去的具体表达 {index}") for index in range(20))
        got = alice_reading.opinion_persona_context(rows)
        self.assertLessEqual(len(got), 4)
        self.assertEqual(len({item["past_expression"] for item in got}), len(got))
        self.assertLessEqual(len(json.dumps(got, ensure_ascii=False)), 1600)

    def test_long_negated_expression_is_whole_or_absent(self):
        expression = "我曾经很容易跟着热闹下判断。" * 16 + "但现在已经不再这样判断。"
        got = alice_reading.opinion_persona_context([fact(expression)])
        self.assertTrue(got)
        self.assertEqual(got[0]["past_expression"], expression)
        self.assertEqual(got[0]["supporting_quote"], expression)
        self.assertTrue(got[0]["past_expression"].endswith("但现在已经不再这样判断。"))

    def test_oversized_whole_expression_is_skipped_without_hiding_later_rows(self):
        expression = "我以前的看法有很多限定。" * 80 + "但现在已经改变。"
        got = alice_reading.opinion_persona_context([fact(expression), fact("我现在愿意先听反对意见")])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["past_expression"], "我现在愿意先听反对意见")

    def test_user_confirmed_historical_identity_keeps_its_source(self):
        got = alice_reading.opinion_persona_context([fact(
            "I grew up in a small coastal town.", kind="identity", facet_key="origin",
            source_kind="archived_assistant_disclosure_user_confirmed")])
        self.assertEqual(got[0]["dimension"], "identity")
        self.assertEqual(got[0]["source"], "archived_assistant_disclosure_user_confirmed")


class OpinionInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_write_call_receives_state_in_data_not_system_role(self):
        raw = '{"recall":"有人把普通事讲得很感人","view":"我仍想看具体细节"}'
        llm = mock.AsyncMock(return_value=raw)
        with mock.patch.object(alice_reading, "_llm", llm):
            result = await alice_reading.write_opinion("合成标题", "合成正文", persona_facts=[fact()])
        self.assertEqual(result["view"], "我仍想看具体细节")
        llm.assert_awaited_once()
        messages = llm.call_args.args[0]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertNotIn("我对煽情说教没耐心", messages[0]["content"])
        self.assertIn("我对煽情说教没耐心", messages[1]["content"])
        self.assertIn("合成正文", messages[1]["content"])
        self.assertEqual(llm.call_args.kwargs["max_tokens"], 320)

    async def test_distinct_state_changes_opinion_inputs_without_forcing_changed_view(self):
        calls = []

        async def llm(messages, **kwargs):
            calls.append(messages)
            return '{"recall":"同一件事","view":"暂时没形成看法"}'

        with mock.patch.object(alice_reading, "_llm", llm):
            one = await alice_reading.write_opinion("同标题", "同正文", persona_facts=[fact("我愿意听热闹的争论")])
            two = await alice_reading.write_opinion("同标题", "同正文", persona_facts=[fact("我习惯安静地核对细节")])
        self.assertEqual(one, two)
        self.assertNotEqual(calls[0][1]["content"], calls[1][1]["content"])
        self.assertEqual(calls[0][0], calls[1][0])

    async def test_empty_state_has_no_authored_default(self):
        llm = mock.AsyncMock(return_value='{"recall":"r","view":"v"}')
        with mock.patch.object(alice_reading, "_llm", llm):
            await alice_reading.write_opinion("标题", "正文", persona_facts=[])
        self.assertEqual(llm.call_args.args[0][1]["content"], "标题：标题\n\n内容：正文")

    async def test_empty_body_still_makes_zero_calls(self):
        llm = mock.AsyncMock()
        with mock.patch.object(alice_reading, "_llm", llm):
            result = await alice_reading.write_opinion("标题", "", persona_facts=[fact()])
        self.assertEqual(result, {"recall": "", "view": ""})
        llm.assert_not_awaited()

    async def test_one_reading_reuses_existing_facts_fetch_and_single_opinion_call(self):
        rows = [fact()]
        sink = types.SimpleNamespace(
            list_life_items=mock.AsyncMock(return_value=[]),
            list_companion_persona_facts=mock.AsyncMock(return_value=rows),
            append_life_item=mock.AsyncMock(return_value=True),
        )
        growth = types.ModuleType("persona_growth")  # SPLIT：alice_reading 现按顶层 persona_growth 导入
        growth.judge_growth_from_reading = mock.AsyncMock(return_value=[])
        growth.save_growth = mock.AsyncMock(return_value=0)
        llm = mock.AsyncMock(return_value='{"recall":"合成阅读记录","view":"我想先核对细节"}')
        chosen = {"title": "合成文章", "url": "https://example.invalid/", "source": "rss"}
        with (mock.patch.object(alice_reading, "pick_section", return_value=("rss", 0, "合成板块", 1)),
              mock.patch.object(alice_reading, "fetch_candidates", return_value=[chosen]),
              mock.patch.object(alice_reading, "choose_with_reasons", mock.AsyncMock(return_value={"chosen": chosen})),
              mock.patch.object(alice_reading, "fetch_article", mock.AsyncMock(return_value="同一段合成正文")),
              mock.patch.object(alice_reading, "_llm", llm),
              mock.patch.dict(sys.modules, {"persona_growth": growth})):
            result = await alice_reading._run_once_inner(sink, "synthetic-owner", force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["opinion_persona_context_count"], 1)
        self.assertIn("我对煽情说教没耐心", llm.call_args.args[0][1]["content"])
        llm.assert_awaited_once()
        # Existing reads: taste(80), choice brief(200), existing growth(200).
        self.assertEqual(sink.list_companion_persona_facts.await_count, 3)
        sink.append_life_item.assert_awaited_once()
        growth.judge_growth_from_reading.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
