import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition import test_method_learning as fixtures
from cognition.decision import validate,working_context
from cognition.loop import CognitiveLoop
from cognition.state import Ledger


class PendingLearningTests(unittest.TestCase):
    read=fixtures.MethodLearningTests.read
    search=fixtures.MethodLearningTests.search
    consume=fixtures.MethodLearningTests.consume

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path)
        self.read('a',0,useful=False);self.search();self.first=self.consume();self.pending=self.consume(cap=4)
    def tearDown(self):self.ledger.close();self.tmp.cleanup()
    def proposal(self):return {'action':'evaluate','target_episode_id':self.pending['episode_id'],'goal':None,'resource_id':'',
        'predict_success':False,'reason':'先补评已经读到的材料','next_check_seconds':60,'inquiry':None}
    def evaluate(self,callback=None,cap=10):
        messages=[]
        class Model:
            async def complete(inner,packet):
                messages.append(packet)
                if len(messages)==1:return self.proposal()
                if callback:callback()
                # It is a repeated source; reuse the earlier source-bound answer
                # as a declared test condition, not a new positive observation.
                return {'schema_version':2,'progress_type':'confirmation','criterion_status':'met','novelty':'known','missing':'尚缺阈值数值',
                    'prior_episode_ids':[self.first['episode_id']],'verdict':'not_useful','evidence_id':'s0000',
                    'answer':'阈值触发接点，电机拉索关窗','reason':'之前已记录相同来源与因果链'}
        class Env:
            reads=0
            async def observe(inner):return [],[]
            async def read(inner,resource):inner.reads+=1;raise AssertionError('saved-read evaluation cannot fetch again')
        env=Env();out=asyncio.run(CognitiveLoop(self.ledger,Model(),env,daily_cap=cap).tick(opportunity_source='experiment'))
        self.assertEqual(env.reads,0)
        return out,messages

    def test_optional_saved_evaluation_updates_original_without_new_read(self):
        original=self.ledger.db.execute('select result_json from episodes where id=?',(self.pending['episode_id'],)).fetchone()[0]
        out,_=self.evaluate();self.assertEqual(out['status'],'evaluated');self.assertEqual(out['assessment_status'],'assessed')
        latest=self.ledger.db.execute('select result_json from episodes where id=?',(self.pending['episode_id'],)).fetchone()[0]
        a,b=json.loads(original),json.loads(latest)
        self.assertEqual(a['text'],b['text']);self.assertEqual(a['receipt'],b['receipt'])
        self.assertFalse(self.ledger.view()['pending_learning'])
        self.assertEqual(self.ledger.view()['workspace']['method_trials'][-1]['status'],'no_new_progress')

    def test_original_pre_read_knowledge_is_used_not_later_context(self):
        expected=json.loads(self.ledger.db.execute('select before_json from episodes where id=?',(self.pending['episode_id'],)).fetchone()[0])
        self.read('a',99,useful=True)
        out,messages=self.evaluate();payload=json.loads(messages[1][1]['content'])
        self.assertEqual(payload['knowledge_before']['revision'],expected['revision'])
        self.assertNotIn('第 99',json.dumps(payload,ensure_ascii=False))
        self.assertEqual(out['assessment_status'],'assessed')

    def test_withdrawal_while_model_runs_prevents_late_resurrection(self):
        out,_=self.evaluate(lambda:self.ledger.revoke_inquiry(self.pending['episode_id'],'不再采用这条解释'))
        self.assertEqual(out['status'],'cancelled')
        self.assertFalse(self.ledger.view()['pending_learning'])

    def test_budget_exhaustion_keeps_pending_and_records_attempt_as_unassessed(self):
        out,_=self.evaluate(cap=5)
        self.assertEqual(out['assessment_status'],'unassessed')
        pending=self.ledger.view()['pending_learning'][0]
        self.assertEqual(pending['previous_attempts'],1);self.assertEqual(pending['last_attempt_status'],'unassessed')

    def test_scope_and_explicit_withdrawal_are_respected(self):
        with self.ledger.db:
            self.ledger.db.execute("update episodes set scope='private:11',actor='11'")
            self.ledger.db.execute("update goals set scope='private:11',actor='11'")
        self.assertFalse(self.ledger.view()['pending_learning'])
        self.assertFalse(self.ledger.view('private:22','22')['pending_learning'])
        own=self.ledger.view('private:11','11');self.assertTrue(own['pending_learning'])
        context=working_context(own,[],[]);context.update(scope='private:22',actor='22')
        with self.assertRaises(ValueError):validate(self.proposal(),context)
        self.ledger.revoke_inquiry(self.pending['episode_id'],'明确撤销')
        self.assertFalse(self.ledger.view('private:11','11')['pending_learning'])


if __name__=='__main__':unittest.main()
