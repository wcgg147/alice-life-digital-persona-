import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition.loop import CognitiveLoop
from cognition.state import Ledger
from cognition.observation_assimilation import pending

R={'id':'bili:fixture','family':'bilibili_metadata_comments','source_ref':'https://www.bilibili.com/video/BV1x1x96aEGM','title':'窗户怎么自己关上了'}
TEXT='装置在降温时自动关窗，但说明没有给出温度阈值。观众认为它很省心。'*8
CHOICE={'judgments':[{'i':1,'open':True,'why':'想了解关窗装置'}],'pick':1,'why_pick':'想知道它怎样决定关窗时机'}
NOTICE={'understanding':{'evidence_id':'s0000','summary':'原文描述降温时自动关窗，没有交代温度阈值。'},
        'questions':[{'evidence_id':'s0000','question':'这个关窗装置的温度阈值如何决定？','reason':'原文提到自动关窗，但没有阈值说明'}]}
REST={'action':'rest','resource_id':'','goal':None,'predict_success':False,'reason':'暂时不行动','next_check_seconds':600}

class Model:
    requires_inquiry=True
    def __init__(self,rows):self.rows=iter(rows);self.calls=[];self.callback=None
    async def complete(self,messages):
        self.calls.append(messages)
        if self.callback:self.callback()
        return next(self.rows)

class Env:
    def __init__(self):
        self.cfg={'cognition_idle_attention_enabled':True,'cognition_observation_assimilation_enabled':True}
        self.observations=0;self.reads=0
    async def observe(self):self.observations+=1;return [R],[]
    async def read(self,r):self.reads+=1;return {'status':'ok','source_ref':r['source_ref'],'text':TEXT}

class TimeoutModel(Model):
    async def complete(self,messages):
        self.calls.append(messages)
        raise TimeoutError('transport did not return a response')

class AssimilationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.l=Ledger(self.path);self.env=Env()
    def tearDown(self):self.l.close();self.tmp.cleanup()
    def tick(self,model):return asyncio.run(CognitiveLoop(self.l,model,self.env,daily_cap=0).tick(opportunity_source='experiment'))
    def observe(self):return self.tick(Model([CHOICE]))

    def test_observation_processes_once_after_restart_then_returns_to_free_decision(self):
        first=self.observe();self.assertTrue(pending(self.l));self.assertFalse(self.l.view()['open_questions'])
        self.l.close();self.l=Ledger(self.path)
        model=Model([NOTICE,REST]);processed=self.tick(model)
        self.assertEqual(processed['status'],'noticed');self.assertEqual(processed['question_count'],1)
        self.assertEqual(self.env.reads,1);self.assertEqual(self.env.observations,1)
        self.assertFalse(pending(self.l));v=self.l.view()
        self.assertEqual(v['open_questions'][0]['source_episode_id'],first['episode_id'])
        self.assertFalse(v['goals']);self.assertFalse(v['inquiry_model']);self.assertFalse(v['strategies'])
        next_result=self.tick(model);self.assertEqual(next_result['status'],'rest')
        context=json.loads(model.calls[1][1]['content'])
        self.assertEqual(context['open_questions'][0]['question'],NOTICE['questions'][0]['question'])
        self.assertEqual(self.l.db.execute('SELECT sum(calls) FROM call_budget').fetchone()[0],3)

    def test_empty_questions_are_successful_processing_without_manufactured_goals(self):
        self.observe();out=self.tick(Model([{**NOTICE,'questions':[]}]))
        self.assertEqual(out['question_count'],0);self.assertFalse(pending(self.l))
        self.assertFalse(self.l.view()['open_questions']);self.assertFalse(self.l.view()['goals'])

    def test_bad_grounding_is_retained_as_failure_not_silently_retried(self):
        self.observe();out=self.tick(Model([{**NOTICE,'understanding':{'evidence_id':'fake','summary':'没有实际来源的理解。'}}]))
        self.assertEqual(out['status'],'noticing_error');self.assertFalse(self.l.view()['open_questions'])
        self.assertFalse(pending(self.l));self.assertTrue(self.l.db.execute('SELECT result_json FROM episodes WHERE id=?',(out['episode_id'],)).fetchone())

    def test_source_revocation_during_model_call_cannot_install_questions(self):
        first=self.observe();model=Model([NOTICE]);model.callback=lambda:self.l.revoke(first['episode_id'])
        out=self.tick(model)
        self.assertEqual(out['status'],'cancelled');self.assertFalse(self.l.view()['open_questions'])

    def test_retracting_processing_or_source_removes_proposals(self):
        first=self.observe();notice=self.tick(Model([NOTICE]));self.assertTrue(self.l.view()['open_questions'])
        self.l.revoke(notice['episode_id']);self.assertFalse(self.l.view()['open_questions']);self.assertTrue(pending(self.l))
        self.l.revoke(first['episode_id']);self.assertFalse(pending(self.l))

    def test_disabled_processing_keeps_observation_and_original_decision(self):
        self.observe();self.env.cfg['cognition_observation_assimilation_enabled']=False
        out=self.tick(Model([REST]));self.assertEqual(out['status'],'rest')
        self.assertTrue(pending(self.l));self.assertFalse(self.l.view()['open_questions'])

    def test_private_source_is_never_sent_to_public_comprehension(self):
        first=self.observe()
        with self.l.db:self.l.db.execute("UPDATE episodes SET scope='private:1',actor='1' WHERE id=?",(first['episode_id'],))
        self.assertFalse(pending(self.l));self.assertFalse(self.l.view()['inspection_sources'])

    def test_disable_during_processing_discards_proposals_and_keeps_actual_observation(self):
        first=self.observe();model=Model([NOTICE])
        model.callback=lambda:self.env.cfg.update(cognition_observation_assimilation_enabled=False)
        out=self.tick(model)
        self.assertEqual(out['status'],'cancelled');self.assertFalse(self.l.view()['open_questions'])
        self.assertEqual(self.l.db.execute('SELECT status FROM episodes WHERE id=?',(first['episode_id'],)).fetchone()[0],'observed')

    def test_one_length_repair_is_counted_and_failed_output_stays_out_of_decision_context(self):
        self.observe();long={**NOTICE,'understanding':{'evidence_id':'s0000','summary':'过长且不能直接进入认知状态的摘要。'*30}}
        model=Model([long,NOTICE,REST]);out=self.tick(model)
        self.assertEqual(out['status'],'noticed');self.assertEqual(len(model.calls),2)
        result=json.loads(self.l.db.execute('SELECT result_json FROM episodes WHERE id=?',(out['episode_id'],)).fetchone()[0])
        self.assertEqual(len(result['format_attempts']),1)
        self.assertTrue(result['format_attempts'][0]['raw_output'])
        self.tick(model);context=json.loads(model.calls[2][1]['content'])
        noticed=next(e for e in context['episodes'] if e['id']==out['episode_id'])
        self.assertNotIn('format_attempts',noticed['result'])
        self.assertEqual(self.l.db.execute('SELECT sum(calls) FROM call_budget').fetchone()[0],4)

    def test_second_invalid_length_is_a_terminal_failure_not_a_retry_loop(self):
        self.observe();long={**NOTICE,'understanding':{'evidence_id':'s0000','summary':'过长的摘要。'*60}}
        model=Model([long,long]);out=self.tick(model)
        self.assertEqual(out['status'],'noticing_error');self.assertEqual(len(model.calls),2)
        self.assertFalse(pending(self.l));self.assertFalse(self.l.view()['open_questions'])

    def test_questions_nested_inside_understanding_still_pass_grounding_checks(self):
        self.observe()
        nested={'understanding':{**NOTICE['understanding'],'questions':NOTICE['questions']},'questions':[]}
        out=self.tick(Model([nested]))
        self.assertEqual(out['status'],'noticed');self.assertEqual(out['question_count'],1)
        self.assertEqual(self.l.view()['open_questions'][0]['question'],NOTICE['questions'][0]['question'])

    def test_one_empty_transport_failure_recovers_saved_observation_without_rereading(self):
        first=self.observe();failed=self.tick(TimeoutModel([]))
        self.assertEqual(failed['status'],'noticing_error');self.assertTrue(pending(self.l))
        self.l.close();self.l=Ledger(self.path)
        result=self.tick(Model([NOTICE]))
        self.assertEqual(result['status'],'noticed');self.assertFalse(pending(self.l))
        row=self.l.db.execute('select before_json from episodes where id=?',(result['episode_id'],)).fetchone()
        self.assertEqual(json.loads(row[0])['transport_retry_of'],failed['episode_id'])
        self.assertEqual(self.l.view()['open_questions'][0]['source_episode_id'],first['episode_id'])
        self.assertEqual(self.env.reads,1);self.assertEqual(self.env.observations,1)
        self.assertEqual(self.l.db.execute('select sum(calls) from call_budget').fetchone()[0],3)

    def test_second_transport_failure_is_terminal(self):
        first=self.observe();self.tick(TimeoutModel([]));self.assertTrue(pending(self.l))
        self.tick(TimeoutModel([]));self.assertFalse(pending(self.l))
        self.assertEqual(self.l.db.execute('select sum(calls) from call_budget').fetchone()[0],3)

    def test_revoked_source_is_not_retried_after_transport_failure(self):
        first=self.observe();self.tick(TimeoutModel([]));self.assertTrue(pending(self.l))
        self.l.revoke(first['episode_id']);self.assertFalse(pending(self.l))
