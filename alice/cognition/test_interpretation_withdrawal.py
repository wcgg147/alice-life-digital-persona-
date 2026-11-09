"""Withdrawal must reach downstream knowledge, not only the reading assessment."""
import asyncio
import json
import unittest

from cognition.decision import working_context
from cognition.development import review_topic
from cognition.inquiry import receipt_matches
from cognition.self_model import retrieve_related_knowledge
from cognition.state import Ledger
from cognition.test_development import Model as ReviewModel
from cognition import test_question_state as question_fixtures


class InterpretationWithdrawalTests(unittest.TestCase):
    setUp = question_fixtures.QuestionStateTests.setUp
    tearDown = question_fixtures.QuestionStateTests.tearDown
    read = question_fixtures.QuestionStateTests.read
    initial = question_fixtures.QuestionStateTests.initial
    integrate = question_fixtures.QuestionStateTests.integrate
    state = question_fixtures.QuestionStateTests.state

    def test_withdrawal_removes_integrated_recall_and_reopens_attention_after_restart(self):
        self.integrate()
        original = self.ledger.db.execute('select result_json from episodes where id=?', (self.a,)).fetchone()[0]
        self.ledger.revoke_inquiry(self.a, '原解释未被引用原文充分支持')
        self.ledger.close()
        self.ledger = Ledger(self.path)
        view = self.ledger.view()
        self.assertEqual(self.state()['parts'][0]['status'], 'unresolved')
        recalled = retrieve_related_knowledge(view, ['温度变化触发开关'])
        self.assertFalse(any(b['episode_id'] == self.a for c in recalled for b in c['evidence']))
        self.assertNotIn(self.a, [p['episode_id'] for p in view['progress_memory']['a']])
        self.assertIn(self.b, [p['episode_id'] for p in view['progress_memory']['a']])
        self.assertTrue(all(self.a not in b['supporting_episodes'] for b in view['inquiry_model']))
        ctx = working_context(view, [], [])
        self.assertIn('触发条件是什么？', ctx['attention_focus']['questions'])
        row = self.ledger.db.execute('select result_json,revoked from episodes where id=?', (self.a,)).fetchone()
        result = json.loads(row['result_json'])
        self.assertFalse(row['revoked'])
        self.assertTrue(receipt_matches(result))
        self.assertEqual(result['text'], json.loads(original)['text'])
        self.assertIn('withdrawn_inquiry_assessment', result)

    def test_late_integration_cannot_install_a_withdrawn_interpretation(self):
        out, _ = self.integrate(callback=lambda: self.ledger.revoke_inquiry(self.a, '整合期间撤回解释'))
        self.assertEqual(out['status'], 'integration_error')
        self.assertFalse(self.ledger.view()['question_states'])

    def test_new_integration_cannot_reuse_the_withdrawn_source_via_previous_claims(self):
        self.integrate()
        old = self.initial()
        self.ledger.revoke_inquiry(self.a, '撤回旧解释')
        self.read('a', 2)
        out, _ = self.integrate(old)
        self.assertEqual(out['status'], 'integration_error')
        self.assertFalse(self.state()['parts'][0]['claims'])

    def test_accepted_summary_and_derived_goal_are_withdrawn_together(self):
        self.read('a', 2)
        out = asyncio.run(review_topic(ReviewModel(self.ledger), self.ledger, 'book:a'))
        self.assertEqual(out['status'], 'accepted')
        self.ledger.revoke_inquiry(self.a, '复评依赖的早期解释撤回')
        view = self.ledger.view()
        self.assertNotIn(out['new_goal_id'], {g['id'] for g in view['goals']})
        self.assertEqual(next(g for g in view['goals'] if g['id'] == 'a')['status'], 'active')
        self.assertEqual(self.ledger.export()['development_reviews'][0]['status'], 'withdrawn')
        self.assertTrue(view['consolidations'][0]['summary'].startswith('[自动轨迹汇总]'))

    def test_inflight_review_cannot_restore_summary_after_withdrawal(self):
        self.read('a', 2)
        model = ReviewModel(self.ledger, change=lambda _: self.ledger.revoke_inquiry(self.a, '复评期间撤回'))
        with self.assertRaisesRegex(ValueError, 'inputs_changed'):
            asyncio.run(review_topic(model, self.ledger, 'book:a'))
        self.assertFalse(any(r['status'] == 'accepted' for r in self.ledger.export()['development_reviews']))

    def test_legacy_cached_summary_cannot_survive_without_review_record(self):
        with self.ledger.db:
            self.ledger.db.execute("update consolidations set summary='STALE_INTERPRETATION'")
        self.ledger.revoke_inquiry(self.a, '撤回旧摘要依据')
        self.assertNotIn('STALE_INTERPRETATION', json.dumps(working_context(self.ledger.view(), [], [])))

    def test_fresh_review_of_surviving_sources_can_form_a_new_summary(self):
        self.read('a', 2)
        self.ledger.revoke_inquiry(self.a, '撤回旧解释')
        out = asyncio.run(review_topic(ReviewModel(self.ledger, status='active', new_question=False), self.ledger, 'book:a'))
        self.assertEqual(out['status'], 'accepted')
        self.assertFalse(self.ledger.view()['consolidations'][0]['summary'].startswith('[自动轨迹汇总]'))
        from cognition.consolidation import rebuild_consolidations
        with self.ledger.db:
            rebuild_consolidations(self.ledger)
        self.assertFalse(self.ledger.view()['consolidations'][0]['summary'].startswith('[自动轨迹汇总]'))
        self.assertNotIn('后续已继续读到', self.ledger.view()['consolidations'][0]['summary'])


if __name__ == '__main__':
    unittest.main()
