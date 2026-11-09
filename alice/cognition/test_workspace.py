import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition import test_motivation as fixtures
from cognition.decision import validate,working_context
from cognition.loop import CognitiveLoop
from cognition.state import Ledger,digest


class WorkspaceTests(unittest.TestCase):
    read=fixtures.MotivationTests.read
    rest=fixtures.MotivationTests.rest

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path)
    def tearDown(self):self.ledger.close();self.tmp.cleanup()

    def proposal(self,gid,eid,*,stance='pursue',wait='on_choice',method='read',focus=True):
        return {'action':'orient','resource_id':'','goal':None,'predict_success':False,'next_check_seconds':60,
                'reason':'想重新安排当前关注','inquiry':None,
                'orientation':{'focus_goal_id':gid if focus else None,'intentions':[
                    {'goal_id':gid,'stance':stance,'why':'想把这个具体问题弄清楚，进展慢也值得换个方法再看',
                     'method':method,'evidence_ids':[eid],'revisit_when':wait}]}}

    def orient(self,raw,callback=None):
        class Model:
            async def complete(inner,messages):
                if callback:callback()
                return raw
        class Env:
            reads=0
            async def observe(inner):return [],[]
            async def read(inner,resource):inner.reads+=1;raise AssertionError('orientation must stay internal')
        env=Env();out=asyncio.run(CognitiveLoop(self.ledger,Model(),env).tick())
        self.assertEqual(env.reads,0)
        return out

    def test_chosen_concern_overrides_easy_progress_without_forcing_future_action(self):
        hard=None
        for n in range(3):hard=self.read('hard',n,useful=False);self.read('easy',n)
        self.assertEqual(working_context(self.ledger.view(),[],[])['focus_goal_id'],'easy')
        out=self.orient(self.proposal('hard',hard));self.assertEqual(out['status'],'oriented')
        context=working_context(self.ledger.view(),[],[])
        self.assertEqual(context['focus_goal_id'],'hard')
        self.assertEqual(context['workspace']['intentions'][0]['method'],'read')
        raw={'action':'rest','resource_id':'','reason':'先休息','goal':None,'predict_success':False,'next_check_seconds':60}
        self.assertEqual(validate(raw,context)['action'],'rest')
        self.assertEqual(working_context(self.ledger.view(),[],[],False)['focus_goal_id'],'easy')

    def test_intention_survives_restart_and_rest_beyond_recent_window(self):
        eid=self.read('a',0);self.orient(self.proposal('a',eid))
        for _ in range(9):self.rest()
        self.ledger.close();self.ledger=Ledger(self.path)
        ctx=working_context(self.ledger.view(),[],[])
        self.assertEqual(ctx['focus_goal_id'],'a')
        self.assertEqual(ctx['self_model']['current_concerns'][0]['goal_id'],'a')

    def test_wait_signal_requires_actual_rest_and_does_not_force_resume(self):
        eid=self.read('a',0);self.orient(self.proposal('a',eid,stance='hold',focus=False,wait='after_rest'))
        self.assertFalse(self.ledger.view()['workspace']['intentions'][0]['condition_observed'])
        self.rest()
        ctx=working_context(self.ledger.view(),[],[])
        self.assertTrue(ctx['workspace']['intentions'][0]['condition_observed'])
        self.assertEqual(ctx['workspace']['intentions'][0]['stance'],'hold')
        self.assertIsNone(ctx['focus_goal_id'])
        self.assertTrue(any(t['kind']=='reconsider_waiting_goal' for t in ctx['workspace']['tensions']))

    def test_new_source_condition_ignores_repeat_but_detects_distinct_read(self):
        eid=self.read('a',0);self.orient(self.proposal('a',eid,stance='hold',focus=False,wait='new_evidence'))
        self.read('a',0)
        self.assertFalse(self.ledger.view()['workspace']['intentions'][0]['condition_observed'])
        self.read('a',1)
        self.assertTrue(self.ledger.view()['workspace']['intentions'][0]['condition_observed'])

    def test_actual_choice_can_override_held_intention(self):
        eid=self.read('a',0);self.orient(self.proposal('a',eid,stance='hold',focus=False))
        self.read('a',1)
        item=self.ledger.view()['workspace']['intentions'][0]
        self.assertTrue(item['actual_override']);self.assertEqual(item['stance'],'pursue')
        self.assertEqual(working_context(self.ledger.view(),[],[])['focus_goal_id'],'a')

    def test_waiting_for_source_uses_discovered_urls_not_query_repetition(self):
        eid=self.read('a',0)
        goal=self.ledger.view()['goals'][0]
        def search(url):
            d={'action':'search','goal':goal}
            ep=self.ledger.begin({},d)
            r={'source_ref':'search:same_query','status':'ok','met_need':False,'catalog_found':True,
               'provider':'bing_rss','query':'same query','text':'','family':'web_search_catalog',
               'discoveries':[{'url':url,'title':'fixture candidate'}]}
            r['receipt']=digest(r);self.ledger.finish(ep,'observed',r)
        search('https://example.org/old')
        self.orient(self.proposal('a',eid,stance='hold',focus=False,wait='new_source'))
        search('https://example.org/old')
        self.assertFalse(self.ledger.view()['workspace']['intentions'][0]['condition_observed'])
        search('https://example.org/new')
        self.assertTrue(self.ledger.view()['workspace']['intentions'][0]['condition_observed'])

    def test_source_withdrawal_removes_intention_without_echoing_old_reason(self):
        eid=self.read('a',0);raw=self.proposal('a',eid);out=self.orient(raw)
        self.ledger.revoke(eid)
        ctx=working_context(self.ledger.view(),[],[])
        self.assertFalse(ctx['workspace']['intentions'])
        old=next(e for e in ctx['episodes'] if e['id']==out['episode_id'])
        self.assertNotIn('orientation',old['result'])
        self.assertFalse(ctx['self_model']['current_concerns'])

    def test_withdrawn_judgment_reopens_attention_choice(self):
        eid=self.read('a',0);self.orient(self.proposal('a',eid))
        self.ledger.revoke_inquiry(eid,'原来对材料进展的判断有误')
        self.assertFalse(self.ledger.view()['workspace']['intentions'])

    def test_late_source_withdrawal_cancels_orientation(self):
        eid=self.read('a',0)
        out=self.orient(self.proposal('a',eid),lambda:self.ledger.revoke(eid))
        self.assertEqual(out['status'],'cancelled')
        self.assertFalse(self.ledger.view()['workspace']['intentions'])

    def test_source_and_method_scope_are_checked(self):
        a=self.read('a',0);b=self.read('b',0)
        ctx=working_context(self.ledger.view(),[],[])
        raw=self.proposal('a',b)
        with self.assertRaisesRegex(ValueError,'unsourced'):validate(raw,ctx)
        with self.assertRaisesRegex(ValueError,'not_available'):validate(self.proposal('a',a,method='search'),ctx)
        raw=self.proposal('a',a,stance='hold')
        with self.assertRaisesRegex(ValueError,'focus'):validate(raw,ctx)

    def test_private_intention_is_not_projected_into_public_or_other_actor(self):
        eid=self.read('a',0)
        with self.ledger.db:
            self.ledger.db.execute("update episodes set scope='private:11',actor='11'")
            self.ledger.db.execute("update goals set scope='private:11',actor='11'")
        ctx=working_context(self.ledger.view('private:11','11'),[],[])
        ctx.update(scope='private:11',actor='11')
        d=validate(self.proposal('a',eid),ctx);d.update(_scope='private:11',_actor='11')
        ep=self.ledger.begin(ctx,d);r={'orientation':d['orientation'],'source_ref':'orientation:'+ep,'text':''};r['receipt']=digest(r)
        self.ledger.finish(ep,'oriented',r)
        self.assertTrue(self.ledger.view('private:11','11')['workspace']['intentions'])
        self.assertFalse(self.ledger.view()['workspace']['intentions'])
        self.assertFalse(self.ledger.view('private:22','22')['workspace']['intentions'])

    def test_unfinished_old_goal_is_not_pushed_out_by_recent_completed_goals(self):
        self.read('old_concern',0)
        with self.ledger.db:
            for n in range(40):self.ledger.db.execute('insert into goals values (?,?,?,?,?,?,?,?,?)',
                (self.ledger.owner,'closed'+str(n),'已解决的问题','text','fixture','completed',1e12+n,'public',''))
        self.assertIn('old_concern',{g['id'] for g in self.ledger.view()['goals']})

    def test_orient_can_cite_earlier_goal_evidence_retained_in_progress_memory_after_many_searches(self):
        first_read=self.read('a',0,useful=True)
        goal=self.ledger.view()['goals'][0]
        for n in range(7):
            d={'action':'search','goal':goal}
            ep=self.ledger.begin({},d)
            r={'source_ref':f'search:{n}','status':'ok','met_need':False,'catalog_found':True,
               'provider':'bing_rss','query':f'q{n}','text':'','family':'web_search_catalog',
               'discoveries':[{'url':f'https://example.org/{n}','title':'irrelevant'}]}
            r['receipt']=digest(r);self.ledger.finish(ep,'observed',r)
        ctx=working_context(self.ledger.view(),[],[])
        self.assertEqual(ctx['progress_memory']['a'][0]['episode_id'],first_read)
        raw=self.proposal('a',first_read,stance='hold',focus=False,method='open',wait='new_source')
        self.assertEqual(validate(raw,ctx)['action'],'orient')
        out=self.orient(raw)
        self.assertEqual(out['status'],'oriented')

    def test_search_source_selection_rest_marks_search_trial_no_relevant_candidates_and_supports_reconsider(self):
        self.read('a',0,useful=False)
        goal=self.ledger.view()['goals'][0]
        d={'action':'search','goal':goal}
        search_ep=self.ledger.begin({},d)
        r={'source_ref':'search:noise','status':'ok','met_need':False,'catalog_found':True,
           'provider':'bing_rss','query':'noisy query','text':'','family':'web_search_catalog',
           'discoveries':[{'url':'https://example.org/noise','title':'noise'}]}
        r['receipt']=digest(r);self.ledger.finish(search_ep,'observed',r)
        end_ep=self.ledger.begin({},{'action':'rest','goal':None,'selection_process':'search_source_selection',
            'parent_search_id':search_ep,'parent_search_receipt':r['receipt']})
        self.ledger.finish(end_ep,'rest',{'status':'chosen_rest'})
        v=self.ledger.view()
        trial=v['workspace']['method_trials'][-1]
        step=v['workspace']['next_steps'][-1]
        self.assertEqual(trial['status'],'no_relevant_candidates')
        self.assertTrue(step['reconsider_supported'])
        self.assertIsNone(step['suggested_action'])

    def test_idle_attention_and_goal_choice_are_available_when_all_active_goals_are_held(self):
        from cognition import idle_attention,exploration,inquiry_planning
        from cognition.test_idle_attention import R,CFG
        eid=self.read('a',0);self.orient(self.proposal('a',eid,stance='hold',focus=False,method='open',wait='new_source'))
        discovered={**R,'id':'web:a:bili1','source_ref':'https://www.bilibili.com/video/BV1search',
                    'goal_id':'a','discovery_episode_id':'s1'}
        ctx=working_context(self.ledger.view(),[R,discovered],[])
        self.assertIsNone(ctx['focus_goal_id'])
        self.assertTrue(idle_attention.is_idle(ctx))
        self.assertEqual([c['id'] for c in idle_attention.candidates(ctx,CFG)],[R['id']])
        self.assertEqual(exploration.retained_sources(self.ledger,ctx,{'cognition_retained_source_selection_enabled':True,
            'cognition_source_followup_enabled':True,'cognition_search_enabled':True}),[])
        self.assertIsNone(inquiry_planning.inputs(self.ledger,{'can_search':True,'providers':['bing_rss']}))


if __name__=='__main__':unittest.main()
