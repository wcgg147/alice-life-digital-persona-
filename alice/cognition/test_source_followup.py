import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition.exploration import catalog,normalize_result
from cognition.loop import CognitiveLoop
from cognition.state import Ledger,digest

URL='https://example.org/threshold'
RID=f'web:{digest("g")[:16]}:{digest(URL)[:16]}'
SEARCH={'action':'search','goal':{'id':'g','status':'active'},'resource_id':'','reason':'找装置说明',
        'search_provider':'bing_rss','search_query':'window temperature threshold','predict_success':True,'next_check_seconds':600}
REST={'action':'rest','goal':None,'resource_id':'','reason':'这些候选现在不值得读','predict_success':False,'inquiry':None,'next_check_seconds':600}
READ={'action':'read','goal':{'id':'g','status':'active'},'resource_id':RID,'reason':'取得有明确阈值的实际说明',
      'predict_success':True,'next_check_seconds':600,'inquiry':{'kind':'fact','criterion':'正文直接说明温度阈值','expected_progress':.6}}
ASSESS={'verdict':'useful','evidence_id':'s0000','answer':'温控阈值是17摄氏度。','reason':'实际说明给出了阈值',
        'schema_version':2,'progress_type':'new_answer','criterion_status':'met','novelty':'new','missing':'','prior_episode_ids':[]}


class Model:
    requires_inquiry=True
    def __init__(self,rows,hook=None):self.rows=iter(rows);self.inputs=[];self.hook=hook
    async def complete(self,messages):
        self.inputs.append(messages)
        if self.hook:self.hook()
        value=next(self.rows)
        if isinstance(value,Exception):raise value
        return value


class Env:
    def __init__(self,ledger):
        self.l=ledger;self.cfg={'cognition_source_followup_enabled':True};self.searches=0;self.reads=0
    async def observe(self):return catalog(self.l),[]
    def exploration_capabilities(self):return {'can_search':True,'providers':['bing_rss'],'provider':'bing_rss'}
    async def search(self,query,goal,**kwargs):
        self.searches+=1
        return normalize_result({'status':'ok','results':[{'url':URL,'title':'温控装置说明','snippet':'温度阈值的原始记录'}]},query,'bing_rss')
    async def read(self,resource):
        self.reads+=1
        return {'status':'ok','source_ref':resource['source_ref'],'text':'这个温控关窗装置的触发阈值是17摄氏度。'+('说明中的其他工作条件需按实际使用场景核对。'*8)}


class SourceFollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.l=Ledger(self.path);self.env=Env(self.l)
        e=self.l.begin({}, {'action':'rest','goal':{'id':'g','question':'这个温控装置的触发阈值是多少？','need':'text','origin':'fixture:observation','status':'active'}})
        self.l.finish(e,'rest',{},0)
    def tearDown(self):self.l.close();self.tmp.cleanup()
    def tick(self,model,parent=None):return asyncio.run(CognitiveLoop(self.l,model,self.env,daily_cap=0).tick(opportunity_source='experiment',source_search_id=parent))
    def parent(self):return self.tick(Model([SEARCH]))['episode_id']

    def test_one_run_can_search_choose_read_and_assess_without_second_scheduled_wait(self):
        model=Model([SEARCH,READ,ASSESS])
        asyncio.run(CognitiveLoop(self.l,model,self.env,daily_cap=0).run(rounds=1))
        self.assertEqual((self.env.searches,self.env.reads),(1,1));self.assertEqual(len(model.inputs),3)
        rows=list(self.l.db.execute("select * from episodes where json_extract(decision_json,'$.selection_process')='search_source_selection'"))
        self.assertEqual(len(rows),1);d=json.loads(rows[0]['decision_json']);r=json.loads(rows[0]['result_json'])
        self.assertEqual(d['goal']['id'],'g');self.assertTrue(d['parent_search_id']);self.assertEqual(r['inquiry_assessment']['status'],'assessed')
        self.assertIn('g',self.l.view()['progress_memory'])
        self.assertNotIn('experimental_world_model',json.loads(model.inputs[1][-1]['content']))

    def test_source_choice_can_end_and_is_attempted_at_most_once(self):
        parent=self.parent();model=Model([REST]);out=self.tick(model,parent)
        self.assertEqual(out['status'],'rest');self.assertEqual(self.env.reads,0)
        again=Model([]);self.assertEqual(self.tick(again,parent)['status'],'cancelled');self.assertFalse(again.inputs)
        self.assertEqual(self.l.db.execute('select sum(calls) from call_budget').fetchone()[0],2)

    def test_followup_cannot_turn_into_another_search_or_new_goal(self):
        parent=self.parent();out=self.tick(Model([SEARCH]),parent)
        self.assertEqual(out['status'],'decision_error');self.assertEqual(self.env.searches,1);self.assertEqual(self.env.reads,0)

    def test_ending_source_selection_cannot_complete_the_overall_goal(self):
        parent=self.parent();out=self.tick(Model([{**REST,'goal':{'id':'g','status':'completed'}}]),parent)
        self.assertEqual(out['status'],'decision_error')
        self.assertEqual(self.l.db.execute("select status from goals where id='g'").fetchone()[0],'active')
        self.assertEqual(self.env.reads,0)

    def test_parent_withdrawn_during_selection_prevents_read(self):
        parent=self.parent();out=self.tick(Model([READ],hook=lambda:self.l.revoke(parent)),parent)
        self.assertEqual(out['status'],'decision_error');self.assertEqual(self.env.reads,0)

    def test_disabled_feature_or_private_parent_never_calls_selection_model(self):
        parent=self.parent();self.env.cfg['cognition_source_followup_enabled']=False
        m=Model([]);self.assertEqual(self.tick(m,parent)['status'],'cancelled');self.assertFalse(m.inputs)
        self.env.cfg['cognition_source_followup_enabled']=True
        with self.l.db:self.l.db.execute("update episodes set scope='private',actor='person' where id=?",(parent,))
        self.assertEqual(self.tick(m,parent)['status'],'cancelled');self.assertFalse(m.inputs)

    def test_tampered_search_or_goal_version_is_not_reused(self):
        parent=self.parent();row=self.l.db.execute('select result_json from episodes where id=?',(parent,)).fetchone()
        result=json.loads(row[0]);result['query']='tampered'
        with self.l.db:self.l.db.execute('update episodes set result_json=? where id=?',(json.dumps(result),parent))
        m=Model([]);self.assertEqual(self.tick(m,parent)['status'],'cancelled');self.assertFalse(m.inputs)

    def test_failed_selection_does_not_erase_search_or_retry_it(self):
        model=Model([SEARCH,RuntimeError('gateway_failure')])
        asyncio.run(CognitiveLoop(self.l,model,self.env,daily_cap=0).run(rounds=1))
        row=self.l.db.execute("select status,result_json from episodes where json_extract(decision_json,'$.action')='search'").fetchone()
        self.assertEqual(row['status'],'observed');self.assertTrue(json.loads(row['result_json'])['catalog_found'])
        self.assertEqual(self.env.searches,1);self.assertEqual(self.env.reads,0)

    def test_scope_change_during_selection_cannot_install_read(self):
        parent=self.parent()
        def disable():self.env.cfg['cognition_source_followup_enabled']=False
        out=self.tick(Model([READ],hook=disable),parent)
        self.assertEqual(out['status'],'decision_error');self.assertEqual(self.env.reads,0)
