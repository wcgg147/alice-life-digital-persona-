import asyncio
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from cognition.bridge import serve
from cognition.engineering import EngineeringLoop
from cognition.loop import CognitiveLoop
from cognition.social import SocialState
from cognition.state import Ledger

CONFIG = {"enabled": True, "allowed_groups": ["100"], "allowed_private_users": ["11", "22"], "account_owner_qq": "11"}


def event(mid="1", text="你别再每次反问了", scope="private:11", actor="11", role="person", **extra):
    return {"message_id": mid, "scope": scope, "actor": actor, "role": role, "text": text, "at": time.time(), **extra}


def assessment(**extra):
    return {"is_self_feedback": True, "evidence_quote": "别再每次反问了",
            "policy": {"situation": "这个人随口分享近况时", "guidance": "先回应内容，不惯性反问"},
            "engineering": None, "validation": None, **extra}


class SocialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.social = SocialState(self.ledger)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def trial(self):
        key, _ = self.social.ingest(event(), CONFIG)
        result = self.social.apply_assessment(key, assessment())
        return key, result["policy_id"]

    def test_native_identity_dedupe_and_owner_private_feedback(self):
        key, inserted = self.social.ingest(event(), CONFIG)
        self.assertTrue(inserted)
        again, inserted = self.social.ingest(event(), CONFIG)
        self.assertFalse(inserted)
        self.assertEqual(key, again)
        self.assertEqual(self.social.pending(CONFIG)["id"], key)

    def test_trial_scope_and_actor_do_not_leak(self):
        self.trial()
        own = self.social.reply_packet("private:11", "11")
        self.assertEqual(len(own["policy_trials"]), 1)
        self.assertFalse(self.social.reply_packet("100", "11")["policy_trials"])
        self.assertFalse(self.social.reply_packet("private:22", "22")["policy_trials"])
        self.assertNotIn("反问", json.dumps(self.social.reply_packet("100", "11"), ensure_ascii=False))

    def test_group_preference_does_not_apply_to_other_speaker(self):
        key, _ = self.social.ingest(event(scope="100"), CONFIG)
        self.social.apply_assessment(key, assessment())
        self.assertTrue(self.social.reply_packet("100", "11")["policy_trials"])
        self.assertFalse(self.social.reply_packet("100", "22")["policy_trials"])

    def test_scope_admission_and_delivery_receipts(self):
        for e in [event(scope="999"), event(scope="private:22", actor="11"), event(role="alice")]:
            with self.assertRaises((PermissionError, ValueError)):
                self.social.ingest(e, CONFIG)

    def test_unsupported_quote_and_partial_assessment_are_atomic(self):
        key, _ = self.social.ingest(event(), CONFIG)
        with self.assertRaises(ValueError):
            self.social.apply_assessment(key, assessment(evidence_quote="这句不是原文"))
        with self.assertRaises(ValueError):
            self.social.apply_assessment(key, assessment(engineering={"problem": "缺字段"}))
        self.assertFalse(self.ledger.export()["feedback_policies"])
        self.assertEqual(self.social.pending(CONFIG)["id"], key)

    def test_ordinary_input_can_be_declined_without_growth(self):
        key, _ = self.social.ingest(event(text="今天散步去了"), CONFIG)
        self.social.apply_assessment(key, {"is_self_feedback": False, "evidence_quote": "散步去了", "policy": None, "engineering": None, "validation": None})
        self.assertIsNone(self.social.pending(CONFIG))
        self.assertFalse(self.ledger.export()["feedback_policies"])

    def test_validation_requires_actual_related_delivery_and_can_be_withdrawn(self):
        source, pid = self.trial()
        good, _ = self.social.ingest(event("3", "这样回复好多了"), CONFIG)
        validate = assessment(evidence_quote="这样回复好多了", policy=None,
                              validation={"policy_id": pid, "reply_event_id": "invented", "verdict": "supports"})
        with self.assertRaises(ValueError):
            self.social.apply_assessment(good, validate)
        # This is a simulated acknowledged delivery, explicitly a unit fixture.
        reply, _ = self.social.ingest(event("2", "听起来很放松。", role="alice", receipts=["fixture-receipt"], basis={"policy_ids": [pid]}), CONFIG)
        # Move feedback after delivery; the earlier event cannot validate a future reply.
        with self.ledger.db:
            self.ledger.db.execute("UPDATE social_events SET at=? WHERE id=?", (time.time()+.01, good))
        validate["validation"]["reply_event_id"] = reply
        self.social.apply_assessment(good, validate)
        self.assertEqual(self.social.context("private:11", "11")["policy_trials"][0]["status"], "supported")
        self.assertEqual(self.ledger.view('private:11','11')['goals'][0]['status'],'completed')
        self.social.revoke(good)
        self.assertEqual(self.social.context("private:11", "11")["policy_trials"][0]["status"], "trial")
        self.assertEqual(self.ledger.view('private:11','11')['goals'][0]['status'],'active')
        self.social.revoke(source)
        self.assertFalse(self.social.context("private:11", "11")["policy_trials"])

    def test_new_event_during_action_not_lost_by_future_wake(self):
        ep = self.ledger.begin({"social_watermark": time.time()-.1}, {"action": "rest"})
        self.social.ingest(event(), CONFIG)
        self.ledger.finish(ep, "rest", {}, 3600)
        self.assertLess(self.ledger.view()["next_at"]-time.time(), 1)

    def test_deferred_feedback_does_not_block_next_event(self):
        first,_=self.social.ingest(event(),CONFIG)
        self.social.defer(first,600)
        second,_=self.social.ingest(event('2','你回复的东西太长了'),CONFIG)
        self.assertEqual(self.social.pending(CONFIG)['id'],second)

    def test_v1_goal_and_episode_survive_additive_migration(self):
        import sqlite3
        old=Path(self.tmp.name)/'old.sqlite3'
        db=sqlite3.connect(old)
        db.executescript("CREATE TABLE goals(owner TEXT,id TEXT,question TEXT,need TEXT,origin TEXT,status TEXT,updated REAL,PRIMARY KEY(owner,id)); CREATE TABLE episodes(owner TEXT,id TEXT,started REAL,finished REAL,status TEXT,before_json TEXT,decision_json TEXT,result_json TEXT,revoked INTEGER DEFAULT 0,PRIMARY KEY(owner,id)); PRAGMA user_version=1;")
        db.execute("INSERT INTO goals VALUES ('alice','old-goal','old-question','text','book:1','active',1)")
        db.commit();db.close()
        upgraded=Ledger(old)
        try:
            self.assertEqual(upgraded.view()['goals'][0]['question'],'old-question')
            self.assertEqual(upgraded.db.execute('PRAGMA user_version').fetchone()[0],2)
        finally:upgraded.close()

    def test_private_goal_and_episode_are_not_public_and_revoked_origin_disappears(self):
        key, _ = self.social.ingest(event(), CONFIG)
        d = {"action": "rest", "_scope": "private:11", "_actor": "11",
             "goal": {"id": "private-goal", "question": "PRIVATE_SENTINEL", "need": "text", "origin": key, "status": "active"}}
        ep = self.ledger.begin({}, d)
        self.ledger.finish(ep, "rest", {"text": "PRIVATE_SENTINEL"})
        self.assertIn("PRIVATE_SENTINEL", json.dumps(self.social.reply_packet("private:11", "11")))
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(self.social.reply_packet("100", "11")))
        self.social.revoke(key)
        self.assertFalse(self.ledger.view("private:11", "11")["goals"])

    def test_export_delete_include_events_policies_and_engineering_candidates(self):
        key, _ = self.social.ingest(event(), CONFIG)
        self.social.apply_assessment(key, assessment(engineering={"problem": "重复问", "hypothesis": "上下文遗漏", "verification": "用新话题回放"}))
        data = self.ledger.export()
        self.assertTrue(data["engineering_candidates"])
        self.ledger.delete()
        self.assertTrue(all(not rows for rows in self.ledger.export().values()))

    def test_reflection_is_separate_model_step_with_durable_intent(self):
        key, _ = self.social.ingest(event(), CONFIG)
        class Env:
            cfg = CONFIG
            async def observe(inner): return [], []
        class Model:
            calls = 0
            async def complete(inner, messages):
                inner.calls += 1
                if inner.calls == 1:
                    return json.dumps({"action": "reflect", "event_id": key, "resource_id": "", "reason": "想核对这条反馈", "goal": None, "predict_success": True, "next_check_seconds": 600})
                self.assertEqual(self.ledger.view("private:11", "11")["episodes"][0]["status"], "started")
                return json.dumps(assessment())
        model = Model()
        result = asyncio.run(CognitiveLoop(self.ledger, model, Env()).tick())
        self.assertEqual(result["status"], "reflected")
        self.assertEqual(model.calls, 2)
        self.assertTrue(self.social.reply_packet("private:11", "11")["policy_trials"])

    def test_authenticated_http_and_private_isolation(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
        settings = Path(self.tmp.name) / "bridge.json"
        settings.write_text(json.dumps({"url": f"http://127.0.0.1:{port}", "token": "x"*40}), encoding="utf-8")
        def call(path, body, token="x"*40):
            req = urllib.request.Request(f"http://127.0.0.1:{port}"+path, data=json.dumps(body).encode(), headers={"Authorization": "Bearer "+token})
            with urllib.request.urlopen(req, timeout=2) as r:return json.load(r)
        with serve(self.path, settings, lambda: CONFIG):
            with self.assertRaises(urllib.error.HTTPError) as bad:
                call("/v1/context", {"scope": "100", "actor": "11"}, "bad")
            self.assertEqual(bad.exception.code, 401)
            self.assertTrue(call("/v1/event", event())["inserted"])
            self.trial()
            self.assertFalse(call("/v1/context", {"scope": "100", "actor": "11"})["policy_trials"])
            self.assertTrue(call("/v1/context", {"scope": "private:11", "actor": "11"})["policy_trials"])
            with self.assertRaises(urllib.error.HTTPError) as forbidden:
                call("/v1/context", {"scope": "999", "actor": "11"})
            self.assertEqual(forbidden.exception.code, 403)

    def test_engineering_candidate_lifecycle_requires_reproduction_and_holdouts(self):
        key, _ = self.social.ingest(event(text="你话题跳跃太大了吧"), CONFIG)
        res = self.social.apply_assessment(key, assessment(
            evidence_quote="话题跳跃太大了吧",
            engineering={"problem": "短句不感兴趣触发旧时尚话题召回", "hypothesis": "2-gram匹配召回旧句", "verification": "对比短反应召回与新情境回复"}
        ))
        cid = res["engineering_id"]
        eng = EngineeringLoop(self.ledger)
        self.assertEqual(eng.get(cid)["status"], "proposed")
        with self.assertRaises(ValueError):
            eng.mark_applied(cid, target_files=["alice/qq/channel.py"], rollback_token="r1", test_receipt="t1")
        unrep = eng.evaluate(cid, lambda c: {
            "reproduced": False,
            "baseline_cases": [{"id": "c1", "passed": True}],
            "candidate_cases": [{"id": "c1", "passed": True}],
            "holdout_cases": [{"id": "h1", "passed": True}],
            "suite_passed": True,
        })
        self.assertEqual(unrep["status"], "unreproduced")
        partial = eng.evaluate(cid, lambda c: {
            "reproduced": True,
            "baseline_cases": [{"id": "c1", "passed": False}],
            "candidate_cases": [{"id": "c1", "passed": True}],
            "holdout_cases": [{"id": "h1", "passed": False}],
            "suite_passed": True,
        })
        self.assertEqual(partial["status"], "reproduced")
        verified = eng.evaluate(cid, lambda c: {
            "reproduced": True,
            "baseline_cases": [{"id": "c1", "passed": False}],
            "candidate_cases": [{"id": "c1", "passed": True}],
            "holdout_cases": [{"id": "h1", "passed": True}],
            "suite_passed": True,
        })
        self.assertEqual(verified["status"], "verified")
        applied = eng.mark_applied(cid, target_files=["alice/qq/channel.py"], rollback_token="r1", test_receipt="t1")
        self.assertEqual(applied["status"], "applied")
        rolled = eng.rollback(cid, "regression check")
        self.assertEqual(rolled["status"], "rolled_back")
        self.social.revoke(key)
        self.assertEqual(eng.get(cid)["status"], "withdrawn")

    def test_reply_packet_exposes_subquestions_and_filters_blank_internal_episodes(self):
        from cognition.question_state import goal_version
        from cognition.self_model import synthesize_self_model
        from cognition.state import digest
        goal = {"id": "g:pub", "question": "有庆与凤霞结局如何？", "need": "text", "origin": "book:h:1", "status": "active"}
        ep1 = self.ledger.begin({}, {"action": "read", "resource_id": "book:h:1", "goal": goal})
        r1 = {"status": "ok", "source_ref": "book:h:1", "family": "book_original_text",
              "text": "有庆在献血时死去，凤霞嫁给了二喜。" * 4, "met_need": True}
        r1["receipt"] = digest(r1)
        self.ledger.finish(ep1, "observed", r1)
        g_row = next(g for g in self.ledger.view()["goals"] if g["id"] == "g:pub")
        ie = self.ledger.begin({}, {"action": "integrate", "integration_goal_id": "g:pub", "goal": None, "resource_id": ""})
        ir = {"status": "ok", "goal_id": "g:pub", "goal_version": goal_version(g_row),
              "parts": [{"id": "p1", "question": "有庆结局如何？",
                         "claim": {"id": "c1", "answer": "有庆献血死去", "coverage": "answer",
                                   "evidence": [{"episode_id": ep1, "span_id": "s0000",
                                                 "quote": "有庆在献血时死去", "receipt": r1["receipt"]}],
                                   "challenges": []}},
                        {"id": "p2", "question": "凤霞结局如何？", "claim": None}]}
        ir["receipt"] = digest(ir)
        self.ledger.finish(ie, "integrated", ir)
        packet = self.social.reply_packet("public", "")
        self.assertEqual(len(packet["recent_experiences"]), 1)
        self.assertEqual(packet["recent_experiences"][0]["id"], ep1)
        uq = next(q for q in packet["self_model"]["unresolved_questions"] if q["id"] == "g:pub")
        self.assertEqual(uq["answered_subquestions"], ["有庆结局如何？"])
        self.assertEqual(uq["open_subquestions"], ["凤霞结局如何？"])
        sm = synthesize_self_model(self.ledger.view())
        self.assertEqual(sm["behavioral_tendencies"]["recent_actions"]["integrate"], 1)


if __name__ == "__main__":
    unittest.main()
