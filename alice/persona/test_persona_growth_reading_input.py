# -*- coding: utf-8 -*-
"""Reading growth input tests using in-memory collaborators only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import persona_growth as growth

API = Path(__file__).resolve().parents[1]
assert Path(growth.__file__).resolve() == (API / "persona/persona_growth.py").resolve()

# persona_growth currently inserts the main checkout into sys.path. Load the
# reading module explicitly from this test's checkout and stub dynamic imports.
spec = importlib.util.spec_from_file_location(
    "isolated_reading_for_persona_test", API / "life/alice_reading.py"
)
reading = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reading)
assert Path(reading.__file__).resolve() == (API / "life/alice_reading.py").resolve()

OPINION = "我对煽情说教越来越没耐心，我更想自己判断。"
ARTICLE = "外部作者觉得所有人都应该始终热情乐观。"


class FakeClient:
    def __init__(self):
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        system = messages[0]["content"]
        if system.startswith(reading.CHOOSE_SYSTEM):
            return json.dumps({
                "pick": 1, "why_pick": "想看看",
                "judgments": [{"i": 1, "open": True, "why": "这个话题我在意"}],
            }, ensure_ascii=False)
        if system == reading.MERGE_SYSTEM:
            return "我更愿意自己判断内容，不喜欢被说教。"
        if system == reading.OPINION_SYSTEM:
            return json.dumps({"recall": "读到一篇讨论观点的文章", "view": OPINION}, ensure_ascii=False)
        if system == growth.JUDGE_SYSTEM:
            # Ground the model stub in the actual Alice turn under test. The
            # old fixed OPINION was deliberately absent in long/English cases.
            lines = messages[1]["content"].splitlines()
            quote = next((line.removeprefix("Alice: ").removeprefix("她读完的想法：")
                          for line in lines if line.startswith("Alice: ")), "")
            if not quote or quote == "（没有明确看法）":
                return json.dumps({"changes": []})
            return json.dumps({"changes": [{
                "dim": 6, "value": quote[:100], "evidence": quote[:100],
            }]}, ensure_ascii=False)
        raise AssertionError("Unexpected model call")


class FakeMemory:
    def __init__(self):
        self.facts = [
            {"facet_key": "media_taste:bilibili", "value_text": "我以前比较愿意听别人建议。"},
            {"facet_key": "growth:value:old", "value_text": "我以前更容易认同说教。"},
        ]
        self.life_items = []
        self.saved_growth = []

    async def list_companion_persona_facts(self, user_id, limit=48):
        return list(self.facts[:limit])

    async def set_persona_fact_value(self, user_id, fact_id, value, **kwargs):
        self.facts[0]["value_text"] = value
        return 1

    async def append_life_item(self, user_id, item, days=3):
        self.life_items.append(item)
        return 1

    async def upsert_companion_persona_facts(self, user_id, facts, **kwargs):
        self.saved_growth.extend(facts)
        return len(facts)


def fake_modules(client):
    llm = types.ModuleType("llm_client")
    llm.__file__ = "<local-test-stub>"
    llm.robust_llm_client = client
    llm.thinking_disabled_extra_body = lambda model, base_url=None: {}
    config = types.ModuleType("config")
    config.MEMORY_CURATOR_MODEL = "local-test-stub"
    return {"llm_client": llm, "config": config}


class ReadingGrowthInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_title_and_body_preserve_alice_view_separately(self):
        client = FakeClient()
        opinion = "不太喜欢被告诉该怎么想。" + "我还是愿意自己判断。" * 12
        self.assertLessEqual(len(opinion), 160)
        with mock.patch.dict(sys.modules, fake_modules(client)):
            result = await growth.judge_growth_from_reading(
                "一个很长的标题" * 100, ARTICLE * 100, opinion,
                existing=["旧看法"], source_kind="reading"
            )
        self.assertEqual(len(client.calls), 1)
        messages, kwargs = client.calls[0]
        prompt = messages[1]["content"]
        self.assertIn("外部材料: 标题：", prompt)
        self.assertIn("正文摘要：", prompt)
        self.assertIn("Alice: " + opinion, prompt)
        self.assertLess(prompt.index("外部材料:"), prompt.index("Alice:"))
        self.assertIn("旧看法", prompt)
        self.assertEqual(result[0]["source_kind"], "reading")
        self.assertEqual(kwargs["model"], None)
        self.assertEqual(kwargs["max_retries"], 3)

    async def test_short_and_english_material_keep_exact_view(self):
        client = FakeClient()
        view = "I dislike being told what to think; I prefer making my own judgment."
        with mock.patch.dict(sys.modules, fake_modules(client)):
            await growth.judge_growth_from_reading(
                "An ordinary title", "The author's claim is short.", view
            )
        prompt = client.calls[0][0][1]["content"]
        self.assertIn("外部材料: 标题：An ordinary title", prompt)
        self.assertIn("The author's claim is short.", prompt)
        self.assertIn("Alice: " + view, prompt)

    async def test_empty_or_whitespace_view_has_no_model_call_or_growth(self):
        for view in ("", "   ", None):
            with self.subTest(view=view):
                client = FakeClient()
                with mock.patch.dict(sys.modules, fake_modules(client)):
                    changes = await growth.judge_growth_from_reading("标题", ARTICLE * 30, view)
                self.assertEqual(client.calls, [])
                self.assertEqual(changes, [])

    async def test_known_roles_keep_labels_and_300_limit_unknown_is_not_alice(self):
        client = FakeClient()
        user_text, alice_text, other_text = "u" * 301, "a" * 301, "x" * 301
        with mock.patch.dict(sys.modules, fake_modules(client)):
            result = await growth.judge_growth([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": alice_text},
                {"role": "other", "content": other_text},
            ], existing=["先前想法"])
        prompt = client.calls[0][0][1]["content"]
        expected = (
            "用户: " + "u" * 300 + "\n" +
            "Alice: " + "a" * 300 + "\n" +
            "未知来源: " + "x" * 300 + "\n\n---\n"
            "她已经有的说法（**如果这次要报的和这些意思重复，就别报**）：\n"
            "- 先前想法"
        )
        self.assertEqual(prompt, expected)
        self.assertEqual(result[0]["source_kind"], "conversation")
        self.assertEqual(len(client.calls), 1)

    async def test_source_kind_override_and_nonideal_view_are_preserved(self):
        client = FakeClient()
        with mock.patch.dict(sys.modules, fake_modules(client)):
            result = await growth.judge_growth_from_reading(
                "标题", ARTICLE, OPINION, source_kind="external_test"
            )
        self.assertEqual(result[0]["value"], OPINION)
        self.assertEqual(result[0]["source_kind"], "external_test")
        prompt = client.calls[0][0][1]["content"]
        self.assertIn("外部材料:", prompt)
        self.assertIn("Alice: " + OPINION, prompt)

    async def test_real_run_once_passes_opinion_to_judge_without_extra_calls(self):
        client, memory = FakeClient(), FakeMemory()
        candidates = [{"title": "这个标题" * 80, "url": "https://example.invalid/a"}]
        body = ARTICLE * 100
        with mock.patch.dict(sys.modules, fake_modules(client)), \
             mock.patch.object(reading, "pick_section", return_value=("popular", 0, "首页热榜")), \
             mock.patch.object(reading, "fetch_candidates", return_value=candidates), \
             mock.patch.object(reading, "_seed_text", return_value="已有底子"), \
             mock.patch.object(reading, "fetch_article", new_callable=mock.AsyncMock, return_value=body):
            result = await reading.run_once(memory, "isolated-test-user", force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "done")
        self.assertEqual(len(client.calls), 4)
        choose, merge, opinion_call, judgment = client.calls
        self.assertTrue(choose[0][0]["content"].startswith(reading.CHOOSE_SYSTEM))
        self.assertEqual(merge[0][0]["content"], reading.MERGE_SYSTEM)
        self.assertEqual(opinion_call[0][0]["content"], reading.OPINION_SYSTEM)
        self.assertEqual(judgment[0][0]["content"], growth.JUDGE_SYSTEM)
        self.assertIn(body, opinion_call[0][1]["content"])
        judge_prompt = judgment[0][1]["content"]
        self.assertIn("外部材料:", judge_prompt)
        self.assertIn("Alice: " + OPINION, judge_prompt)
        self.assertIn("我以前更容易认同说教。", judge_prompt)
        self.assertEqual(len(memory.saved_growth), 1)
        self.assertEqual(memory.saved_growth[0]["value_text"], OPINION)
        self.assertEqual(memory.saved_growth[0]["evidence_quote"], OPINION)
        self.assertEqual(memory.saved_growth[0]["source_kind"], "reading")


if __name__ == "__main__":
    unittest.main()
