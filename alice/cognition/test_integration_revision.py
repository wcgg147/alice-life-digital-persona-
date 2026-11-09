import asyncio,json,unittest
from unittest.mock import patch
from cognition import test_question_state as fixtures
from cognition.loop import CognitiveLoop
from cognition.state import Ledger,digest
from cognition.self_model import retrieve_related_knowledge

class IntegrationRevisionTests(unittest.TestCase):
    setUp=fixtures.QuestionStateTests.setUp
    tearDown=fixtures.QuestionStateTests.tearDown
    read=fixtures.QuestionStateTests.read
    initial=fixtures.QuestionStateTests.initial

    def run_revision(self,*,enabled=True,target=None,callback=None,claim_target=None):
        raw=self.initial()
        if claim_target:raw['parts'][0]['evidence'][0]['episode_id']=claim_target
        raw['withdraw_interpretations']=[{'episode_id':target or self.b,'reason':'旧解释把不同条件混为一谈',
            'evidence':[{'episode_id':self.b,'span_id':'s0000'}]}]
        calls=[]
        class Model:
            async def complete(inner,messages):
                calls.append(messages)
                if len(calls)==1:return {'action':'integrate','integration_goal_id':'a','goal':None,'resource_id':'',
                    'predict_success':False,'next_check_seconds':600,'reason':'核对旧认识及其适用范围'}
                if callback:callback()
                return raw
        class Env:
            cfg={'cognition_interpretation_revision_enabled':enabled}
            async def observe(inner):return [],[]
            async def read(inner,resource):raise AssertionError('no external reads')
        return asyncio.run(CognitiveLoop(self.ledger,Model(),Env(),daily_cap=0).tick(opportunity_source='experiment')),calls

    def test_explicit_revision_keeps_raw_source_and_removes_old_judgment_after_reopen(self):
        before=json.loads(self.ledger.db.execute('select result_json from episodes where id=?',(self.b,)).fetchone()[0])
        result,calls=self.run_revision();self.assertEqual(result['status'],'integrated')
        payload=json.loads(calls[1][1]['content'])
        self.assertTrue(next(s for s in payload['sources'] if s['episode_id']==self.b)['reading_interpretation'])
        self.ledger.close();self.ledger=Ledger(self.path)
        source=json.loads(self.ledger.db.execute('select result_json from episodes where id=?',(self.b,)).fetchone()[0])
        self.assertEqual(source['text'],before['text']);self.assertEqual(source['receipt'],before['receipt'])
        self.assertEqual(source['inquiry_assessment']['status'],'withdrawn')
        view=self.ledger.view()
        self.assertEqual([p['episode_id'] for p in view['progress_memory']['a']],[self.a])
        self.assertFalse(any(e['episode_id']==self.b for item in retrieve_related_knowledge(view,['温度变化触发开关']) for e in item['evidence']))
        self.assertIn('a',view['question_states'])

    def test_feature_disabled_does_not_allow_withdrawal(self):
        result,_=self.run_revision(enabled=False)
        self.assertEqual(result['status'],'integration_error')
        self.assertEqual(len(self.ledger.view()['progress_memory']['a']),2)

    def test_unknown_target_and_new_claim_using_withdrawn_interpretation_are_rejected(self):
        other=self.read('other',0)
        result,_=self.run_revision(target=other);self.assertEqual(result['status'],'integration_error')
        result,_=self.run_revision(claim_target=self.b);self.assertEqual(result['status'],'integration_error')
        self.assertEqual(len(self.ledger.view()['progress_memory']['a']),2)

    def test_late_judgment_change_rejects_revision(self):
        def change():
            row=self.ledger.db.execute('select result_json from episodes where id=?',(self.b,)).fetchone()
            r=json.loads(row[0]);r['inquiry_assessment']['reason']='评估已被另一次实际修正替换'
            with self.ledger.db:self.ledger.db.execute('update episodes set result_json=? where id=?',(json.dumps(r),self.b))
        result,_=self.run_revision(callback=change);self.assertEqual(result['status'],'integration_error')
        self.assertEqual(len(self.ledger.view()['progress_memory']['a']),2)

    def test_rebuild_failure_rolls_back_withdrawal_and_integration_together(self):
        original=self.ledger._rebuild_strategies;count=0
        def fail_once():
            nonlocal count
            count+=1
            if count==1:raise RuntimeError('injected_rebuild_failure')
            return original()
        with patch.object(self.ledger,'_rebuild_strategies',side_effect=fail_once):result,_=self.run_revision()
        self.assertEqual(result['status'],'integration_error')
        self.assertEqual(len(self.ledger.view()['progress_memory']['a']),2)
        self.assertFalse(self.ledger.view()['question_states'])

    def test_ledger_itself_rejects_cross_actor_revision(self):
        row=self.ledger.db.execute('select result_json from episodes where id=?',(self.b,)).fetchone()
        old=json.loads(row[0])
        revision={'episode_id':self.b,'source_receipt':old['receipt'],
            'judgment_version':digest(old['inquiry_assessment']),'reason':'不能跨可见域撤回','evidence':[]}
        with self.ledger.db:self.ledger.db.execute("update episodes set scope='private:x',actor='x' where id=?",(self.b,))
        ep=self.ledger.begin({}, {'action':'integrate','integration_goal_id':'a','_scope':'public','_actor':''})
        result={'goal_id':'a','withdraw_interpretations':[revision]};result['receipt']=digest(result)
        with self.assertRaisesRegex(ValueError,'out_of_scope'):
            self.ledger.finish(ep,'integrated',result,inquiry_withdrawals=[revision])
        current=json.loads(self.ledger.db.execute('select result_json from episodes where id=?',(self.b,)).fetchone()[0])
        self.assertNotIn('inquiry_revoked',current)

    def test_withdrawn_in_flight_integration_cannot_withdraw_a_reading(self):
        def cancel():
            row=self.ledger.db.execute("select id from episodes where status='started' order by rowid desc limit 1").fetchone()
            self.ledger.revoke(row[0])
        result,_=self.run_revision(callback=cancel)
        self.assertEqual(result['status'],'integration_error')
        self.assertEqual(len(self.ledger.view()['progress_memory']['a']),2)
        self.assertFalse(self.ledger.view()['question_states'])

if __name__=='__main__':unittest.main()
