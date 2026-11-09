"""Single useful reading can be organized without reclaiming a held goal."""
import json
import unittest

from cognition import idle_attention
from cognition.decision import working_context
from cognition.state import Ledger
from cognition import test_motivation as fixtures
from cognition import test_question_state as question_fixtures


class SingleKnowledgeTests(unittest.TestCase):
    setUp=fixtures.MotivationTests.setUp
    tearDown=fixtures.MotivationTests.tearDown
    read=fixtures.MotivationTests.read
    initial=question_fixtures.QuestionStateTests.initial
    integrate=question_fixtures.QuestionStateTests.integrate

    def test_single_read_uses_existing_integration_then_withdrawal_removes_recall(self):
        from cognition.self_model import retrieve_related_knowledge
        self.a=self.read('a',0,detail=True)
        outcome,calls=self.integrate()
        self.assertEqual(outcome['status'],'integrated');self.assertEqual(len(calls),2)
        view=self.ledger.view()
        self.assertFalse(view['question_integrations'])
        self.assertEqual(len(view['question_states']['a']['parts']),2)
        self.assertTrue(retrieve_related_knowledge(view,['温度变化触发开关']))
        self.ledger.revoke_inquiry(self.a,'本解释撤回，保留原始文本')
        self.ledger.close();self.ledger=Ledger(self.path)
        self.assertFalse(retrieve_related_knowledge(self.ledger.view(),['温度变化触发开关']))

    def test_useful_single_read_can_be_organized_but_unknown_or_unhelpful_cannot(self):
        useful=self.read('useful',0,detail=True)
        self.read('unhelpful',0,useful=False,detail=True)
        self.read('unknown',0,legacy=True)
        view=self.ledger.view()
        self.assertEqual([o['goal_id'] for o in view['question_integrations']],['useful'])
        self.assertEqual(view['question_integrations'][0]['source_ids'],[useful])
        self.assertFalse(view['question_states'])  # opportunity is not an acquired claim

    def test_withdrawn_judgment_cannot_reopen_single_read_organization_after_restart(self):
        eid=self.read('a',0,detail=True)
        self.assertTrue(self.ledger.view()['question_integrations'])
        self.ledger.revoke_inquiry(eid,'解释超出了实际证据')
        self.ledger.close();self.ledger=Ledger(self.path)
        view=self.ledger.view()
        self.assertFalse(view['question_integrations']);self.assertFalse(view['progress_memory'])
        row=self.ledger.db.execute('select result_json,revoked from episodes where id=?',(eid,)).fetchone()
        self.assertFalse(row['revoked']);self.assertTrue(json.loads(row['result_json'])['text'])

    def test_held_goal_integration_does_not_block_idle_but_active_work_still_does(self):
        self.read('a',0,detail=True)
        context=working_context(self.ledger.view(),[],[])
        context['workspace']={'intentions':[{'goal_id':'a','stance':'hold','condition_observed':False}]}
        self.assertTrue(context['question_integrations'])
        self.assertTrue(idle_attention.is_idle(context))
        context['workspace']['intentions'][0]['condition_observed']=True
        self.assertFalse(idle_attention.is_idle(context))
        context['workspace']={'intentions':[{'goal_id':'a','stance':'hold','condition_observed':False}]}
        context['question_integrations'].append({'goal_id':'other'})
        self.assertFalse(idle_attention.is_idle(context))

    def test_fully_answered_single_source_can_review_and_withdraw_without_revival(self):
        import asyncio
        from cognition.development import review_topic
        self.a=self.read('a',0,detail=True)
        raw=self.initial();raw['parts']=raw['parts'][:1]
        result,_=self.integrate(raw);self.assertEqual(result['status'],'integrated')
        view=self.ledger.view()
        self.assertTrue(view['development_opportunities'])
        self.assertEqual(len(view['consolidations']),1)
        class Model:
            async def complete(inner,messages):
                payload=json.loads(messages[1]['content']);source=payload['sources'][0]
                return {'summary':'实际材料支持温度变化触发开关，问题已获得有来源的回答。',
                    'goal_id':'a','goal_status':'completed','answer':'温度变化触发开关',
                    'reason':'已整合子问题有直接原文支持','evidence':[{'episode_id':source['episode_id'],'span_id':'s0000'}],
                    'next_question':None,'next_need':None}
        asyncio.run(review_topic(Model(),self.ledger,'book:a'))
        self.assertEqual(self.ledger.view()['goals'][0]['status'],'completed')
        self.assertTrue(self.ledger.view()['consolidations'])
        self.ledger.revoke_inquiry(self.a,'原解释撤回')
        self.ledger.close();self.ledger=Ledger(self.path)
        view=self.ledger.view()
        self.assertEqual(view['goals'][0]['status'],'active')
        self.assertFalse(view['development_opportunities']);self.assertFalse(view['consolidations'])


if __name__=='__main__':unittest.main()
