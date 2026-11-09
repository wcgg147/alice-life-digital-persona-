import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition.decision import validate,working_context
from cognition.inquiry import validate_assessment
from cognition.loop import CognitiveLoop
from cognition.state import Ledger,digest


class MotivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path)
    def tearDown(self):
        self.ledger.close();self.tmp.cleanup()
    def read(self,gid,n,*,useful=True,answer=None,source='experiment',external=False,legacy=False,family='fixture',detail=False,uncertain=False):
        goal={'id':gid,'question':gid+'的问题怎么解释？','need':'text','origin':'fixture:'+gid,'status':'active'}
        intent={'kind':'mechanism','criterion':'需要有关于这个问题的具体解释','expected_progress':.7}
        d={'action':'read','resource_id':f'book:{gid}:{n}','goal':goal,'inquiry':None if legacy else intent}
        ep=self.ledger.begin({'opportunity':{'source':source,'external_pending':external}},d)
        text=f'{gid} 的第 {n} 份实际观测：温度变化触发开关，电机随后收缩绳索，窗户关闭。'*4
        r={'status':'ok','source_ref':f'fixture:{gid}:{n}','text':text,'family':family,'met_need':True}
        r['receipt']=digest(r);self.ledger.finish(ep,'observed',r)
        if not legacy:
            raw={'verdict':'useful' if useful else 'not_useful','evidence_id':'s0000',
                 'answer':answer or f'{gid} 的第 {n} 项解释' if useful else '', 'reason':'测试可撤销的材料评估'}
            if detail:
                raw.update(schema_version=2,progress_type='new_answer' if useful else 'none',
                           criterion_status='met' if useful else 'unmet',novelty='new',missing='',prior_episode_ids=[])
            if uncertain:
                raw.update(verdict='uncertain',progress_type='uncertain',criterion_status='uncertain',novelty='uncertain')
            a=validate_assessment(raw,text,intent,r['receipt'])
            self.ledger.annotate_inquiry(ep,a,r['receipt'])
        return ep
    def rest(self,source='runtime'):
        ep=self.ledger.begin({'opportunity':{'source':source,'external_pending':False}},{'action':'rest'})
        self.ledger.finish(ep,'rest',{})
    def profile(self,gid):return next(p for p in self.ledger.view()['motivation']['goals'] if p['goal_id']==gid)

    def candidates(self):
        return [{'id':'candidate:'+f,'source_ref':'new:'+f,'family':f,'title':'新的可读来源','affordances':['text']}
                for f in ['stalled','productive','unknown']]

    def test_no_goal_opportunities_use_progress_not_completed_goal_relevance(self):
        ids=[]
        for n in range(3):
            ids.append(self.read('p',n,family='productive',detail=True))
            ids.append(self.read('s',n,family='stalled',detail=True,useful=False))
        with self.ledger.db:self.ledger.db.execute("update goals set status='completed'")
        for _ in range(10):self.rest('experiment')
        state=self.ledger.view();ctx=working_context(state,self.candidates(),[])
        self.assertIsNone(ctx['focus_goal_id'])
        self.assertEqual(ctx['resources'][0]['family'],'productive')
        self.assertNotIn('candidate_opportunities',state['motivation'])
        profiles={p['family']:p for p in ctx['motivation']['candidate_opportunities']}
        self.assertGreater(profiles['productive']['opportunity_estimate'],profiles['unknown']['opportunity_estimate'])
        self.assertGreater(profiles['unknown']['opportunity_estimate'],profiles['stalled']['opportunity_estimate'])
        self.assertEqual(working_context(state,self.candidates(),[],False)['resources'][0]['family'],'stalled')
        self.assertFalse(ctx['motivation']['interest_cues'])
        self.assertEqual(validate({'action':'rest','resource_id':'','reason':'可探索但选择休息','goal':None,
                                  'predict_success':False,'next_check_seconds':600},ctx)['action'],'rest')
        for eid in ids:self.ledger.revoke_inquiry(eid,'撤销所有解释')
        restored=working_context(self.ledger.view(),self.candidates(),[])
        self.assertEqual(restored['resources'][0]['family'],'stalled')
        self.assertTrue(all(p['unique_attempts']==0 for p in restored['motivation']['candidate_opportunities']))

    def test_noise_repetition_and_legacy_do_not_create_goal_independent_learning(self):
        for n in range(4):self.read('noise',n,family='noise',detail=True,uncertain=True)
        for _ in range(4):self.read('repeat',0,family='repeat',detail=True,answer='同一个回答')
        self.read('repeat',1,family='repeat',detail=True,answer='同一个回答')
        self.read('legacy',0,family='legacy')
        f=self.ledger.view()['source_feedback']
        self.assertLess(f['noise']['opportunity_estimate'],.25)
        self.assertEqual(f['noise']['measured_feedback'],0)
        self.assertEqual(f['repeat']['unique_attempts'],2)
        self.assertEqual(f['repeat']['new_progress'],1)
        self.assertEqual(f['legacy']['unique_attempts'],0)
        self.assertEqual(f['legacy']['legacy_unmeasured'],1)

    def test_source_feedback_is_scoped_and_candidate_projection_is_bounded(self):
        old=self.read('private',0,family='productive',detail=True)
        with self.ledger.db:
            self.ledger.db.execute("update episodes set scope='private:11',actor='11' where id=?",(old,))
            self.ledger.db.execute("update goals set scope='private:11',actor='11'")
        self.assertFalse(self.ledger.view()['source_feedback'])
        self.assertFalse(self.ledger.view('private:22','22')['source_feedback'])
        own=self.ledger.view('private:11','11')
        ctx=working_context(own,self.candidates(),[])
        self.assertNotIn('seen_source_refs',str(ctx['motivation']))
        self.assertEqual(len(ctx['motivation']['candidate_opportunities']),3)
        self.assertEqual(own['source_feedback']['productive']['new_progress'],1)
        self.ledger.revoke(old)
        self.assertFalse(self.ledger.view('private:11','11')['source_feedback'])

    def test_source_identity_is_remembered_but_does_not_forbid_updated_content(self):
        self.read('p',0,family='productive',detail=True)
        for _ in range(10):self.rest('experiment')
        resources=self.candidates();resources[1]['source_ref']='fixture:p:0'
        ctx=working_context(self.ledger.view(),resources,[])
        p=next(p for p in ctx['motivation']['candidate_opportunities'] if p['family']=='productive')
        self.assertTrue(p['source_identity_seen'])
        self.assertEqual({r['id'] for r in ctx['resources']},{r['id'] for r in resources})

    def test_latest_surviving_feedback_corrects_uncertainty_without_duplicate_samples(self):
        self.read('p',0,family='productive',detail=True,uncertain=True)
        correction=self.read('p',0,family='productive',detail=True)
        f=self.ledger.view()['source_feedback']['productive']
        self.assertEqual((f['unique_attempts'],f['measured_feedback'],f['new_progress']),(1,1,1))
        self.assertEqual(f['supporting_episodes'],[correction])
        self.ledger.revoke_inquiry(correction,'撤销后来判断，早先的不确定评估仍保留')
        f=self.ledger.view()['source_feedback']['productive']
        self.assertEqual((f['unique_attempts'],f['measured_feedback'],f['new_progress']),(1,0,0))

    def test_productive_goal_changes_focus_while_ablation_and_withdrawal_restore_recency(self):
        ids=[]
        for n in range(3):
            ids.append(self.read('productive',n));ids.append(self.read('recent_stalled',n,useful=False))
        state=self.ledger.view();ctx=working_context(state,[],[])
        self.assertEqual(ctx['focus_goal_id'],'productive')
        self.assertEqual(working_context(state,[],[],False)['focus_goal_id'],'recent_stalled')
        for eid in ids:self.ledger.revoke_inquiry(eid,'撤销实验判断，保留原文')
        self.assertEqual(working_context(self.ledger.view(),[],[])['focus_goal_id'],'recent_stalled')

    def test_repeated_answer_and_repeated_source_do_not_manufacture_progress(self):
        self.read('a',0,answer='同一个解释')
        self.read('a',1,answer='同一个解释')
        self.read('a',1,answer='同一个解释')
        self.read('a',1,answer='同一个解释')
        p=self.profile('a');self.assertEqual(p['total_new_progress'],1);self.assertTrue(p['stalled'])
        self.assertTrue(self.ledger.view()['motivation']['rest_or_change_method_supported'])

    def test_legacy_transport_success_is_unmeasured_and_not_interest(self):
        for n in range(6):self.read('a',n,legacy=True)
        m=self.ledger.view()['motivation'];p=m['goals'][0]
        self.assertEqual(p['legacy_unmeasured'],6);self.assertEqual(p['window_attempts'],0)
        self.assertEqual(p['opportunity_estimate'],.25);self.assertFalse(m['interest_cues'])

    def test_only_runtime_returns_with_progress_make_provisional_cues(self):
        for n in range(3):self.read('manual',n,source='manual');self.rest('manual')
        for n in range(3):self.read('natural',n,source='runtime');self.rest()
        cues=self.ledger.view()['motivation']['interest_cues']
        self.assertEqual([c['goal_id'] for c in cues],['natural'])
        self.assertEqual(cues[0]['status'],'provisional_attention_cue')
        self.assertEqual(self.profile('natural')['runtime_returns'],2)

    def test_external_events_and_consecutive_activity_do_not_prove_interest_returns(self):
        for n in range(4):self.read('event',n,source='runtime',external=True);self.rest()
        for n in range(4):self.read('streak',n,source='runtime')
        self.assertFalse(self.ledger.view()['motivation']['interest_cues'])

    def test_revoked_support_removes_cue_and_reopen_preserves_remaining_evidence(self):
        ids=[]
        for n in range(3):ids.append(self.read('a',n,source='runtime'));self.rest()
        self.assertTrue(self.ledger.view()['motivation']['interest_cues'])
        for eid in ids:self.ledger.revoke_inquiry(eid,'证据解释失效')
        self.ledger.close();self.ledger=Ledger(self.path)
        self.assertFalse(self.ledger.view()['motivation']['interest_cues'])
        self.assertEqual(self.profile('a')['total_new_progress'],0)

    def test_low_ranked_goal_and_rest_remain_admissible(self):
        for n in range(3):self.read('b',n);self.read('a',n,useful=False)
        c=working_context(self.ledger.view(),[],[])
        d={'action':'rest','resource_id':'','reason':'仍关心这个难问题，暂时等待新材料','goal':{'id':'a','status':'paused'},
           'predict_success':False,'next_check_seconds':600}
        self.assertEqual(validate(d,c)['goal']['id'],'a')

    def test_tick_records_manual_provenance_without_claiming_runtime_autonomy(self):
        class Model:
            async def complete(inner,messages):return {'action':'rest','resource_id':'','reason':'没有必要行动','goal':None,'predict_success':False,'next_check_seconds':60}
        class Env:
            async def observe(inner):return [],[]
        out=asyncio.run(CognitiveLoop(self.ledger,Model(),Env()).tick())
        before=json.loads(self.ledger.db.execute('SELECT before_json FROM episodes WHERE id=?',(out['episode_id'],)).fetchone()[0])
        self.assertEqual(before['opportunity']['source'],'manual')

    def test_goal_progress_is_scoped_and_old_measurements_survive_working_window(self):
        for n in range(3):self.read('a',n)
        for _ in range(10):self.rest('manual')
        self.assertEqual(self.profile('a')['total_new_progress'],3)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE episodes SET scope='private:11',actor='11' WHERE decision_json LIKE '%\"action\": \"read\"%'")
            self.ledger.db.execute("UPDATE goals SET scope='private:11',actor='11'")
        self.assertFalse(self.ledger.view()['motivation']['goals'])
        self.assertFalse(self.ledger.view('private:22','22')['motivation']['goals'])
        self.assertEqual(self.ledger.view('private:11','11')['motivation']['goals'][0]['total_new_progress'],3)

    def test_old_observed_memory_can_ground_a_new_goal_after_working_window_expires(self):
        old=self.read('old',0)
        for _ in range(10):self.rest('experiment')
        with self.ledger.db:self.ledger.db.execute("update goals set status='completed'")
        ctx=working_context(self.ledger.view(),[],[])
        self.assertNotIn(old,{e['id'] for e in ctx['episodes']})
        raw={'action':'search','resource_id':'','reason':'沿着旧经历提出另一个问题',
             'goal':{'id':'','question':'这段已有观察中的开关执行链还能怎样检验？','need':'text','origin':old,'status':'active'},
             'predict_success':True,'next_check_seconds':600,'search_query':'温度 开关 执行链 验证','search_provider':'bing_rss'}
        ctx.update(scope='public',exploration={'can_search':True,'providers':['bing_rss']})
        self.assertEqual(validate(raw,ctx)['goal']['origin'],old)
        self.assertEqual({g['status'] for g in ctx['goals']},{'completed'})
        self.assertEqual(validate({'action':'rest','resource_id':'','reason':'暂时不行动','goal':None,
                                   'predict_success':False,'next_check_seconds':1800},ctx)['action'],'rest')
        self.ledger.revoke(old)
        gone=working_context(self.ledger.view(),[],[]);gone.update(scope='public',exploration={'can_search':True,'providers':['bing_rss']})
        with self.assertRaisesRegex(ValueError,'unsourced_goal'):validate(raw,gone)

    def test_memory_goal_origins_remain_private_and_ablation_does_not_expose_them(self):
        old=self.read('private',0)
        with self.ledger.db:
            self.ledger.db.execute("update episodes set scope='private:11',actor='11' where id=?",(old,))
            self.ledger.db.execute("update goals set scope='private:11',actor='11'")
        for _ in range(10):self.rest('experiment')
        self.assertFalse(self.ledger.view().get('memory_goal_origins'))
        self.assertFalse(self.ledger.view('private:22','22').get('memory_goal_origins'))
        view=self.ledger.view('private:11','11')
        self.assertIn(old,{s['id'] for s in view['memory_goal_origins']})
        self.assertFalse(working_context(view,[],[],False).get('memory_goal_origins'))

    def test_revoked_memory_during_decision_does_not_create_goal_or_call_search(self):
        old=self.read('old',0)
        for _ in range(10):self.rest('experiment')
        ledger=self.ledger
        class Model:
            async def complete(inner,messages):
                ledger.revoke(old)
                return {'action':'search','resource_id':'','reason':'根据旧经历探索',
                    'goal':{'id':'','question':'另一个有来源的新问题是什么？','need':'text','origin':old,'status':'active'},
                    'predict_success':True,'next_check_seconds':600,'search_query':'一个新问题','search_provider':'bing_rss'}
        class Env:
            calls=0
            async def observe(inner):return [],[]
            def exploration_capabilities(inner):return {'can_search':True,'providers':['bing_rss']}
            async def search(inner,*a,**k):inner.calls+=1;raise AssertionError('revoked origin must not act')
        env=Env();out=asyncio.run(CognitiveLoop(ledger,Model(),env).tick(opportunity_source='experiment'))
        self.assertEqual(out['status'],'decision_error');self.assertEqual(env.calls,0)
        result=json.loads(ledger.db.execute('select result_json from episodes where id=?',(out['episode_id'],)).fetchone()[0])
        self.assertEqual(result['validation_code'],'memory_goal_origin_changed')
        self.assertFalse(any(g['question']=='另一个有来源的新问题是什么？' for g in ledger.view()['goals']))


if __name__=='__main__':unittest.main()
