import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition import test_motivation as fixtures
from cognition.decision import working_context
from cognition.loop import CognitiveLoop
from cognition.state import Ledger,digest


class QuestionStateTests(unittest.TestCase):
    read=fixtures.MotivationTests.read
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path)
        self.a=self.read('a',0);self.b=self.read('a',1)
    def tearDown(self):self.ledger.close();self.tmp.cleanup()
    def initial(self):return {'parts':[
        {'id':'','question':'触发条件是什么？','conclusion':'温度变化触发开关','coverage':'answer','evidence':[{'episode_id':self.a,'span_id':'s0000'}],'challenges':[]},
        {'id':'','question':'执行的具体阈值是多少？','conclusion':'','evidence':[],'challenges':[]}]}
    def integrate(self,raw=None,callback=None,cap=20):
        calls=[];raw=raw or self.initial()
        class Model:
            async def complete(inner,messages):
                calls.append(messages)
                if len(calls)==1:return {'action':'integrate','integration_goal_id':'a','goal':None,'resource_id':'',
                    'predict_success':False,'next_check_seconds':600,'reason':'整理已有回答与剩余问题'}
                if callback:callback()
                return raw
        class Env:
            async def observe(inner):return [],[]
            async def read(inner,resource):raise AssertionError('internal integration must not read externally')
        out=asyncio.run(CognitiveLoop(self.ledger,Model(),Env(),daily_cap=cap).tick(opportunity_source='experiment'))
        return out,calls
    def state(self):return self.ledger.view()['question_states']['a']

    def test_integrated_subquestions_persist_and_only_open_parts_drive_remaining_gap(self):
        out,_=self.integrate();self.assertEqual(out['status'],'integrated')
        state=self.state();self.assertEqual([p['status'] for p in state['parts']],['tentatively_answered','unresolved'])
        self.assertEqual(self.ledger.view()['workspace']['next_steps'][0]['remaining_gap'],'执行的具体阈值是多少？')
        self.ledger.close();self.ledger=Ledger(self.path)
        self.assertEqual(self.state()['parts'][0]['status'],'tentatively_answered')
        self.assertFalse(self.ledger.view()['question_integrations'])

    def test_new_missing_prose_cannot_reopen_supported_subquestion(self):
        self.integrate();eid=self.read('a',2)
        from cognition.inquiry import validate_assessment
        with self.ledger.db:
            row=self.ledger.db.execute('select * from episodes where id=?',(eid,)).fetchone();r=json.loads(row['result_json']);d=json.loads(row['decision_json'])
            raw={'schema_version':2,'progress_type':'partial_answer','criterion_status':'met','novelty':'new',
                 'missing':'触发条件是什么？','prior_episode_ids':[],'verdict':'useful','evidence_id':'s0000','answer':'读到其他局部信息','reason':'测试旧缺口提案不能覆盖已知'}
            r['inquiry_assessment']=validate_assessment(raw,r['text'],d['inquiry'],r['receipt'])
            self.ledger.db.execute('update episodes set result_json=? where id=?',(json.dumps(r),eid))
        self.assertEqual(self.state()['parts'][0]['status'],'tentatively_answered')
        step=self.ledger.view()['workspace']['next_steps'][0]
        self.assertNotIn('触发条件',step['remaining_gap']);self.assertTrue(step['has_unintegrated_sources'])

    def test_omission_or_blank_conclusion_does_not_clear_old_support(self):
        self.integrate();part=self.state()['parts'][0];self.read('a',2)
        raw={'parts':[{'id':part['id'],'question':part['question'],'conclusion':'','evidence':[],'challenges':[]}]}
        out,_=self.integrate(raw);self.assertEqual(out['status'],'integrated')
        self.assertEqual(self.state()['parts'][0]['status'],'tentatively_answered')

    def test_new_counterevidence_is_contested_and_withdrawal_restores_old_support(self):
        self.integrate();part=self.state()['parts'][0];old=part['claims'][0]['id']
        goal=self.ledger.view()['goals'][0]
        ep=self.ledger.begin({}, {'action':'read','goal':goal,'resource_id':'counter'})
        text='复测发现温度变化并不触发开关，先前推断有误；观察显示光照变化触发开关。'*4
        r={'status':'ok','source_ref':'fixture:counter','family':'fixture','met_need':True,'text':text};r['receipt']=digest(r);self.ledger.finish(ep,'observed',r)
        raw={'parts':[{'id':part['id'],'question':part['question'],'conclusion':'光照变化触发而非温度','coverage':'answer',
            'evidence':[{'episode_id':ep,'span_id':'s0000'}],'challenges':[old]}]}
        out,_=self.integrate(raw);self.assertEqual(out['status'],'integrated')
        self.assertEqual(self.state()['parts'][0]['status'],'contested')
        self.ledger.revoke(ep);self.assertEqual(self.state()['parts'][0]['status'],'tentatively_answered')

    def test_source_withdrawal_reopens_only_its_part_and_integration_withdrawal_removes_schema(self):
        out,_=self.integrate();self.ledger.revoke(self.a)
        self.assertEqual(self.state()['parts'][0]['status'],'unresolved')
        self.ledger.revoke(out['episode_id']);self.assertFalse(self.ledger.view()['question_states'])

    def test_late_source_withdrawal_prevents_installing_a_stale_integration(self):
        out,_=self.integrate(callback=lambda:self.ledger.revoke(self.a))
        self.assertEqual(out['status'],'integration_error');self.assertFalse(self.ledger.view()['question_states'])

    def test_unseen_span_and_other_question_source_are_rejected(self):
        raw=self.initial();raw['parts'][0]['evidence'][0]['span_id']='s9999'
        out,_=self.integrate(raw);self.assertEqual(out['status'],'integration_error')
        other=self.read('b',0);raw=self.initial();raw['parts'][0]['evidence'][0]['episode_id']=other
        out,_=self.integrate(raw);self.assertEqual(out['status'],'integration_error')
        self.assertFalse(self.ledger.view()['question_states'])

    def test_budget_failure_remains_retryable_and_does_not_create_knowledge(self):
        out,calls=self.integrate(cap=1)
        self.assertEqual(out['status'],'integration_error');self.assertEqual(len(calls),1)
        self.assertTrue(self.ledger.view()['question_integrations']);self.assertFalse(self.ledger.view()['question_states'])

    def test_failed_integration_is_archived_and_only_error_feedback_reaches_retry(self):
        self.integrate();part=self.state()['parts'][0]
        self.read('a',2,useful=True)
        bad={'parts':[{'id':part['id'],'question':'改写了原来的问题','conclusion':'UNACCEPTED_REPLY_SENTINEL',
            'coverage':'answer','evidence':[{'episode_id':self.a,'span_id':'s0000'}],'challenges':[]}]}
        out,_=self.integrate(bad);self.assertEqual(out['status'],'integration_error')
        row=self.ledger.db.execute('select result_json from episodes where id=?',(out['episode_id'],)).fetchone()
        archived=json.loads(row[0]);self.assertEqual(json.loads(archived['raw_output']),bad)
        ctx=working_context(self.ledger.view(),[],[])
        self.assertNotIn('UNACCEPTED_REPLY_SENTINEL',json.dumps(ctx))
        failed=next(e for e in ctx['episodes'] if e['id']==out['episode_id'])
        self.assertEqual(failed['result']['code'],'subquestion_identity_changed')
        retried,calls=self.integrate()
        self.assertEqual(retried['status'],'integrated')
        payload=json.loads(calls[1][1]['content'])
        self.assertEqual(payload['previous_attempt']['validation_code'],'subquestion_identity_changed')
        self.assertNotIn('UNACCEPTED_REPLY_SENTINEL',calls[1][1]['content'])

    def test_private_integration_does_not_leak_to_public_or_other_actor(self):
        out,_=self.integrate()
        with self.ledger.db:
            self.ledger.db.execute("update episodes set scope='private:11',actor='11'")
            self.ledger.db.execute("update goals set scope='private:11',actor='11'")
        self.assertFalse(self.ledger.view()['question_states'])
        self.assertFalse(self.ledger.view('private:22','22')['question_states'])
        self.assertTrue(self.ledger.view('private:11','11')['question_states'])

    def test_ablation_removes_question_state_and_old_integration_prose(self):
        out,_=self.integrate();ctx=working_context(self.ledger.view(),[],[],False)
        self.assertFalse(ctx['question_states'])
        ep=next(e for e in ctx['episodes'] if e['id']==out['episode_id'])
        self.assertNotIn('parts',ep['result'])

    def test_partial_source_support_does_not_resolve_the_subquestion(self):
        raw=self.initial();raw['parts'][0]['coverage']='partial'
        out,_=self.integrate(raw);self.assertEqual(out['status'],'integrated')
        part=self.state()['parts'][0];self.assertEqual(part['status'],'partial')
        self.assertIn(part['id'],self.state()['open_part_ids'])

    def test_unresolved_parts_actually_guide_candidate_attention_and_source_withdrawal_reopens_focus(self):
        self.integrate()
        resources=[{'id':'known','source_ref':'fixture:known','family':'new_family','title':'触发条件是什么？'},
                   {'id':'open','source_ref':'fixture:open','family':'new_family','title':'执行的具体阈值是多少？'}]
        view=self.ledger.view();ctx=working_context(view,resources,[])
        self.assertEqual(ctx['resources'][0]['id'],'open')
        old_relevance=next(r for r in ctx['resources'] if r['id']=='known')['attention_signals']['relevance']
        self.assertEqual(ctx['attention_focus']['questions'],['执行的具体阈值是多少？'])
        self.assertEqual(ctx['goals'][0]['question'],view['goals'][0]['question'])
        self.assertIsNone(working_context(view,resources,[],False)['attention_focus'])
        self.ledger.revoke(self.a)
        ctx=working_context(self.ledger.view(),resources,[])
        self.assertIn('触发条件是什么？',ctx['attention_focus']['questions'])
        self.assertGreater(next(r for r in ctx['resources'] if r['id']=='known')['attention_signals']['relevance'],old_relevance)

    def test_attention_focus_keeps_legacy_gap_uncertainty_explicit(self):
        from cognition.test_inquiry import detail
        from cognition.inquiry import validate_assessment
        with self.ledger.db:
            row=self.ledger.db.execute('select * from episodes where id=?',(self.b,)).fetchone();r=json.loads(row['result_json']);d=json.loads(row['decision_json'])
            a={**detail(),'verdict':'useful','evidence_id':'s0000','answer':'温度变化触发开关','reason':'只知部分链条','missing':'执行阈值是多少？'}
            r['inquiry_assessment']=validate_assessment(a,r['text'],d['inquiry'],r['receipt'])
            self.ledger.db.execute('update episodes set result_json=? where id=?',(json.dumps(r),self.b))
        ctx=working_context(self.ledger.view(),[],[])
        self.assertEqual(ctx['attention_focus']['basis'],'fallible_last_known_gap_proposal')
        self.assertEqual(ctx['attention_focus']['source_episode'],self.b)

    def test_useful_milestone_sources_are_not_evicted_by_later_unassessed_or_non_progress_reads(self):
        early=[self.a,self.b,self.read('a',2,useful=True)]
        for n in range(3,11):
            self.read('a',n,useful=False)
        for n in range(11,14):
            ep=self.read('a',n,useful=False)
            with self.ledger.db:
                row=self.ledger.db.execute('select result_json from episodes where id=?',(ep,)).fetchone()
                r=json.loads(row[0]);r['inquiry_assessment']={'status':'unassessed','reason':'assessment_failed'}
                self.ledger.db.execute('update episodes set result_json=? where id=?',(json.dumps(r),ep))
        latest=self.read('a',14,useful=True)
        view=self.ledger.view()
        opt=next(o for o in view['question_integrations'] if o['goal_id']=='a')
        self.assertEqual(len(opt['source_ids']),12)
        for eid in early:
            self.assertIn(eid,opt['source_ids'])
        self.assertIn(latest,opt['source_ids'])
        self.assertEqual(opt['assessed_useful_count'],4)
        self.assertGreater(opt['outside_recent_episodes_count'],0)
        self.assertTrue(any(t.get('goal_id')=='a' and t.get('kind')=='unintegrated_progress_outside_window'
                            for t in view['workspace']['tensions']))
        self.assertTrue(any(t.get('goal_id')=='a' and t.get('kind')=='unassessed_saved_reads'
                            for t in view['workspace']['tensions']))
        out,_=self.integrate()
        self.assertEqual(out['status'],'integrated')
        self.assertFalse(any(t.get('goal_id')=='a' and t.get('kind')=='unintegrated_progress_outside_window'
                             for t in self.ledger.view()['workspace']['tensions']))

    def test_recent_accumulated_progress_offers_integration_before_window_overflow(self):
        from cognition.method_learning import attach_options
        self.integrate()
        for n in range(2,5):self.read('a',n,useful=True)
        view=self.ledger.view();opt=next(o for o in view['question_integrations'] if o['goal_id']=='a')
        self.assertEqual(opt['outside_recent_episodes_count'],0)
        self.assertEqual(opt['assessed_useful_count'],3)
        step=next(s for s in view['workspace']['next_steps'] if s['goal_id']=='a')
        self.assertEqual(step['suggested_action'],'integrate')
        ctx=working_context(view,[{'id':'next','family':'fixture','source_ref':'fixture:a:next','title':'新来源'}],[])
        ctx.update(scope='public',exploration={'can_search':True,'providers':['bing_rss']})
        attach_options(ctx)
        options=next(o for o in ctx['workspace']['available_alternatives'] if o['goal_id']=='a')
        self.assertTrue(options['not_mandatory'])
        self.assertIn('integrate',{c['action'] for c in options['choices']})
        self.assertIn('read',{c['action'] for c in options['choices']})
        self.assertIn('rest',{c['action'] for c in options['choices']})
        self.assertEqual([p['status'] for p in self.state()['parts']],['tentatively_answered','unresolved'])

    def test_existing_claim_sources_outlive_more_than_twelve_new_useful_reads(self):
        self.integrate()
        newer=[self.read('a',n,useful=True) for n in range(2,18)]
        opt=next(o for o in self.ledger.view()['question_integrations'] if o['goal_id']=='a')
        self.assertEqual(len(opt['source_ids']),12)
        self.assertIn(self.a,opt['source_ids'])
        for eid in newer[-2:]:self.assertIn(eid,opt['source_ids'])
        self.ledger.revoke(self.a)
        self.assertEqual(self.state()['parts'][0]['status'],'unresolved')
        opt=next(o for o in self.ledger.view()['question_integrations'] if o['goal_id']=='a')
        self.assertNotIn(self.a,opt['source_ids'])

    def test_all_integrated_parts_answered_marks_next_step_ready_to_reconsider_or_consolidate(self):
        raw={'parts':[
            {'id':'','question':'触发条件是什么？','conclusion':'温度变化触发开关','coverage':'answer','evidence':[{'episode_id':self.a,'span_id':'s0000'}],'challenges':[]},
            {'id':'','question':'执行的具体阈值是多少？','conclusion':'第二段原文给出的阈值','coverage':'answer','evidence':[{'episode_id':self.b,'span_id':'s0000'}],'challenges':[]}]}
        out,_=self.integrate(raw)
        self.assertEqual(out['status'],'integrated')
        view=self.ledger.view()
        step=next(s for s in view['workspace']['next_steps'] if s['goal_id']=='a')
        self.assertTrue(step['all_integrated_parts_answered'])
        self.assertFalse(step['provisional_reuse_supported'])
        self.assertTrue(step['reconsider_supported'])
        self.assertIsNone(step['remaining_gap'])

    def test_multiple_old_citations_do_not_starve_new_milestone_sources(self):
        old=[self.a,self.b]+[self.read('a',n,useful=True) for n in range(2,8)]
        raw={'parts':[{'id':'','question':f'子问题{n}','conclusion':'已知的局部线索','coverage':'partial',
            'evidence':[{'episode_id':eid,'span_id':'s0000'} for eid in old[n*2:n*2+2]],'challenges':[]}
            for n in range(4)]}
        self.assertEqual(self.integrate(raw)[0]['status'],'integrated')
        milestones=[self.read('a',n,useful=True) for n in range(8,13)]
        latest=[self.read('a',n,useful=False) for n in range(13,15)]
        opt=next(o for o in self.ledger.view()['question_integrations'] if o['goal_id']=='a')
        for eid in milestones+latest:self.assertIn(eid,opt['source_ids'])
        self.assertEqual(len(self.state()['parts']),4)
        self.assertTrue(all(len(p['claims'][0]['evidence'])==2 for p in self.state()['parts']))

    def test_sources_omitted_from_one_window_remain_pending_after_later_integration(self):
        self.integrate()
        added=[self.read('a',n,useful=True) for n in range(2,18)]
        before=next(o for o in self.ledger.view()['question_integrations'] if o['goal_id']=='a')
        omitted=set(added)-set(before['source_ids']);self.assertTrue(omitted)
        self.assertEqual(self.integrate()[0]['status'],'integrated')
        after=next(o for o in self.ledger.view()['question_integrations'] if o['goal_id']=='a')
        self.assertTrue(omitted.issubset(set(after['source_ids'])))
        self.assertEqual(after['unintegrated_source_count'],len(omitted))

    def test_legacy_integration_recovers_its_actual_input_window(self):
        out,_=self.integrate()
        with self.ledger.db:
            r=json.loads(self.ledger.db.execute('select result_json from episodes where id=?',(out['episode_id'],)).fetchone()[0])
            r.pop('input_source_ids');r.pop('receipt');r['receipt']=digest(r)
            self.ledger.db.execute('update episodes set result_json=? where id=?',(json.dumps(r),out['episode_id']))
        self.assertFalse(self.ledger.view()['question_integrations'])
        self.assertEqual(self.state()['parts'][0]['status'],'tentatively_answered')

    def test_withdrawn_integration_reopens_only_its_unreviewed_input_coverage(self):
        self.integrate();new=self.read('a',2,useful=True)
        out,_=self.integrate()
        self.assertFalse(self.ledger.view()['question_integrations'])
        self.ledger.revoke(out['episode_id'])
        opt=next(o for o in self.ledger.view()['question_integrations'] if o['goal_id']=='a')
        self.assertEqual(opt['unintegrated_source_count'],1)
        self.assertIn(new,opt['source_ids'])
        self.assertEqual(self.state()['parts'][0]['status'],'tentatively_answered')

    def test_prior_quote_can_be_reused_only_by_its_existing_part_and_exact_span(self):
        from cognition.question_state import validate
        self.integrate();previous=self.state();part=previous['parts'][0];goal=self.ledger.view()['goals'][0]
        row=self.ledger.db.execute('select result_json from episodes where id=?',(self.a,)).fetchone()
        r=json.loads(row[0]);source={'goal_id':'a','receipt':r['receipt'],'text':r['text'],'source_ref':r['source_ref']}
        raw={'parts':[{'id':part['id'],'question':part['question'],'conclusion':'温度变化触发开关','coverage':'answer',
              'evidence':[{'episode_id':self.a,'span_id':'s0000'}],'challenges':[]}]}
        self.assertEqual(validate(raw,goal,{},previous,prior_sources={self.a:source})[0]['claim']['evidence'][0]['receipt'],r['receipt'])
        raw['parts'][0]['evidence'][0]['span_id']='s0240'
        with self.assertRaisesRegex(ValueError,'unknown_question_source'):validate(raw,goal,{},previous,prior_sources={self.a:source})
        raw['parts'][0]['evidence'][0]['span_id']='s0000'
        other=previous['parts'][1];raw['parts'][0].update(id=other['id'],question=other['question'])
        with self.assertRaisesRegex(ValueError,'unknown_question_source'):validate(raw,goal,{},previous,prior_sources={self.a:source})
        raw['parts'][0].update(id=part['id'],question=part['question'])
        with self.assertRaisesRegex(ValueError,'unknown_question_source'):validate(raw,goal,{},previous,prior_sources={})
        with self.assertRaisesRegex(ValueError,'unknown_question_source'):validate(raw,goal,{},previous,prior_sources={self.a:{**source,'receipt':'changed'}})

    def test_wrong_opaque_id_resolves_only_an_exact_unique_existing_question(self):
        import copy
        from cognition.question_state import validate
        self.integrate();previous=self.state();part=previous['parts'][0];goal=self.ledger.view()['goals'][0]
        r=json.loads(self.ledger.db.execute('select result_json from episodes where id=?',(self.a,)).fetchone()[0])
        sources={self.a:{'goal_id':'a','receipt':r['receipt'],'text':r['text'],'source_ref':r['source_ref']}}
        raw={'parts':[{'id':'mistyped-opaque-id','question':part['question'],'conclusion':'温度变化触发开关','coverage':'answer',
             'evidence':[{'episode_id':self.a,'span_id':'s0000'}],'challenges':[]}]}
        resolved=validate(raw,goal,sources,previous)[0]
        self.assertEqual(resolved['id'],part['id'])
        self.assertEqual(resolved['identity_resolution']['provided_id'],'mistyped-opaque-id')
        changed=copy.deepcopy(raw);changed['parts'][0]['question']='已经改写的问题'
        with self.assertRaisesRegex(ValueError,'identity_changed'):validate(changed,goal,sources,previous)
        crossed=copy.deepcopy(raw);crossed['parts'][0]['id']=previous['parts'][1]['id']
        with self.assertRaisesRegex(ValueError,'identity_changed'):validate(crossed,goal,sources,previous)
        ambiguous=copy.deepcopy(previous);ambiguous['parts'].append({**copy.deepcopy(part),'id':'second-id-for-same-label'})
        with self.assertRaisesRegex(ValueError,'identity_changed'):validate(raw,goal,sources,ambiguous)

    def test_two_useful_search_reads_offer_integrate_while_keeping_search_choice(self):
        from cognition.decision import action_system
        from cognition.inquiry import validate_assessment
        from cognition.method_learning import attach_options
        goal={'id':'webq','question':'蒸馏争议涉及哪些法律、技术与竞争表现？','need':'text','origin':'fixture:webq','status':'active'}
        intent={'kind':'fact','criterion':'说明争议的具体表现','expected_progress':.6}
        s_ep=self.ledger.begin({},{'action':'search','goal':goal})
        s_res={'source_ref':'search:webq','status':'ok','met_need':False,'catalog_found':True,'provider':'wikipedia',
               'query':'Anthropic distillation','text':'','family':'web_search_catalog',
               'discoveries':[{'url':'https://en.wikipedia.org/?curid=1','title':'Anthropic'},
                              {'url':'https://en.wikipedia.org/?curid=2','title':'Distillation attack'}]}
        s_res['receipt']=digest(s_res);self.ledger.finish(s_ep,'observed',s_res)
        for idx,url in enumerate(['https://en.wikipedia.org/?curid=1','https://en.wikipedia.org/?curid=2']):
            ep=self.ledger.begin({},{'action':'read','resource_id':f'web:webq:{idx}','goal':goal,'inquiry':intent})
            text=f'第{idx}份百科正文：记录了服务条款限制、账号封禁与模型提取争议。'*4
            r={'status':'ok','source_ref':url,'text':text,'family':'encyclopedia_article','met_need':True}
            r['receipt']=digest(r);self.ledger.finish(ep,'observed',r)
            a=validate_assessment({'schema_version':2,'progress_type':'partial_answer','criterion_status':'met','novelty':'new',
                'missing':'尚缺具体技术防御细节','prior_episode_ids':[],'verdict':'useful','evidence_id':'s0000',
                'answer':f'第{idx}份部分回答','reason':'提供了部分直接证据'},text,intent,r['receipt'])
            self.ledger.annotate_inquiry(ep,a,r['receipt'])
        view=self.ledger.view()
        step=next(s for s in view['workspace']['next_steps'] if s['goal_id']=='webq')
        self.assertEqual(step['status'],'provisional_progress')
        self.assertEqual(step['suggested_action'],'integrate')
        self.assertEqual(step['suggested_integration_goal_id'],'webq')
        ctx=working_context(view,[],[])
        ctx.update(scope='public',exploration={'can_search':True,'providers':['wikipedia','bing_rss']})
        attach_options(ctx)
        choices=next(o for o in ctx['workspace']['available_alternatives'] if o['goal_id']=='webq')['choices']
        self.assertIn('integrate',{c['action'] for c in choices})
        self.assertIn('search',{c['action'] for c in choices})
        self.assertIn('question_integrations 表示有实际阅读尚未整合',action_system(ctx))
        self.integrate()
        read_ids=[e['id'] for e in reversed(self.ledger.view()['episodes']) if (e['decision'].get('goal') or {}).get('id')=='webq' and e['decision'].get('action')=='read']
        from cognition.question_state import goal_version
        r0=json.loads(self.ledger.db.execute('select result_json from episodes where id=?',(read_ids[0],)).fetchone()[0])
        ie=self.ledger.begin({},{'action':'integrate','integration_goal_id':'webq','goal':None})
        ir={'goal_id':'webq','goal_version':goal_version(goal),'input_source_ids':read_ids,'parts':[
            {'id':'p1','question':'是否涉及商业竞争担忧？','claim':{'id':'c1','answer':'涉及竞争对手争议','coverage':'answer',
             'evidence':[{'episode_id':read_ids[0],'span_id':'s0000','quote':'记录了服务条款限制','receipt':r0['receipt']}],'challenges':[]}},
            {'id':'p2','question':'具体技术防御是什么？','claim':None}]}
        ir['receipt']=digest(ir);self.ledger.finish(ie,'integrated',ir)
        ep3=self.ledger.begin({},{'action':'read','resource_id':'web:webq:2','goal':goal,'inquiry':intent})
        text3='第2份百科正文：重复先前指控但无技术防御细节。'*4
        r3={'status':'ok','source_ref':'https://en.wikipedia.org/?curid=3','text':text3,'family':'encyclopedia_article','met_need':True}
        r3['receipt']=digest(r3);self.ledger.finish(ep3,'observed',r3)
        a3=validate_assessment({'schema_version':2,'progress_type':'none','criterion_status':'unmet','novelty':'known',
            'missing':'尚缺具体技术防御细节','prior_episode_ids':[read_ids[0]],'verdict':'not_useful','evidence_id':'s0000',
            'answer':'','reason':'重复已知内容'},text3,intent,r3['receipt'])
        self.ledger.annotate_inquiry(ep3,a3,r3['receipt'])
        v2=self.ledger.view()
        self.assertEqual(list(v2['question_states'])[0],'webq')
        self.assertFalse(v2['question_states']['webq']['has_unintegrated_sources'])
        step2=next(s for s in v2['workspace']['next_steps'] if s['goal_id']=='webq')
        self.assertFalse(step2['has_unintegrated_sources'])
        cons=next(c for c in v2['consolidations'] if c['topic']=='web:webq')
        self.assertIn('阶段认识：「第0份部分回答；第1份部分回答」',cons['summary'])
        self.assertIn('web:webq:1',cons['summary'])
        self.ledger.revoke_inquiry(read_ids[1],'撤回第二份评估')
        cons_after=next(c for c in self.ledger.view()['consolidations'] if c['topic']=='web:webq')
        self.assertIn('阶段认识：「第0份部分回答」',cons_after['summary'])
        self.assertNotIn('第1份部分回答',cons_after['summary'])
        with self.ledger.db:
            self.ledger.db.execute("update goals set status='completed' where id='webq'")
        from cognition import idle_goal_choice
        from cognition.exploration import source_messages
        from cognition.self_model import retrieve_related_knowledge
        v3=self.ledger.view()
        self.assertFalse(retrieve_related_knowledge(v3,['福贵在龙二被枪毙后为什么发抖？']))
        recalled=retrieve_related_knowledge(v3,['模型提取争议与服务条款限制的具体表现是什么？'])
        self.assertTrue(any(item['goal_id']=='webq' and item['answer']=='涉及竞争对手争议' for item in recalled))
        q_item={'id':'q_new','source_episode_id':'obs1','source_receipt':'r1','evidence_id':'s0000',
                'quote':'讨论模型提取争议与服务条款限制','question':'模型提取争议与服务条款限制还涉及哪些技术细节？','status':'open','reason':'想弄清技术细节'}
        ctx3=working_context({**v3,'open_questions':[q_item],'inspection_sources':[{'id':'obs1','receipt':'r1'}]},[],[])
        ctx3.update(exploration={'can_search':True,'providers':['wikipedia']})
        self.assertTrue(any(item['goal_id']=='webq' for item in ctx3['related_prior_knowledge']))
        ig_msgs=idle_goal_choice.messages(ctx3,[q_item])
        ig_payload=json.loads(ig_msgs[1]['content'])
        self.assertTrue(any(item['goal_id']=='webq' for item in ig_payload['related_prior_knowledge']))
        sm_msgs=source_messages(ctx3,{'id':'s1','goal':{'id':'g_new','question':q_item['question']},
                                      'query':'model distillation','provider':'wikipedia','candidates':[]})
        sm_payload=json.loads(sm_msgs[1]['content'])
        self.assertTrue(any(item['goal_id']=='webq' for item in sm_payload['related_prior_knowledge']))
        self.ledger.revoke(read_ids[0])
        self.assertFalse(any(item['goal_id']=='webq' for item in retrieve_related_knowledge(self.ledger.view(),[q_item['question']])))


if __name__=='__main__':unittest.main()
