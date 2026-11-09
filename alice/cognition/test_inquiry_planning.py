import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition.exploration import normalize_result
from cognition.inquiry_planning import inputs,current,attempted,validate
from cognition.loop import CognitiveLoop
from cognition.state import Ledger,digest

PLAN={'parts':[{'question':'装置的温度阈值是多少？','evidence_needed':'原始说明中的阈值记录',
               'provider':'bing_rss','query':'温控 关窗 阈值'}],'reason':'阈值和触发机制可以分别查证'}
REST={'action':'rest','goal':None,'resource_id':'','predict_success':False,'reason':'现在不继续','next_check_seconds':600}


class Model:
    requires_inquiry=True
    def __init__(self,rows,hook=None):self.rows=iter(rows);self.messages=[];self.hook=hook
    async def complete(self,messages):
        self.messages.append(messages)
        if self.hook:self.hook()
        row=next(self.rows);return row(messages) if callable(row) else row


class Env:
    def __init__(self):self.cfg={'cognition_inquiry_planning_enabled':True};self.reads=0;self.searches=0
    def exploration_capabilities(self):return {'can_search':True,'providers':['bing_rss'],'provider':'bing_rss'}
    async def observe(self):return [],[]
    async def search(self,query,goal,**kwargs):
        self.searches+=1
        return normalize_result({'status':'ok','results':[{'url':'https://example.org/menu','title':'面包店菜单','snippet':'与温控无关'}]},query,'bing_rss')


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.l=Ledger(self.path);self.env=Env()
        self.origin=self.l.begin({}, {'action':'inspect','resource_id':'b','goal':None,'selection_process':'idle_selective_attention'})
        r={'status':'ok','source_ref':'https://example.org/observed','family':'public_web_page',
           'text':'这个温控装置能够在降温时自动关窗，但没有交代温度阈值。','met_need':False};r['receipt']=digest(r)
        self.l.finish(self.origin,'observed',r)
        self.goal={'id':'g','question':'温度如何触发关窗及阈值是多少？','need':'text','origin':self.origin,'status':'active'}
        self.anchor=self.l.begin({}, {'action':'search','goal':self.goal,'search_query':'original','search_provider':'bing_rss'})
        r=normalize_result({'status':'ok','results':[{'url':'https://example.org/menu','title':'面包店菜单','snippet':'与温控无关'}]},'original','bing_rss')
        r.update(source_ref='search:original',text='',met_need=False,family='web_search_catalog');r['receipt']=digest(r)
        self.l.finish(self.anchor,'observed',r)
        d={'action':'hypothesize','goal':{'id':'g','status':'active'},'resource_id':'','reason':'记录不同关键词的对照',
           'predict_success':True,'next_check_seconds':60,'hypothesis':{'source_episode_id':self.anchor,
           'claim':'替代关键词可能得到相关页面','factor':'query_variant','alternative_query':'variant',
           'alternative_provider':None,'confidence':.5}}
        self.hid=self.tick(Model([d]))['hypothesis_id']
        d={'action':'experiment','goal':{'id':'g','status':'active'},'resource_id':'','reason':'比较实际搜索候选',
           'predict_success':True,'next_check_seconds':60,'hypothesis_id':self.hid}
        def score(messages):
            c=json.loads(messages[-1]['content'])['candidates']
            return {'scores':[{'id':x['id'],'relevant':False,'reason':'候选是菜单，与当前问题无关'} for x in c]}
        self.trial=self.tick(Model([d,score]))['episode_id']
        self.assertIsNotNone(self.bundle())
    def tearDown(self):self.l.close();self.tmp.cleanup()
    def tick(self,model):return asyncio.run(CognitiveLoop(self.l,model,self.env,daily_cap=0).tick(opportunity_source='experiment'))
    def bundle(self):return inputs(self.l,self.env.exploration_capabilities())

    def test_plan_survives_restart_then_free_decision_can_rest_without_more_search(self):
        before=self.env.searches;calls=self.l.db.execute('select sum(calls) from call_budget').fetchone()[0]
        out=self.tick(Model([PLAN]));self.assertEqual(out['status'],'planned')
        self.l.close();self.l=Ledger(self.path)
        model=Model([REST]);self.assertEqual(self.tick(model)['status'],'rest')
        c=json.loads(model.messages[0][-1]['content'])
        self.assertEqual(c['prospective_goal_plan']['parts'],PLAN['parts'])
        self.assertEqual(c['prospective_goal_plan']['goal_id'],'g')
        self.assertEqual(self.env.searches,before)
        self.assertEqual(self.l.db.execute('select sum(calls) from call_budget').fetchone()[0]-calls,2)
        self.assertFalse(self.l.view()['progress_memory']);self.assertFalse(self.l.view()['inquiry_model'])

    def test_no_parts_is_valid_and_not_repeated(self):
        out=self.tick(Model([{**PLAN,'parts':[]}]))
        self.assertEqual(out['part_count'],0);self.assertTrue(attempted(self.l,self.bundle()))
        self.assertEqual(self.tick(Model([REST]))['status'],'rest')

    def test_failure_is_retained_once_and_invalid_self_output_not_reinjected(self):
        out=self.tick(Model([{**PLAN,'parts':[{**PLAN['parts'][0],'provider':'unauthorized','answer':'FALSE_SENTINEL'}]}]))
        self.assertEqual(out['status'],'planning_error');self.assertIsNone(current(self.l,self.bundle()))
        raw=self.l.db.execute('select result_json from episodes where id=?',(out['episode_id'],)).fetchone()[0]
        self.assertIn('FALSE_SENTINEL',raw)
        model=Model([REST]);self.tick(model)
        self.assertNotIn('FALSE_SENTINEL',json.dumps(model.messages,ensure_ascii=False))

    def test_withdrawn_measurement_or_original_observation_removes_plan(self):
        self.tick(Model([PLAN]));self.assertIsNotNone(current(self.l,self.bundle()))
        self.l.revoke_experiment(self.trial,'该相关性评估需要撤回')
        self.assertIsNone(self.bundle())
        self.assertEqual(self.tick(Model([REST]))['status'],'rest')

    def test_revoking_plan_allows_explicit_retry_without_reviving_old_plan(self):
        out=self.tick(Model([PLAN]));self.l.revoke(out['episode_id'])
        self.assertFalse(attempted(self.l,self.bundle()));self.assertIsNone(current(self.l,self.bundle()))
        self.assertEqual(self.tick(Model([{**PLAN,'parts':[]}]))['status'],'planned')

    def test_input_withdrawal_or_configuration_change_during_call_cancels_write(self):
        out=self.tick(Model([PLAN],hook=lambda:self.l.revoke(self.origin)))
        self.assertEqual(out['status'],'cancelled');self.assertIsNone(self.bundle())

    def test_disable_during_call_cancels_without_a_usable_plan(self):
        out=self.tick(Model([PLAN],hook=lambda:self.env.cfg.update(cognition_inquiry_planning_enabled=False)))
        self.assertEqual(out['status'],'cancelled');self.assertIsNone(current(self.l,self.bundle()))

    def test_unknown_source_or_disabled_flag_skips_planning(self):
        self.env.cfg['cognition_inquiry_planning_enabled']=False
        m=Model([REST]);self.assertEqual(self.tick(m)['status'],'rest')
        self.assertNotIn('prospective_goal_plan',json.loads(m.messages[0][-1]['content']))

    def test_disabling_or_withdrawing_dependency_cannot_echo_old_plan_from_history(self):
        plan={**PLAN,'parts':[{**PLAN['parts'][0],'query':'PLANNING_ONLY_SENTINEL'}]}
        self.tick(Model([plan]));self.env.cfg['cognition_inquiry_planning_enabled']=False
        model=Model([REST]);self.tick(model)
        self.assertNotIn('PLANNING_ONLY_SENTINEL',json.dumps(model.messages))
        self.env.cfg['cognition_inquiry_planning_enabled']=True
        self.l.revoke(self.trial);model=Model([REST]);self.tick(model)
        self.assertNotIn('PLANNING_ONLY_SENTINEL',json.dumps(model.messages))

    def test_multiple_goals_leave_planning_to_general_control(self):
        g={**self.goal,'id':'another','question':'另一个正在考虑的独立问题是什么？'}
        eid=self.l.begin({}, {'action':'rest','goal':g});self.l.finish(eid,'rest',{})
        self.assertIsNone(self.bundle())
        self.assertEqual(self.tick(Model([REST]))['status'],'rest')

    def test_changed_plan_payload_without_receipt_is_not_usable(self):
        out=self.tick(Model([PLAN]));row=self.l.db.execute('select result_json from episodes where id=?',(out['episode_id'],)).fetchone()
        r=json.loads(row[0]);r['parts'][0]['query']='tampered'
        with self.l.db:self.l.db.execute('update episodes set result_json=? where id=?',(json.dumps(r),out['episode_id']))
        self.assertIsNone(current(self.l,self.bundle()))

    def test_schema_discards_answers_and_rejects_unbounded_or_injected_queries(self):
        cap=self.env.exploration_capabilities()
        self.assertNotIn('answer',validate({**PLAN,'answer':'not knowledge'},cap))
        for q in ['a','abc\ncommand','a'*121]:
            with self.assertRaises(ValueError):validate({**PLAN,'parts':[{**PLAN['parts'][0],'query':q}]},cap)
        with self.assertRaises(ValueError):validate({**PLAN,'parts':PLAN['parts']*4},cap)

    def test_blank_optional_explanation_does_not_discard_valid_unknown_questions(self):
        cap=self.env.exploration_capabilities()
        raw=json.dumps(PLAN,ensure_ascii=False)[:-1]+',"reason":""}'
        plan=validate(raw,cap)
        self.assertEqual(plan,{'parts':PLAN['parts'],'reason':''})
        self.assertEqual(validate({'parts':PLAN['parts']},cap)['reason'],'')
        with self.assertRaises(ValueError):validate({**PLAN,'reason':[]},cap)
        with self.assertRaises(ValueError):validate({**PLAN,'parts':[{**PLAN['parts'][0],'evidence_needed':''}],'reason':''},cap)
