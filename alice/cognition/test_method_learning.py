import asyncio
from pathlib import Path
import tempfile
import unittest

from cognition import test_motivation as fixtures
from cognition.decision import working_context
from cognition.loop import CognitiveLoop
from cognition.state import Ledger


class MethodLearningTests(unittest.TestCase):
    read=fixtures.MotivationTests.read
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path)
        for n in range(3):self.read('a',n,useful=False)
    def tearDown(self):self.ledger.close();self.tmp.cleanup()
    def latest(self):return self.ledger.view()['workspace']['method_trials'][-1]

    def search(self,url='https://example.org/mechanism',gid='a',fail=False):
        class Env:
            calls=0
            def exploration_capabilities(inner):return {'can_search':True,'provider':'library','providers':['library']}
            async def observe(inner):return [],[]
            async def search(inner,query,goal,**kwargs):
                inner.calls+=1
                if fail:raise OSError('controlled adapter outage')
                return {'status':'ok','provider':'library','query':query,'discoveries':[{'url':url,'title':'直接机制记录'}]}
        class Model:
            async def complete(inner,messages):return {'action':'search','goal':{'id':gid,'status':'active'},'resource_id':'',
                'reason':'顺序阅读没有推进，换成查找机制来源','search_query':'问题的触发与执行机制','search_provider':'library',
                'predict_success':True,'next_check_seconds':60}
        env=Env();out=asyncio.run(CognitiveLoop(self.ledger,Model(),env).tick(opportunity_source='experiment'))
        self.assertEqual(env.calls,1);self.assertEqual(out['status'],'tool_error' if fail else 'observed')
        return out

    def consume(self,url='https://example.org/mechanism',gid='a',cap=20):
        class Env:
            async def observe(inner):return [{'id':'new_source','family':'public_web_page','source_ref':url,'goal_id':gid}],[]
            async def read(inner,resource):return {'status':'ok','source_ref':url,'text':'温度降到阈值时触点闭合，信号启动电机，电机拉动绳索关闭窗户。'*5}
        class Model:
            requires_inquiry=True
            calls=0
            async def complete(inner,messages):
                inner.calls+=1
                if inner.calls==1:return {'action':'read','goal':{'id':gid,'status':'active'},'resource_id':'new_source',
                    'reason':'读取刚找到的机制记录','predict_success':True,'next_check_seconds':60,
                    'inquiry':{'kind':'mechanism','criterion':'取得具体触发与执行链的直接证据','expected_progress':.7}}
                return {'schema_version':2,'progress_type':'partial_answer','criterion_status':'met','novelty':'new',
                    'missing':'尚缺阈值的具体数值','prior_episode_ids':[],'verdict':'useful','evidence_id':'s0000',
                    'answer':'阈值触发接点，电机拉索关窗','reason':'提供了触发到执行的因果链'}
        return asyncio.run(CognitiveLoop(self.ledger,Model(),Env(),daily_cap=cap).tick(opportunity_source='experiment'))

    def test_implicit_actual_approach_does_not_invent_an_intention(self):
        self.assertEqual(self.latest()['status'],'no_new_progress')
        self.assertEqual(self.latest()['known_feedback'],3)
        self.assertEqual(self.latest()['plan_kind'],'executed_approach')
        self.assertFalse(self.ledger.view()['workspace']['intentions'])

    def test_search_is_not_question_progress_but_reading_closes_the_chain(self):
        found=self.search()
        self.assertEqual(self.latest()['status'],'awaiting_read')
        self.assertEqual(self.latest()['new_progress'],0)
        out=self.consume();trial=self.latest()
        self.assertEqual(trial['method'],'search');self.assertEqual(trial['status'],'provisional_progress')
        self.assertEqual(trial['attempt_episode_ids'],[found['episode_id'],out['episode_id']])
        self.assertEqual(trial['supporting_episodes'],[out['episode_id']])
        self.assertTrue(self.ledger.view()['workspace']['next_steps'][-1]['provisional_reuse_supported'])
        self.assertEqual(trial['read_outcomes'][0]['source_ref'],'https://example.org/mechanism')
        self.assertEqual(self.ledger.view()['workspace']['next_steps'][-1]['suggested_action'],'search')
        self.assertEqual(self.ledger.view()['workspace']['next_steps'][-1]['remaining_gap'],'尚缺阈值的具体数值')

    def test_pending_method_chain_guides_attention_to_its_candidate_without_blocking_others(self):
        self.search()
        resources=[{'id':'other','family':'cards','title':'a的问题怎么解释？','source_ref':'fixture:other'},
                   {'id':'new','family':'public_web_page','title':'机制资料','source_ref':'https://example.org/mechanism','goal_id':'a'}]
        ctx=working_context(self.ledger.view(),resources,[])
        self.assertEqual(ctx['resources'][0]['id'],'new')
        self.assertTrue(ctx['resources'][0]['chosen_method_candidate'])
        self.assertEqual(len(ctx['resources']),2)
        self.assertNotIn('chosen_method_candidate',working_context(self.ledger.view(),resources,[],False)['resources'][0])

    def test_repeat_cannot_inflate_progress_and_reopen_keeps_the_chain(self):
        self.search();self.consume();self.consume()
        trial=self.latest();self.assertEqual(trial['new_progress'],1);self.assertEqual(trial['known_feedback'],2)
        self.assertEqual(len(trial['counterexamples']),1)
        self.assertEqual(trial['status'],'no_new_progress')
        self.assertTrue(self.ledger.view()['workspace']['next_steps'][-1]['reconsider_supported'])
        self.assertFalse(self.ledger.view()['workspace']['next_steps'][-1]['provisional_reuse_supported'])
        self.ledger.close();self.ledger=Ledger(self.path)
        self.assertEqual(self.latest()['new_progress'],1)

    def test_unassessed_new_read_does_not_borrow_old_success_or_erase_known_gap(self):
        self.search();first=self.consume();out=self.consume(cap=4)
        self.assertEqual(out['inquiry_assessment']['reason'],'daily_model_budget_exhausted')
        trial=self.latest();step=self.ledger.view()['workspace']['next_steps'][-1]
        self.assertEqual(trial['new_progress'],1)
        self.assertEqual(trial['status'],'feedback_missing')
        self.assertFalse(trial['current_feedback']['known_question_feedback'])
        self.assertEqual(step['remaining_gap'],'尚缺阈值的具体数值')
        self.assertEqual(step['remaining_gap_evidence'],first['episode_id'])
        self.assertFalse(step['remaining_gap_is_latest_observation'])
        self.assertFalse(step['provisional_reuse_supported'])

    def test_last_known_gap_survives_six_outcome_projection_when_new_feedback_is_unknown(self):
        self.search();first=self.consume()
        for _ in range(7):
            out=self.consume();self.ledger.revoke_inquiry(out['episode_id'],'暂不采纳解释')
        self.assertEqual(len(self.latest()['read_outcomes']),6)
        self.assertEqual(self.latest()['last_known_gap']['episode_id'],first['episode_id'])
        self.assertEqual(self.latest()['status'],'feedback_missing')

    def test_late_withdrawal_cannot_restore_current_success_from_older_support(self):
        self.search();first=self.consume();repeat=self.consume()
        self.assertEqual(self.latest()['status'],'no_new_progress')
        self.ledger.revoke_inquiry(repeat['episode_id'],'评估不充分')
        self.assertEqual(self.latest()['status'],'feedback_missing')
        self.assertEqual(self.latest()['supporting_episodes'],[first['episode_id']])
        self.ledger.revoke(repeat['episode_id'])
        self.assertEqual(self.latest()['status'],'provisional_progress')

    def test_new_search_after_progress_waits_for_reading_instead_of_claiming_success(self):
        from cognition.decision import validate
        from cognition.state import digest
        context=working_context(self.ledger.view(),[],[])
        context['exploration']={'can_search':True,'providers':['library']}
        source=context['workspace']['sources'][0]
        raw={'action':'orient','resource_id':'','goal':None,'predict_success':False,'reason':'改用定向查找','next_check_seconds':60,
             'orientation':{'focus_goal_id':'a','intentions':[{'goal_id':'a','stance':'pursue','why':'原有连续阅读没有推进',
             'method':'search','evidence_ids':[source['id']],'revisit_when':'new_evidence'}]}}
        d=validate(raw,context);ep=self.ledger.begin(context,d)
        r={'orientation':d['orientation'],'source_ref':'orientation:'+ep};r['receipt']=digest(r);self.ledger.finish(ep,'oriented',r)
        self.search();self.consume();self.search()
        self.assertEqual(self.latest()['new_progress'],1)
        self.assertEqual(self.latest()['status'],'awaiting_read')
        self.assertEqual(self.ledger.view()['workspace']['next_steps'][-1]['suggested_action'],'read')
        self.search(fail=True)
        step=self.ledger.view()['workspace']['next_steps'][-1]
        self.assertEqual(step['status'],'execution_failed')
        self.assertTrue(step['reconsider_supported'])

    def test_withdrawn_feedback_removes_support_without_punishing_as_failure(self):
        self.search();out=self.consume();self.ledger.revoke_inquiry(out['episode_id'],'解释不充分')
        self.assertEqual(self.latest()['status'],'feedback_missing')
        self.assertEqual(self.latest()['known_feedback'],0)
        self.assertEqual(self.latest()['new_progress'],0)
        self.assertIsNone(self.latest()['read_outcomes'][0]['answer'])

    def test_revoked_discovery_cannot_remain_evidence_of_a_working_search_approach(self):
        found=self.search();out=self.consume();self.ledger.revoke(found['episode_id'])
        trials=self.ledger.view()['workspace']['method_trials']
        self.assertFalse(any(t['method']=='search' and t['new_progress'] for t in trials))
        self.assertTrue(any(e['id']==out['episode_id'] for e in self.ledger.view()['episodes']))

    def test_another_goals_reading_cannot_validate_this_search(self):
        self.read('b',0,useful=False)
        self.search();self.consume(gid='b')
        search_trial=next(t for t in self.ledger.view()['workspace']['method_trials'] if t['method']=='search')
        self.assertEqual(search_trial['goal_id'],'a');self.assertEqual(search_trial['new_progress'],0)

    def test_private_method_experience_is_not_shared_with_other_actor(self):
        self.search();self.consume()
        with self.ledger.db:
            self.ledger.db.execute("update episodes set scope='private:11',actor='11'")
            self.ledger.db.execute("update goals set scope='private:11',actor='11'")
        self.assertFalse(self.ledger.view()['workspace']['method_trials'])
        self.assertFalse(self.ledger.view('private:22','22')['workspace']['method_trials'])
        self.assertTrue(self.ledger.view('private:11','11')['workspace']['method_trials'])

    def test_failed_invocation_is_not_labeled_question_failure_or_success(self):
        self.search(fail=True)
        self.assertEqual(self.latest()['status'],'execution_failed')
        self.assertEqual(self.latest()['known_feedback'],0)
        self.assertEqual(self.latest()['new_progress'],0)
        self.assertTrue(self.ledger.view()['workspace']['next_steps'][-1]['reconsider_supported'])

    def test_available_alternatives_are_real_permissions_and_search_needs_no_existing_candidate(self):
        from cognition.method_learning import attach_options
        ctx=working_context(self.ledger.view(),[],[])
        ctx.update(scope='public',exploration={'can_search':True,'providers':['library']})
        attach_options(ctx)
        options=ctx['workspace']['available_alternatives'][0]['choices']
        search=next(c for c in options if c['action']=='search')
        self.assertFalse(search['requires_existing_candidate'])
        self.assertIn('rest',{c['action'] for c in options})
        ctx['scope']='private:11';attach_options(ctx)
        self.assertNotIn('search',{c['action'] for c in ctx['workspace']['available_alternatives'][0]['choices']})

    def test_available_alternatives_surfaces_integrate_and_evaluate_when_overflow_or_pending_exists(self):
        from cognition.method_learning import attach_options
        for n in range(3,10):self.read('a',n,useful=True)
        out=self.consume(cap=1)
        self.assertEqual(out['inquiry_assessment']['reason'],'daily_model_budget_exhausted')
        ctx=working_context(self.ledger.view(),[],[])
        attach_options(ctx)
        alt=next(a for a in ctx['workspace']['available_alternatives'] if a['goal_id']=='a')
        self.assertTrue(alt['not_mandatory'])
        actions={c['action'] for c in alt['choices']}
        self.assertIn('integrate',actions)
        self.assertIn('evaluate',actions)


if __name__=='__main__':unittest.main()
