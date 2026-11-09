import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cognition.decision import rank_resources, validate, working_context
from cognition.loop import CognitiveLoop
from cognition.state import InstanceLock, Ledger


def resource(n=1, family="cards"):
    return {"id": f"r{n}", "family": family, "source_ref": f"fixture:{family}:{n}", "title": "test"}


def decision(r, need="audio"):
    return {"action": "read", "resource_id": r["id"], "reason": "explore",
            "goal": {"id": "", "question": "test question", "need": need, "origin": r["id"], "status": "active"},
            "predict_success": True, "next_check_seconds": 60}


class Model:
    def __init__(self, response):
        self.response, self.calls = response, 0

    async def complete(self, messages):
        self.calls += 1
        return self.response


class Environment:
    def __init__(self, ledger, r):
        self.ledger, self.r, self.reads = ledger, r, 0

    async def observe(self):
        return [self.r], []

    async def read(self, r):
        self.reads += 1
        # A committed intent must be visible to a second connection BEFORE I/O.
        other = Ledger(self.ledger.path)
        try:
            assert other.view()["episodes"][0]["status"] == "started"
        finally:
            other.close()
        return {"status": "ok", "text": "observed source text " * 10, "source_ref": r["source_ref"],
                "audio_observed": False, "primary_observed": False}


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def run_read(self, n=1, family="cards", need="audio"):
        r = resource(n, family)
        env = Environment(self.ledger, r)
        model = Model(decision(r, need))
        result = asyncio.run(CognitiveLoop(self.ledger, model, env).tick())
        return result, env

    def test_intent_result_restore_and_learning_changes_attention(self):
        first, _ = self.run_read(1)
        self.assertTrue(first["prediction_error"])
        self.assertFalse(self.ledger.view()["strategies"])
        self.run_read(2)
        self.ledger.close()
        self.ledger = Ledger(self.path)
        view = self.ledger.view()
        self.assertEqual(len(view["goals"]), 1)
        self.assertEqual(view["revision"], 2)
        self.assertEqual(view["strategies"][0]["failures"], 2)
        ranked = rank_resources([resource(3), resource(4, "audio_source")], view["strategies"], "audio")
        self.assertEqual(ranked[0]["family"], "audio_source")

    def test_same_source_repetition_does_not_promote(self):
        self.run_read(1)
        self.run_read(1)
        self.assertEqual(self.ledger.view()["strategies"], [])

    def test_revocation_removes_active_rule_and_raw_evidence(self):
        first, _ = self.run_read(1)
        self.run_read(2)
        self.ledger.revoke(first["episode_id"])
        view = self.ledger.view()
        context = working_context(view, [resource(3)], [])
        self.assertEqual(context["strategies"], [])
        revoked = next(e for e in context["episodes"] if e["id"] == first["episode_id"])
        self.assertEqual(revoked, {"id": first["episode_id"], "revoked": True})

    def test_recover_inflight_without_replaying(self):
        self.ledger.begin({}, decision(resource()))
        self.assertEqual(self.ledger.recover(), 1)
        self.assertEqual(self.ledger.recover(), 0)
        self.assertEqual(self.ledger.view()["episodes"][0]["status"], "unknown")
        self.assertFalse(self.ledger.view()["strategies"])

    def test_invalid_json_does_not_read(self):
        env = Environment(self.ledger, resource())
        result = asyncio.run(CognitiveLoop(self.ledger, Model("bad json"), env).tick())
        self.assertEqual(result["status"], "decision_error")
        self.assertEqual(env.reads, 0)

    def test_rest_is_explicit_and_keeps_unfinished_goal(self):
        self.run_read()
        env = Environment(self.ledger, resource(2))
        rest = {"action": "rest", "resource_id": "", "reason": "choose to pause", "goal": None,
                "predict_success": False, "next_check_seconds": 600}
        result = asyncio.run(CognitiveLoop(self.ledger, Model(rest), env).tick())
        self.assertEqual(result["status"], "rest")
        self.assertEqual(env.reads, 0)
        self.assertEqual(self.ledger.view()["goals"][0]["status"], "active")

    def test_budget_survives_reopen_and_multiple_connections(self):
        self.ledger.reserve_call(1)
        other = Ledger(self.path)
        try:
            with self.assertRaisesRegex(RuntimeError, "budget"):
                other.reserve_call(1)
        finally:
            other.close()

    def test_explicit_unlimited_mode_keeps_counting_beyond_prior_cap_and_reopen(self):
        for _ in range(25):self.ledger.reserve_call(0)
        other=Ledger(self.path)
        try:
            other.check_budget(0);other.reserve_call(0)
            self.assertEqual(other.db.execute('select calls from call_budget').fetchone()[0],26)
            with self.assertRaisesRegex(RuntimeError,'budget'):other.reserve_call(20)
        finally:other.close()

    def test_native_entry_accepts_zero_cap_without_starting_external_io(self):
        import subprocess,sys
        cmd=[sys.executable,'-X','utf8',str(Path(__file__).resolve().parents[1]/'surf/live.py'),'--daily-cap','0',
             '--state',str(Path(self.tmp.name)/'unlimited.sqlite3'),'--export',str(Path(self.tmp.name)/'export.json')]
        result=subprocess.run(cmd,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_observation_failure_does_not_consume_model_attempt(self):
        class BrokenObservation(Environment):
            async def observe(inner):
                raise OSError("offline")
        with self.assertRaises(OSError):
            asyncio.run(CognitiveLoop(self.ledger, Model("unused"), BrokenObservation(self.ledger, resource())).tick())
        self.assertFalse(self.ledger.export()["call_budget"])

    def test_stop_prevents_observation_and_model(self):
        stop = Path(self.tmp.name) / "STOP"
        stop.touch()
        env, model = Environment(self.ledger, resource()), Model("unused")
        result = asyncio.run(CognitiveLoop(self.ledger, model, env, stop_path=stop).tick())
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(model.calls, 0)
        self.assertEqual(self.ledger.export()["call_budget"], [])

    def test_stop_arriving_during_model_prevents_action(self):
        stop = Path(self.tmp.name) / "STOP"
        class StopModel:
            async def complete(inner, messages):
                stop.touch()
                return decision(resource())
        env = Environment(self.ledger, resource())
        result = asyncio.run(CognitiveLoop(self.ledger, StopModel(), env, stop_path=stop).tick())
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(env.reads, 0)

    def test_unavailable_resource_unsourced_goal_and_id_rewrite_rejected(self):
        context = working_context(self.ledger.view(), [resource()], [])
        invalid = decision(resource(99))
        with self.assertRaisesRegex(ValueError, "resource"):
            validate(invalid, context)
        invalid = decision(resource())
        invalid["goal"]["origin"] = "fabricated episode"
        with self.assertRaisesRegex(ValueError, "unsourced"):
            validate(invalid, context)
        invalid["goal"]["id"] = "invented"
        with self.assertRaisesRegex(ValueError, "unknown_goal"):
            validate(invalid, context)

    def test_transport_failure_does_not_teach(self):
        class BrokenEnvironment(Environment):
            async def read(inner, r):
                raise OSError("network")
        r = resource()
        for _ in range(2):
            result = asyncio.run(CognitiveLoop(self.ledger, Model(decision(r)), BrokenEnvironment(self.ledger, r)).tick())
            self.assertEqual(result["status"], "tool_error")
        self.assertFalse(self.ledger.export()["strategies"])

    def test_cancelled_action_retains_unknown_and_does_not_learn(self):
        class InterruptedEnvironment(Environment):
            async def read(inner, r):
                raise asyncio.CancelledError()
        r = resource()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(CognitiveLoop(self.ledger, Model(decision(r)), InterruptedEnvironment(self.ledger, r)).tick())
        self.assertEqual(self.ledger.view()["episodes"][0]["status"], "unknown")
        self.assertFalse(self.ledger.view()["strategies"])

    def test_self_explanations_do_not_become_learning_evidence(self):
        self.run_read(1)
        context = working_context(self.ledger.view(), [resource(2)], [])
        self.assertNotIn("reason", context["episodes"][0]["decision"])
        self.assertNotIn("goal", context["episodes"][0]["decision"])

    def test_export_and_delete_are_scoped(self):
        self.run_read()
        other = Ledger(self.path, "other")
        try:
            other.reserve_call(2)
            self.assertTrue(self.ledger.export()["episodes"])
            self.ledger.delete()
            self.assertTrue(all(not rows for rows in self.ledger.export().values()))
            self.assertTrue(other.export()["call_budget"])
        finally:
            other.close()

    def test_instance_lock(self):
        path = Path(self.tmp.name) / "runtime.lock"
        with InstanceLock(path):
            with self.assertRaisesRegex(RuntimeError, "already_running"):
                with InstanceLock(path):
                    pass
        with InstanceLock(path):
            pass

    def test_strategy_disabled_control_retains_same_raw_episodes(self):
        self.run_read(1)
        self.run_read(2)
        a = working_context(self.ledger.view(), [resource(3)], [], True)
        b = working_context(self.ledger.view(), [resource(3)], [], False)
        self.assertEqual(a["episodes"], b["episodes"])
        self.assertTrue(a["strategies"])
        self.assertFalse(b["strategies"])

    def test_longcat_request_uses_existing_thinking_adapter(self):
        from cognition.environment import LiveModel
        with patch("openai.AsyncOpenAI") as client:
            model = LiveModel({"model": "LongCat-2.5", "model_base_url": "https://example.invalid/v1",
                               "model_api_key": "test-only"})
            client.return_value.chat.completions.create = AsyncMock()
            asyncio.run(model.complete([{"role": "user", "content": "test"}]))
            self.assertEqual(client.call_args.kwargs["max_retries"], 0)
            self.assertEqual(client.return_value.chat.completions.create.call_args.kwargs["extra_body"],
                             {"thinking": {"type": "disabled"}})
            self.assertEqual(client.return_value.chat.completions.create.call_args.kwargs["response_format"],
                             {"type": "json_object"})

    def test_self_model_motivation_and_selective_attention(self):
        # 2 failed audio reads on 'cards', then 2 successful text reads on 'book_original_text'
        self.run_read(1, family="cards", need="audio")
        self.run_read(2, family="cards", need="audio")
        for idx in (3, 4):
            r = {"id": f"book:huozhe:{idx}", "family": "book_original_text",
                 "source_ref": f"fixture:book:{idx}", "title": "余华《活着》福贵回乡"}
            env = Environment(self.ledger, r)
            dec = {"action": "read", "resource_id": r["id"], "reason": "explore book",
                   "goal": {"id": "", "question": "福贵回乡后的命运", "need": "text", "origin": r["id"], "status": "active"},
                   "predict_success": True, "next_check_seconds": 60}
            asyncio.run(CognitiveLoop(self.ledger, Model(dec), env).tick())

        view = self.ledger.view()
        candidates = [
            {"id": "book:huozhe:4", "family": "book_original_text", "title": "已读章节"},
            {"id": "book:huozhe:5", "family": "book_original_text", "title": "余华《活着》福贵回乡后续"},
        ]
        ctx = working_context(view, candidates, [])
        sm = ctx["self_model"]
        self.assertEqual(sm["capabilities"][0]["family"], "book_original_text")
        self.assertEqual(sm["limitations"][0]["family"], "cards")
        self.assertEqual(sm["prediction_calibration"]["evaluated_count"], 4)
        self.assertEqual(sm["prediction_calibration"]["error_count"], 2)
        self.assertGreater(sm['prediction_calibration']['recent_accuracy_change'],0.0)
        self.assertIsNone(sm["intrinsic_motivation"]["learning_progress"])
        self.assertFalse(sm['emerged_interests'])
        self.assertEqual(sm["activity_topics"][0]["topic"], "book:huozhe")
        # Unseen relevant chapter ranks above already-read chapter of same family
        self.assertEqual(ctx["resources"][0]["id"], "book:huozhe:5")
        self.assertGreater(ctx["resources"][0]["attention_score"], ctx["resources"][1]["attention_score"])

    def test_memory_consolidation_reconciles_progress_and_revokes_cleanly(self):
        from cognition.consolidation import consolidate_topic_with_model
        ep_ids = []
        for idx in (22, 23, 24):
            r = {"id": f"book:huozhe:{idx}", "family": "book_original_text",
                 "source_ref": f"fixture:book:{idx}", "title": f"《活着》第{idx}块"}
            env = Environment(self.ledger, r)
            dec = {"action": "read", "resource_id": r["id"], "reason": "继续读活着",
                   "goal": {"id": "", "question": "福贵被抓壮丁后怎么回乡？", "need": "text", "origin": r["id"], "status": "active"},
                   "predict_success": True, "next_check_seconds": 60}
            out = asyncio.run(CognitiveLoop(self.ledger, Model(dec), env).tick())
            ep_ids.append(out["episode_id"])

        view = self.ledger.view()
        self.assertEqual(len(view["consolidations"]), 1)
        self.assertEqual(view["consolidations"][0]["start_ref"], "book:huozhe:22")
        self.assertEqual(view["consolidations"][0]["latest_ref"], "book:huozhe:24")
        self.assertEqual(view["consolidations"][0]["episode_count"], 3)

        stale_facts = [{"facet_key": "book:huozhe:progress", "value_text": "读到第 22/70 块"}]
        ctx = working_context(view, [], stale_facts)
        self.assertEqual(ctx["persona_facts"][0]["cognitive_verified_latest_ref"], "book:huozhe:24")

        # Model-driven reflective consolidation evolves goal when answered
        c_model = Model(json.dumps({
            "summary": "福贵从战场死里逃生回到家，发现母亲已去世、凤霞哑了，一家人还得继续熬。",
            "goal_id": self.ledger.view()['goals'][0]['id'], "goal_status": "completed",
            "answer": "福贵从战场活着回到了家。", "reason": "有本轮来源支持，模型判断仍可撤销。",
            "evidence": [{"episode_id": ep_ids[-1], "span_id": "s0000"}],
            "next_question": "福贵回家后怎么养活一家人，有庆和凤霞后来遭遇了什么？", "next_need": "text",
        }, ensure_ascii=False))
        updated = asyncio.run(consolidate_topic_with_model(c_model, self.ledger, "book:huozhe"))
        self.assertIn("死里逃生回到家", updated["summary"])
        active_goals = [g for g in self.ledger.view()["goals"] if g["status"] == "active"]
        self.assertEqual(active_goals[0]["question"], "福贵回家后怎么养活一家人，有庆和凤霞后来遭遇了什么？")

        # Revoking episode 24 updates latest_ref back to 23
        self.ledger.revoke(ep_ids[-1])
        self.assertEqual(self.ledger.view()["consolidations"][0]["latest_ref"], "book:huozhe:23")

    def test_dpo_preference_pair_extraction(self):
        from cognition.dpo_dataset import extract_dpo_pairs
        probe_file = Path(self.tmp.name) / "probe.json"
        probe_file.write_text(json.dumps({
            "candidate_id": "c1",
            "proposal": {"problem": "短句误召回旧话题"},
            "reply_comparisons": {
                "trigger_736": {
                    "user_text": "不感兴趣",
                    "baseline_reply": "也是，时尚这玩意儿离日常太远。",
                    "candidate_reply": "行，那就不聊了。",
                }
            },
        }, ensure_ascii=False), encoding="utf-8")
        pairs = extract_dpo_pairs(probe_results_paths=[probe_file])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["chosen"]["value"], "行，那就不聊了。")
        self.assertEqual(pairs[0]["rejected"]["value"], "也是，时尚这玩意儿离日常太远。")


if __name__ == "__main__":
    unittest.main()
