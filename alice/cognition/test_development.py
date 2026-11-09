import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition.development import review_topic
from cognition.loop import CognitiveLoop
from cognition.state import Ledger, digest


class Model:
    def __init__(self, ledger, *, rest=False, change=None, status='completed', new_question=True):
        self.ledger, self.rest, self.change, self.status, self.new_question = ledger, rest, change, status, new_question
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        payload = json.loads(messages[1]['content'])
        if self.rest and self.calls == 1:
            return {'action': 'rest', 'reason': '想整理刚读过的经历', 'resource_id': '', 'goal': None,
                    'predict_success': False, 'next_check_seconds': 600,
                    'consolidation_id': payload['development_opportunities'][0]['id']}
        source = payload['sources'][-1]
        response = {'summary': '主角穿过石桥，已经回到家中，接下来要面对新的生活。',
                    'goal_id': payload['goal']['id'] if payload['goal'] else None,
                    'goal_status': self.status if payload['goal'] else None,
                    'answer': '主角已经穿过石桥回到家。', 'reason': '当前主题的原文明确写出了回家的进展。',
                    'evidence': [{'episode_id': source['episode_id'], 'span_id': source['spans'][0]['id']}],
                    'next_question': '回到家以后，主角将怎样安排新的生活？' if self.new_question else None,
                    'next_need': 'text'}
        if self.change:
            self.change(response)
        return json.dumps(response, ensure_ascii=False)


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.sqlite3')
        self.ids = self.seed('a', 3)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def seed(self, book, n, start=0, scope='public', actor='', goal_id=None):
        ids = []
        for i in range(start, start+n):
            d = {'action': 'read', 'resource_id': f'book:{book}:{i}',
                 'goal': {'id': goal_id or f'g:{book}', 'question': '主角是否已经回家？', 'need': 'text',
                          'origin': f'book:{book}:0', 'status': 'active'}, '_scope': scope, '_actor': actor}
            ep = self.ledger.begin({}, d)
            r = {'status': 'ok', 'source_ref': f'book:{book}:{i}', 'family': 'book_original_text',
                 'text': '主角穿过石桥，已经回到家中，接下来要面对新的生活。' * 5, 'met_need': True}
            r['receipt'] = digest(r)
            self.ledger.finish(ep, 'observed', r)
            ids.append(ep)
        return ids

    def review(self, **kwargs):
        return asyncio.run(review_topic(Model(self.ledger, **kwargs), self.ledger, 'book:a'))

    def test_only_the_topic_bound_goal_changes_and_source_revocation_undoes_it(self):
        self.seed('b', 1)
        outcome = self.review()
        statuses = {g['id']: g['status'] for g in self.ledger.view()['goals']}
        self.assertEqual(statuses['g:a'], 'completed')
        self.assertEqual(statuses['g:b'], 'active')
        self.assertEqual(statuses[outcome['new_goal_id']], 'active')
        self.ledger.revoke(self.ids[-1])
        view = self.ledger.view()
        statuses = {g['id']: g['status'] for g in view['goals']}
        self.assertEqual(statuses['g:a'], 'active')
        self.assertNotIn(outcome['new_goal_id'], statuses)
        self.assertTrue(next(c for c in view['consolidations'] if c['topic'] == 'book:a')['summary'].startswith('[自动轨迹汇总]'))
        self.assertEqual(self.ledger.export()['development_reviews'][0]['status'], 'withdrawn')

    def test_chosen_rest_runs_review_without_reading_or_an_extra_manual_trigger(self):
        class Env:
            reads = 0

            async def observe(inner):
                return [], []

            async def read(inner, resource):
                inner.reads += 1
                raise AssertionError('rest must not read')

        env = Env();model = Model(self.ledger, rest=True)
        out = asyncio.run(CognitiveLoop(self.ledger, model, env).tick())
        self.assertEqual(out['status'], 'rest')
        self.assertEqual(out['development_review']['status'], 'accepted')
        self.assertEqual(model.calls, 2)
        self.assertEqual(env.reads, 0)
        self.assertFalse(self.ledger.view()['development_opportunities'])

    def test_existing_active_goal_can_remain_open_without_forcing_new_interest(self):
        out = self.review(status='active', new_question=False)
        self.assertIsNone(out['new_goal_id'])
        self.assertEqual(next(g for g in self.ledger.view()['goals'] if g['id']=='g:a')['status'], 'active')
        self.assertFalse(self.ledger.view()['development_opportunities'])
        self.seed('a', 1, start=3)
        self.assertFalse(self.ledger.view()['development_opportunities'])
        self.seed('a', 2, start=4)
        self.assertEqual(self.ledger.view()['development_opportunities'][0]['new_sources'], 3)

    def test_invalid_goal_and_fabricated_evidence_do_not_update_state(self):
        for update in ({'goal_id': 'g:unrelated'}, {'evidence': [{'episode_id': self.ids[0], 'span_id': 's9999'}]},
                       {'goal_status': 'false'}):
            with self.assertRaises(ValueError):
                self.review(change=lambda response: response.update(update))
        self.assertEqual(self.ledger.view()['goals'][0]['status'], 'active')
        self.assertFalse(any(r['status']=='accepted' for r in self.ledger.export()['development_reviews']))

    def test_stale_result_cannot_overwrite_a_concurrent_goal_change(self):
        def change(response):
            with self.ledger.db:
                self.ledger.db.execute("UPDATE goals SET status='paused',updated=999 WHERE id='g:a'")
        with self.assertRaisesRegex(ValueError, 'inputs_changed'):
            self.review(change=change)
        self.assertEqual(self.ledger.view()['goals'][0]['status'], 'paused')

    def integrated_answer(self):
        from cognition.question_state import goal_version
        goal=next(g for g in self.ledger.view()['goals'] if g['id']=='g:a')
        original=json.loads(self.ledger.db.execute('select result_json from episodes where id=?',(self.ids[0],)).fetchone()[0])
        ep=self.ledger.begin({}, {'action':'integrate','integration_goal_id':'g:a','goal':None})
        result={'goal_id':'g:a','goal_version':goal_version(goal),'parts':[
            {'id':'p1','question':'主角是否已经回家？','claim':{'id':'c1','answer':'主角已经回家。','coverage':'answer',
             'evidence':[{'episode_id':self.ids[0],'span_id':'s0000','quote':'主角穿过石桥','receipt':original['receipt']}],
             'challenges':[]}}]}
        result['receipt']=digest(result);self.ledger.finish(ep,'integrated',result)
        return ep

    def test_question_integration_and_withdrawal_change_review_version_without_new_reads(self):
        from cognition.development import snapshot
        before=snapshot(self.ledger,'book:a');ep=self.integrated_answer();after=snapshot(self.ledger,'book:a')
        self.assertEqual(before['source_ids'],after['source_ids'])
        self.assertNotEqual(before['version'],after['version'])
        self.ledger.revoke(ep)
        withdrawn=snapshot(self.ledger,'book:a')
        self.assertEqual(withdrawn['version'],before['version'])

    def test_mid_review_integration_withdrawal_rejects_stale_completion_and_new_goal(self):
        ep=self.integrated_answer()
        with self.assertRaisesRegex(ValueError,'inputs_changed'):
            self.review(change=lambda response:self.ledger.revoke(ep))
        self.assertEqual({g['id']:g['status'] for g in self.ledger.view()['goals']},{'g:a':'active'})
        self.assertFalse(self.ledger.view()['question_states'])
        self.assertFalse(any(r['status']=='accepted' for r in self.ledger.export()['development_reviews']))

    def test_mid_review_revocation_cannot_resurrect_the_source(self):
        with self.assertRaisesRegex(ValueError, 'inputs_changed'):
            self.review(change=lambda response: self.ledger.revoke(self.ids[0]))
        self.assertFalse(any(r['status']=='accepted' for r in self.ledger.export()['development_reviews']))

    def test_withdrawal_retains_later_independent_goal_edits(self):
        out = self.review()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE goals SET status='abandoned',updated=999 WHERE id='g:a'")
        self.ledger.revoke_development(out['review_id'], '模型解释错误')
        self.assertEqual(next(g for g in self.ledger.view()['goals'] if g['id']=='g:a')['status'], 'abandoned')

    def test_private_opportunity_is_not_visible_to_public_or_another_actor(self):
        self.seed('private', 3, scope='private:11', actor='11')
        self.assertNotIn('book:private', [o['topic'] for o in self.ledger.view()['development_opportunities']])
        self.assertNotIn('book:private', [o['topic'] for o in self.ledger.view('private:22','22')['development_opportunities']])
        self.assertIn('book:private', [o['topic'] for o in self.ledger.view('private:11','11')['development_opportunities']])

    def test_export_delete_and_crash_recovery_cover_review_state(self):
        def cancel(response):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.review(change=cancel)
        self.assertEqual(self.ledger.export()['development_reviews'][0]['status'], 'interrupted')
        self.ledger.delete()
        self.assertFalse(self.ledger.export()['development_reviews'])

    def test_budget_runs_out_after_rest_without_partial_goal_changes(self):
        class Env:
            async def observe(inner):
                return [], []
        model = Model(self.ledger, rest=True)
        result = asyncio.run(CognitiveLoop(self.ledger, model, Env(), daily_cap=1).tick())
        self.assertEqual(result['status'], 'rest')
        self.assertEqual(result['development_review']['status'], 'budget_wait')
        self.assertEqual(model.calls, 1)
        self.assertFalse(self.ledger.export()['development_reviews'])
        self.assertEqual(self.ledger.view()['goals'][0]['status'], 'active')

    def test_withdrawn_permission_blocks_a_late_review(self):
        allowed = [True]
        model = Model(self.ledger, change=lambda response: allowed.__setitem__(0, False))
        with self.assertRaises(PermissionError):
            asyncio.run(review_topic(model, self.ledger, 'book:a', guard=lambda: allowed[0]))
        self.assertEqual(self.ledger.view()['goals'][0]['status'], 'active')

    def test_reading_review_cannot_complete_a_social_feedback_trial(self):
        self.seed('policy', 3, goal_id='policy:p')
        with self.assertRaisesRegex(ValueError, 'nonpolicy_goal'):
            asyncio.run(review_topic(Model(self.ledger), self.ledger, 'book:policy'))

    def test_integrated_milestone_sources_and_question_state_are_exposed_to_review(self):
        from cognition.development import snapshot
        from cognition.question_state import goal_version
        more = self.seed('a', 2, start=3)
        all_ids = self.ids + more
        goal = next(g for g in self.ledger.view()['goals'] if g['id'] == 'g:a')
        first_row = self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?', (all_ids[0],)).fetchone()
        first_res = json.loads(first_row[0])
        ie = self.ledger.begin({}, {'action': 'integrate', 'integration_goal_id': 'g:a', 'goal': None, 'resource_id': '',
                                    'predict_success': False, 'next_check_seconds': 600, 'reason': '整合里程碑证据'})
        ir = {'status': 'ok', 'goal_id': 'g:a', 'goal_version': goal_version(goal),
              'parts': [{'id': 'p1', 'question': '主角是否已经回家？',
                         'claim': {'id': 'c1', 'answer': '主角已经穿过石桥回到家中。', 'coverage': 'answer',
                                   'evidence': [{'episode_id': all_ids[0], 'span_id': 's0000',
                                                 'quote': '主角穿过石桥，已经回到家中', 'receipt': first_res['receipt']}],
                                   'challenges': []}}]}
        ir['receipt'] = digest(ir)
        self.ledger.finish(ie, 'integrated', ir)
        s = snapshot(self.ledger, 'book:a')
        self.assertEqual(s['payload']['review_window'], 'milestone_and_latest_observed_sources')
        self.assertIn(all_ids[0], [src['episode_id'] for src in s['payload']['sources']])
        self.assertIn('question_state', s['payload'])
        opp = next(o for o in self.ledger.view()['development_opportunities'] if o['topic'] == 'book:a')
        self.assertTrue(opp.get('all_subquestions_answered'))
        outcome = self.review(change=lambda resp: resp.update(
            evidence=[{'episode_id': all_ids[0], 'span_id': 's0000'}]
        ))
        self.assertEqual(outcome['status'], 'accepted')
        self.assertEqual(next(g for g in self.ledger.view()['goals'] if g['id'] == 'g:a')['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
