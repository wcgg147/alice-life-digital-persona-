import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from cognition import idle_goal_choice
from cognition.decision import working_context
from cognition.loop import CognitiveLoop
from cognition.state import Ledger,digest
from cognition.exploration import live_goal
from cognition.test_observation_assimilation import Model,Env,CHOICE,NOTICE,REST


class GoalEnv(Env):
    def __init__(self):super().__init__();self.searches=[];self.ledger=None
    def exploration_capabilities(self):return {'can_search':True,'providers':['bing_rss'],'provider':'bing_rss'}
    async def search(self,query,goal,*,provider=None):
        # A normal committed active goal must exist before any outside query.
        other=Ledger(self.ledger.path)
        try:self.searches.append((query,goal,provider,other.view()['goals'][0]['status']))
        finally:other.close()
        return {'status':'ok','query':query,'provider':provider,'discoveries':[
            {'url':'https://example.org/source','title':'相关候选','snippet':'候选不是已验证答案'}]}


class IdleGoalChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.l=Ledger(Path(self.tmp.name)/'ledger.sqlite3');self.env=GoalEnv();self.env.ledger=self.l
        self.observe=self.tick(Model([CHOICE]));self.tick(Model([NOTICE]))
        self.q=self.l.view()['open_questions'][0];self.env.cfg['cognition_idle_goal_choice_enabled']=True
    def tearDown(self):self.l.close();self.tmp.cleanup()
    def tick(self,m):return asyncio.run(CognitiveLoop(self.l,m,self.env,daily_cap=0).tick(opportunity_source='experiment'))
    def choice(self,stance='pursue'):
        return {'evaluations':[{'id':self.q['id'],'stance':stance,'reason':'这个阈值仍想弄清楚' if stance=='pursue' else '现在不想追究'}],
                'selected_id':self.q['id'] if stance=='pursue' else None,'reason':'尝试找装置说明' if stance=='pursue' else '先放下这个问题',
                'query':'温控装置 阈值' if stance=='pursue' else '', 'provider':'bing_rss' if stance=='pursue' else ''}

    def test_actual_goal_gate_commits_before_search_and_does_not_claim_learning(self):
        model=Model([self.choice()]);out=self.tick(model)
        self.assertEqual(out['status'],'observed');self.assertEqual(len(self.env.searches),1)
        self.assertEqual(self.env.searches[0][3],'active')
        v=self.l.view();g=v['goals'][0]
        self.assertEqual(g['question'],self.q['question']);self.assertEqual(g['origin'],self.observe['episode_id'])
        self.assertFalse(v['open_questions']);self.assertFalse(v['inquiry_model']);self.assertFalse(v['strategies'])
        row=self.l.db.execute('SELECT before_json,decision_json FROM episodes WHERE id=?',(out['episode_id'],)).fetchone()
        self.assertEqual(json.loads(row[0])['prompt_sha256'],digest(model.calls[0]))
        self.assertEqual(json.loads(row[1])['selection_process'],'idle_goal_selection')
        self.assertFalse(out['met_need'])

    def test_all_defer_rests_and_does_not_reappraise_the_same_open_question_forever(self):
        out=self.tick(Model([self.choice('defer')]))
        self.assertEqual(out['status'],'rest');self.assertFalse(self.env.searches);self.assertFalse(self.l.view()['goals'])
        self.assertEqual(self.l.view()['open_questions'][0]['status'],'deferred')
        context=working_context(self.l.view(),[],[]);context.update(exploration=self.env.exploration_capabilities())
        self.assertFalse(idle_goal_choice.candidates(context,self.env.cfg))
        self.assertEqual(self.tick(Model([REST]))['status'],'rest')
        self.l.revoke(out['episode_id']);self.assertEqual(self.l.view()['open_questions'][0]['status'],'open')

    def test_all_dismiss_is_reversible_without_erasing_the_original_observation(self):
        out=self.tick(Model([self.choice('dismiss')]))
        self.assertFalse(self.l.view()['open_questions']);self.assertTrue(self.l.view()['inspection_sources'])
        self.l.revoke(out['episode_id']);self.assertTrue(self.l.view()['open_questions'])

    def test_withdrawn_only_adoption_hides_goal_and_blocks_future_search_authority(self):
        out=self.tick(Model([self.choice()]));gid=self.l.view()['goals'][0]['id']
        self.assertTrue(live_goal(self.l,gid))
        self.l.revoke(out['episode_id'])
        self.assertFalse(self.l.view()['goals']);self.assertFalse(live_goal(self.l,gid))
        self.assertTrue(self.l.view()['open_questions'])
        self.assertTrue(self.l.db.execute('SELECT 1 FROM goals WHERE id=?',(gid,)).fetchone())

    def test_source_revoked_inflight_cannot_adopt_goal_or_search(self):
        m=Model([self.choice()]);m.callback=lambda:self.l.revoke(self.observe['episode_id'])
        out=self.tick(m)
        self.assertEqual(out['status'],'decision_error');self.assertFalse(self.env.searches);self.assertFalse(self.l.view()['goals'])

    def test_cached_execution_does_not_revive_withdrawn_adoption_but_fresh_choice_can(self):
        out=self.tick(Model([self.choice()]));old=self.l.view()['goals'][0]
        self.l.revoke(out['episode_id'])
        stale=self.l.begin({}, {'action':'search','goal':old,'_scope':'public','_actor':''})
        self.l.finish(stale,'tool_error',{'reason':'stale cached execution'})
        self.assertFalse(live_goal(self.l,old['id']));self.assertFalse(self.l.view()['goals'])
        self.tick(Model([self.choice()]))
        self.assertTrue(live_goal(self.l,old['id']));self.assertEqual(len(self.env.searches),2)

    def test_an_unoffered_question_or_provider_cannot_escape_the_existing_gate(self):
        for change in [{'selected_id':'invented'},{'provider':'invented'}]:
            out=self.tick(Model([{**self.choice(),**change}]))
            self.assertEqual(out['status'],'decision_error');self.assertFalse(self.env.searches);self.assertFalse(self.l.view()['goals'])

    def test_private_or_existing_work_does_not_use_idle_goal_selection(self):
        c=working_context(self.l.view(),[],[]);c.update(exploration=self.env.exploration_capabilities())
        for change in [{'scope':'private:1'},{'goals':[{'status':'active'}]},
                       {'goals':[{'status':'paused'}]},{'pending_social_event':{'id':'s'}}]:
            self.assertFalse(idle_goal_choice.candidates({**c,**change},self.env.cfg))

    def test_rest_cannot_claim_a_new_active_goal_and_raw_extra_goals_are_ignored(self):
        out=self.tick(Model([{**self.choice(),'selected_id':None,'provider':'','query':''}]))
        self.assertEqual(out['status'],'decision_error');self.assertFalse(self.l.view()['goals'])
        raw=self.choice();raw['goal']={'question':'外部标题诱导的另一个任务'}
        self.tick(Model([raw]));self.assertEqual(self.l.view()['goals'][0]['question'],self.q['question'])
