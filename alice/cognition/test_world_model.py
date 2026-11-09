import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition.decision import validate, working_context
from cognition.exploration import normalize_result
from cognition.loop import CognitiveLoop
from cognition.state import Ledger, digest
from cognition.world_model import attach


class Model:
    def __init__(self, responses):
        self.responses=iter(responses);self.calls=0;self.inputs=[]
    async def complete(self, messages):
        self.calls+=1;self.inputs.append(messages)
        value=next(self.responses)
        return value(messages) if callable(value) else value


class Environment:
    def __init__(self):
        self.mode='support';self.calls=[];self.hook=None;self.reverse_order=False
    async def observe(self):
        return [],[]
    def exploration_capabilities(self):
        return {'can_search':True,'providers':['bing_rss','crossref'],'provider':'bing_rss'}
    async def search(self, query, goal, *, provider=None, fresh=False):
        self.calls.append((query,provider,fresh))
        if self.hook:self.hook()
        if self.mode=='error' and query=='variant':
            return normalize_result({'status':'error','results':[],'error_code':'network_failure'},query,provider)
        relevant=(query=='variant') != (self.mode=='counter')
        if self.mode=='provider':relevant=provider=='bing_rss'
        title='温度使金属片收缩并触发关窗' if relevant else '面包店的营业时间与甜点菜单'
        rows=[{'url':'https://example.org/'+('answer' if relevant else 'noise'),'title':title,'snippet':title},
              {'url':'https://example.org/'+('answer2' if relevant else 'noise2'),'title':title+'的另一份记录','snippet':title}]
        if self.reverse_order:rows.reverse()
        return normalize_result({'status':'ok','results':rows},query,provider)


class WorldModelTests(unittest.TestCase):
    def test_scoring_template_covers_all_ids_without_unblinding_or_priming_success(self):
        from cognition.world_model import scoring_messages,judge
        p={'question':'一个公开问题','candidates':[{'id':'a','title':'一个标题','snippet':'一段内容'},
            {'id':'b','title':'另一标题','snippet':'另一内容'}],
           'control_query':'HIDDEN_ORIGINAL','provider':'HIDDEN_PROVIDER','expected_winner':'HIDDEN_WINNER'}
        m=scoring_messages(p);visible=json.loads(m[1]['content'])
        self.assertEqual(set(visible),{'question','candidates'})
        self.assertNotIn('HIDDEN_',json.dumps(m))
        template=json.loads(m[0]['content'].rsplit('\n',1)[1])
        self.assertEqual([r['id'] for r in template['scores']],['a','b'])
        self.assertTrue(all(r['relevant'] is None for r in template['scores']))
        with self.assertRaisesRegex(ValueError,'incomplete_candidate_scores'):
            judge({'scores':[{'id':'a','relevant':False,'reason':'无关'}]},visible,{'control':['a'],'treatment':['b']})

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path)
        with self.ledger.db:self.ledger.db.execute('INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?)',
                                                ('alice','g','温度如何触发窗户关闭？','text','fixture:question','active',1,'public',''))
        goal=self.ledger.view()['goals'][0]
        self.anchor=self.ledger.begin({}, {'action':'search','goal':goal,'search_query':'original'})
        r=normalize_result({'status':'ok','results':[{'url':'https://example.org/noise','title':'面包菜单','snippet':'无关内容'}]},'original','bing_rss')
        r.update(source_ref='search:fixture',text='',met_need=False,family='web_search_catalog');r['receipt']=digest(r)
        self.ledger.finish(self.anchor,'observed',r)
        self.env=Environment()

    def tearDown(self):
        self.ledger.close();self.tmp.cleanup()

    def propose(self, **overrides):
        h={'source_episode_id':self.anchor,'claim':'替代查询可以取得更多相关候选','factor':'query_variant',
           'alternative_query':'variant','alternative_provider':None,'confidence':.99,**overrides}
        d={'action':'hypothesize','resource_id':'','goal':{'id':'g','status':'active'},'reason':'先记录可反证假设',
           'predict_success':True,'next_check_seconds':60,'hypothesis':h}
        return asyncio.run(CognitiveLoop(self.ledger,Model([d]),self.env).tick())

    def trial(self, hid, *, cap=100, scorer=None):
        d={'action':'experiment','resource_id':'','goal':{'id':'g','status':'active'},'reason':'只改变查询版本验证假设',
           'predict_success':True,'next_check_seconds':60,'hypothesis_id':hid}
        def score(messages):
            payload=json.loads(messages[1]['content'])
            self.assertEqual(set(payload),{'question','candidates'})
            rows=[{'id':c['id'],'relevant':'温度' in c['title'],'reason':'按候选可见内容与问题比较'} for c in payload['candidates']]
            return {'scores':rows}
        model=Model([d,scorer or score])
        return asyncio.run(CognitiveLoop(self.ledger,model,self.env,daily_cap=cap).tick()),model

    def test_proposed_confidence_is_not_experimental_evidence(self):
        h=self.propose()['hypothesis_id'];belief=self.ledger.view()['experimental_world_model'][0]
        self.assertEqual(belief['id'],h);self.assertEqual(belief['confidence'],.5)
        self.assertEqual(belief['status'],'untested');self.assertEqual(belief['plan']['proposed_confidence'],.99)

    def test_single_factor_blind_trial_updates_bounded_belief(self):
        h=self.propose()['hypothesis_id'];out,model=self.trial(h)
        self.assertTrue(out['assessment_applied']);self.assertEqual(model.calls,2)
        self.assertEqual({p for _,p,_ in self.env.calls},{'bing_rss'})
        self.assertTrue(all(fresh for _,_,fresh in self.env.calls));self.assertEqual(len(self.env.calls),2)
        belief=self.ledger.view()['experimental_world_model'][0]
        self.assertEqual(belief['supports'],1);self.assertEqual(belief['confidence'],.6667)
        self.assertEqual(belief['preferred']['query'],'variant')
        self.assertFalse(self.ledger.view()['inquiry_model']);self.assertFalse(self.ledger.view()['strategies'])

    def test_duplicate_snapshots_and_result_reordering_cannot_inflate_confidence(self):
        h=self.propose()['hypothesis_id'];self.trial(h)
        version=self.ledger.view()['experimental_world_model'][0]['version']
        self.env.reverse_order=True;self.trial(h)
        belief=self.ledger.view()['experimental_world_model'][0]
        self.assertEqual(belief['supports'],1);self.assertEqual(belief['unique_snapshots'],1)
        self.assertEqual(version,belief['version'])

    def test_same_snapshot_score_disagreement_is_not_fresh_counterevidence(self):
        h=self.propose()['hypothesis_id'];self.trial(h)
        def reversed_score(messages):
            p=json.loads(messages[1]['content'])
            return {'scores':[{'id':c['id'],'relevant':'温度' not in c['title'],'reason':'同一资料上出现评估分歧'} for c in p['candidates']]}
        self.trial(h,scorer=reversed_score)
        b=self.ledger.view()['experimental_world_model'][0]
        self.assertEqual((b['supports'],b['counters']),(0,0));self.assertEqual(b['confidence'],.5)
        self.assertEqual(b['assessment_disagreements'],1)

    def test_counterevidence_reduces_confidence(self):
        h=self.propose()['hypothesis_id'];self.trial(h);self.env.mode='counter';self.trial(h)
        belief=self.ledger.view()['experimental_world_model'][0]
        self.assertEqual((belief['supports'],belief['counters']),(1,1));self.assertEqual(belief['confidence'],.5)
        self.assertIsNone(belief['preferred'])

    def test_two_changed_factors_are_rejected_without_network(self):
        out=self.propose(alternative_provider='crossref')
        self.assertEqual(out['status'],'decision_error');self.assertFalse(self.env.calls)
        self.assertFalse(self.ledger.view()['experimental_world_model'])

    def test_query_must_stay_fixed_when_provider_is_the_factor(self):
        out=self.propose(factor='provider',alternative_query=None,alternative_provider='crossref')
        self.trial(out['hypothesis_id'])
        self.assertEqual({q for q,_,_ in self.env.calls},{'original'})
        self.assertEqual({p for _,p,_ in self.env.calls},{'bing_rss','crossref'})

    def test_prediction_can_favor_control_and_belief_use_requires_tested_exact_variant(self):
        h=self.propose(factor='provider',alternative_query=None,alternative_provider='crossref',
                       prediction='control_more_relevant')['hypothesis_id']
        d={'action':'search','resource_id':'','goal':{'id':'g','status':'active'},'reason':'根据实验选择',
           'predict_success':True,'next_check_seconds':60,'search_query':'original','search_provider':'bing_rss','belief_id':h}
        def context():
            c=working_context(self.ledger.view(),[],[]);c.update(scope='public',exploration=self.env.exploration_capabilities());return c
        with self.assertRaisesRegex(ValueError,'belief_not_applicable'):validate(d,context())
        self.env.mode='provider';self.trial(h)
        b=self.ledger.view()['experimental_world_model'][0]
        self.assertEqual(b['supports'],1);self.assertEqual(b['confidence'],.6667)
        self.assertEqual(b['observed_relevance_rates'],{'control':1.0,'treatment':0.0})
        self.assertEqual(validate(d,context())['belief_version'],b['version'])
        with self.assertRaisesRegex(ValueError,'belief_not_applicable'):
            validate({**d,'search_provider':'crossref'},context())

    def test_cancelled_scoring_keeps_completed_raw_comparison(self):
        h=self.propose()['hypothesis_id']
        def cancel(messages):raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):self.trial(h,scorer=cancel)
        ep=self.ledger.view()['episodes'][0]
        self.assertEqual(ep['status'],'observed');self.assertEqual(len(ep['result']['arms']),2)
        self.assertEqual(ep['result']['experiment_assessment']['status'],'unassessed')
        self.assertEqual(self.ledger.view()['experimental_world_model'][0]['confidence'],.5)

    def test_withdrawal_during_scoring_does_not_reapply_judgment(self):
        h=self.propose()['hypothesis_id']
        def revoke(messages):
            eid=self.ledger.db.execute("SELECT id FROM episodes WHERE result_json LIKE '%experiment_assessment%' ORDER BY started DESC LIMIT 1").fetchone()[0]
            self.ledger.revoke_experiment(eid,'评估途中撤销')
            p=json.loads(messages[1]['content'])
            return {'scores':[{'id':c['id'],'relevant':'温度' in c['title'],'reason':'稍后返回的旧评估'} for c in p['candidates']]}
        out,_=self.trial(h,scorer=revoke)
        self.assertFalse(out['assessment_applied'])
        self.assertEqual(self.ledger.view()['experimental_world_model'][0]['confidence'],.5)

    def test_unknown_scores_or_network_errors_cannot_support_a_hypothesis(self):
        h=self.propose()['hypothesis_id']
        def uncertain(messages):
            p=json.loads(messages[1]['content']);return {'scores':[{'id':c['id'],'relevant':None,'reason':'无法判断'} for c in p['candidates']]}
        self.trial(h,scorer=uncertain);self.env.mode='error';out,model=self.trial(h)
        self.assertEqual(model.calls,1);self.assertEqual(out['assessment']['status'],'inconclusive')
        self.assertEqual(self.ledger.view()['experimental_world_model'][0]['confidence'],.5)

    def test_judgment_withdrawal_retains_raw_pairs_and_blocks_late_results(self):
        h=self.propose()['hypothesis_id'];out,_=self.trial(h);eid=out['episode_id']
        r=json.loads(self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?',(eid,)).fetchone()[0])
        self.ledger.revoke_experiment(eid,'相关性评估错误')
        self.assertFalse(attach(self.ledger,eid,out['assessment'],r['receipt'],h))
        after=json.loads(self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?',(eid,)).fetchone()[0])
        self.assertEqual(r['arms'],after['arms']);self.assertEqual(self.ledger.view()['experimental_world_model'][0]['confidence'],.5)
        projected=working_context(self.ledger.view(),[],[])
        ep=next(e for e in projected['episodes'] if e['id']==eid)
        self.assertNotIn('withdrawn_experiment_assessment',ep['result'])

    def test_revoking_anchor_invalidates_hypothesis_and_trials(self):
        h=self.propose()['hypothesis_id'];out,_=self.trial(h);self.ledger.revoke(self.anchor)
        self.assertFalse(self.ledger.view()['experimental_world_model'])
        self.assertTrue(self.ledger.db.execute('SELECT revoked FROM episodes WHERE id=?',(out['episode_id'],)).fetchone()[0])

    def test_budget_and_interruption_preserve_observations_without_false_learning(self):
        h=self.propose()['hypothesis_id']
        spent=self.ledger.db.execute('SELECT sum(calls) FROM call_budget').fetchone()[0]
        out,model=self.trial(h,cap=spent+1)
        self.assertEqual(model.calls,1);self.assertEqual(out['assessment']['status'],'unassessed')
        self.assertEqual(self.ledger.view()['experimental_world_model'][0]['confidence'],.5)
        row=json.loads(self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?',(out['episode_id'],)).fetchone()[0])
        self.assertEqual(set(row['arms']),{'control','treatment'})

    def test_source_revocation_mid_trial_stops_the_other_arm(self):
        h=self.propose()['hypothesis_id'];self.env.hook=lambda:self.ledger.revoke(self.anchor)
        out,_=self.trial(h)
        self.assertEqual(out['status'],'experiment_error');self.assertEqual(len(self.env.calls),1)
        self.assertFalse(self.ledger.view()['experimental_world_model'])

    def test_private_scope_cannot_propose_public_experiments(self):
        state=self.ledger.view();ctx=working_context(state,[],[]);ctx.update(scope='private:11',exploration=self.env.exploration_capabilities())
        raw={'action':'hypothesize','resource_id':'','goal':{'id':'g','status':'active'},'reason':'猜测',
             'predict_success':True,'next_check_seconds':60,'hypothesis':{}}
        with self.assertRaisesRegex(ValueError,'public_search_scope'):validate(raw,ctx)

    def test_workspace_surfaces_hypothesize_experiment_and_preferred_belief(self):
        from cognition.method_learning import attach_options
        h=self.propose()['hypothesis_id']
        view=self.ledger.view()
        self.assertTrue(any(t.get('kind')=='untested_search_hypothesis' and h in t.get('hypothesis_ids',[])
                            for t in view['workspace']['tensions']))
        step=next(s for s in view['workspace']['next_steps'] if s['goal_id']=='g')
        self.assertNotEqual(step['suggested_action'],'experiment')
        self.assertEqual(step['optional_method_trials']['experiment_hypothesis_id'],h)
        ctx=working_context(view,[],[])
        ctx.update(scope='public',exploration=self.env.exploration_capabilities())
        attach_options(ctx)
        choices=ctx['workspace']['available_alternatives'][0]['choices']
        self.assertTrue(any(c['action']=='experiment' and c['hypothesis_id']==h for c in choices))
        self.assertNotEqual(choices[0]['action'],'experiment')
        self.trial(h)
        view2=self.ledger.view()
        step2=next(s for s in view2['workspace']['next_steps'] if s['goal_id']=='g')
        self.assertEqual(step2['preferred_search_belief']['belief_id'],h)
        self.assertEqual(step2['preferred_search_belief']['query'],'variant')


if __name__=='__main__':unittest.main()
