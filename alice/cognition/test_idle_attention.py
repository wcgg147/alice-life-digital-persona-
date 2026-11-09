import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from cognition import idle_attention
from cognition.loop import CognitiveLoop
from cognition.state import Ledger,digest

R={'id':'bili:fixture','family':'bilibili_metadata_comments','source_ref':'https://www.bilibili.com/video/BV1x1x96aEGM','title':'一条真实候选的标题'}
APPRAISAL={'judgments':[{'i':1,'open':True,'why':'标题有一个尚不明白的反常点'}],'pick':1,'why_pick':'想先了解一下它具体说什么'}
CFG={'cognition_idle_attention_enabled':True}

class IdleAttentionTests(unittest.TestCase):
    def context(self):
        return {'scope':'public','goals':[{'id':'old','status':'completed'}],
                'resources':[R],'episodes':[]}

    def test_no_rest_counter_and_all_skip_remains_valid(self):
        a=self.context();b={**a,'episodes':[{'decision':{'action':'rest'}}]*100}
        self.assertEqual(idle_attention.candidates(a,CFG),idle_attention.candidates(b,CFG))
        choice=idle_attention.decision({'judgments':[{'i':1,'open':False,'why':'不感兴趣'}],'pick':None,'why_pick':''},[R])
        self.assertEqual(choice['action'],'rest');self.assertIsNone(choice['goal'])

    def test_existing_work_and_private_context_keep_full_deliberation(self):
        self.assertFalse(idle_attention.candidates(self.context(),{}))
        for update in [{'scope':'private:1'},{'goals':[{'status':'active'}]},
                       {'goals':[{'status':'paused'}]},{'pending_social_event':{'id':'s'}},
                       {'pending_learning':[{'id':'r'}]},{'workspace':{'next_steps':[{'id':'g'}]}},
                       {'episodes':[{'decision':{'action':'inspect'}}]},
                       {'episodes':[{'decision':{'action':'wonder'}}]},
                       {'episodes':[{'decision':{'action':'notice'}}],'open_questions':[{'status':'open'}]}]:
            self.assertFalse(idle_attention.candidates({**self.context(),**update},CFG))
        self.assertTrue(idle_attention.candidates({**self.context(),
            'episodes':[{'status':'noticing_error','decision':{'action':'notice'}}],'open_questions':[]},CFG))

    def test_seen_and_out_of_domain_candidates_do_not_create_repeated_idle_probes(self):
        self.assertFalse(idle_attention.candidates({**self.context(),'motivation':{'candidate_opportunities':[
            {'resource_id':R['id'],'source_identity_seen':True}]}},CFG))
        self.assertFalse(idle_attention.candidates({**self.context(),'inspection_sources':[{'source_ref':R['source_ref']}]},CFG))
        self.assertFalse(idle_attention.candidates({**self.context(),'episodes':[
            {'decision':{'action':'rest'}},{'status':'tool_error','decision':{'action':'inspect','resource_id':R['id']}}]},CFG))
        self.assertFalse(idle_attention.candidates({**self.context(),'resources':[{**R,'family':'book_original_text'}]},CFG))

    def test_invalid_or_unoffered_selection_cannot_execute(self):
        for change in [{'pick':2},{'pick':True},{'judgments':[]},
                       {'judgments':[{'i':1,'open':False,'why':'跳过'}]},
                       {'judgments':APPRAISAL['judgments']*2}]:
            with self.assertRaises(ValueError):idle_attention.decision({**APPRAISAL,**change},[R])

    def test_real_loop_records_actual_prompt_and_keeps_observation_separate_from_learning(self):
        class Model:
            calls=[]
            async def complete(self,messages):
                self.calls.append(messages)
                if len(self.calls)==1:return copy.deepcopy(APPRAISAL)
                return {'action':'rest','resource_id':'','goal':None,'predict_success':False,'reason':'暂时不继续','next_check_seconds':600}
        class Env:
            cfg=CFG
            reads=0
            async def observe(self):return [R],[]
            async def read(self,r):
                self.reads+=1
                return {'status':'ok','text':'实际有限观察到的标题、简介和评论。'*100,'source_ref':r['source_ref']}
        with tempfile.TemporaryDirectory() as tmp:
            l=Ledger(Path(tmp)/'ledger.sqlite3');m=Model();env=Env()
            try:
                loop=CognitiveLoop(l,m,env,daily_cap=0)
                first=asyncio.run(loop.tick(opportunity_source='experiment'))
                self.assertEqual(first['status'],'observed');self.assertEqual(env.reads,1)
                row=l.db.execute('SELECT before_json,decision_json FROM episodes WHERE id=?',(first['episode_id'],)).fetchone()
                before,decision=map(json.loads,row)
                self.assertEqual(before['prompt_sha256'],digest(m.calls[0]))
                self.assertEqual(decision['selection_process'],'idle_selective_attention')
                self.assertEqual(decision['attention_appraisal'][0]['resource_id'],R['id'])
                self.assertFalse(l.view()['goals']);self.assertFalse(l.view()['strategies'])
                self.assertFalse(l.view()['inquiry_model']);self.assertFalse(first['learning_assessed'])
                source=l.view()['inspection_sources'][0]
                self.assertEqual(source['attention_intent']['reason'],APPRAISAL['why_pick'])
                second=asyncio.run(loop.tick(opportunity_source='experiment'))
                self.assertEqual(second['status'],'rest');self.assertEqual(len(m.calls),2)
                self.assertIn('revision',json.loads(m.calls[1][1]['content']))
                self.assertEqual(json.loads(m.calls[1][1]['content'])['inspection_sources'][0]['attention_intent'],source['attention_intent'])
                self.assertEqual(l.db.execute('SELECT sum(calls) FROM call_budget').fetchone()[0],2)
                l.revoke(first['episode_id'])
                self.assertFalse(l.view()['inspection_sources'])
            finally:l.close()
