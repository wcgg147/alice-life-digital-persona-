import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition.decision import validate, working_context
from cognition.inquiry import validate_assessment
from cognition.loop import CognitiveLoop
from cognition.state import Ledger


TEXT = '雨站观测记录：风暴到来前气压持续下降，压差驱动空气流动，所以这里风速升高。' * 4
INTENT = {'kind': 'mechanism', 'criterion': '材料直接给出气压与风速变化的因果关系', 'expected_progress': .9}


def detail(kind='partial_answer', **overrides):
    forms={
        'new_answer':('useful','met','new',''),
        'partial_answer':('useful','partial','new','尚缺完整机制的其他环节'),
        'confirmation':('not_useful','met','known',''),
        'none':('not_useful','unmet','uncertain','尚未取得问题所需证据'),
        'uncertain':('uncertain','uncertain','uncertain','无法确定是否推进'),
    }
    verdict,criterion,novelty,missing=forms[kind]
    return {'schema_version':2,'progress_type':kind,'criterion_status':criterion,'novelty':novelty,
            'missing':missing,'prior_episode_ids':[],'verdict':verdict,**overrides}


class SequenceModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.messages = []

    async def complete(self, messages):
        self.calls += 1
        self.messages.append(messages)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        return json.dumps(response, ensure_ascii=False)


class InquiryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.sqlite3')

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def read(self, n, family='archive_a', verdict='not_useful', quote=None, cap=50, assessment=None):
        r = {'id': f'{family}:{n}', 'family': family, 'source_ref': f'fixture:{family}:{n}'}
        d = {'action': 'read', 'resource_id': r['id'], 'reason': '探索原因',
             'goal': {'id': '', 'question': '为什么气压变化时风会增强？', 'need': 'text', 'origin': r['id'], 'status': 'active'},
             'predict_success': True, 'next_check_seconds': 60, 'inquiry': INTENT}
        a = {**detail({'useful':'partial_answer','not_useful':'none','uncertain':'uncertain'}[verdict]),
             'evidence_id': 's0000', **({'quote': quote} if quote else {}),
             'answer': '压差驱动空气流动' if verdict == 'useful' else '', 'reason': '测试评估，允许被撤销'}
        if assessment:a.update(assessment)

        class Env:
            async def observe(inner):
                return [r], []

            async def read(inner, resource):
                return {'status': 'ok', 'text': TEXT, 'source_ref': r['source_ref']}

        model = SequenceModel([d, a])
        out = asyncio.run(CognitiveLoop(self.ledger, model, Env(), daily_cap=cap).tick())
        return out, model

    def test_text_success_does_not_imply_question_progress(self):
        out, _ = self.read(1)
        self.assertTrue(out['met_need'])
        self.assertFalse(out['prediction_error'])
        self.assertEqual(out['inquiry_assessment']['prediction_error'], -.9)
        self.assertEqual(self.ledger.view()['inquiry_model'][0]['status'], 'candidate')
        self.assertEqual(self.ledger.view()['inquiry_model'][0]['failures'], 1)

    def test_assessment_receives_pre_action_knowledge_not_hindsight(self):
        first, model = self.read(1)
        payload = json.loads(model.messages[1][1]['content'])
        self.assertEqual(payload['knowledge_before']['revision'], 0)
        self.assertFalse(payload['knowledge_before']['prior_reads'])
        second, model = self.read(2)
        payload = json.loads(model.messages[1][1]['content'])
        self.assertEqual(payload['knowledge_before']['prior_reads'][0]['episode_id'], first['episode_id'])
        self.assertNotIn(second['episode_id'], json.dumps(payload['knowledge_before']))

    def test_new_resource_attention_changes_and_withdrawal_restores_without_erasing_text(self):
        first, _ = self.read(1)
        self.read(2)
        resources = [{'id': 'a:new', 'family': 'archive_a'}, {'id': 'b:new', 'family': 'archive_b'}]
        before = self.ledger.view()
        ctx = working_context(before, resources, [])
        self.assertEqual(ctx['resources'][0]['family'], 'archive_b')
        old_version = before['inquiry_model'][0]['version']
        self.ledger.revoke_inquiry(first['episode_id'], '原文解释被错误判成无关，撤销判断')
        after = self.ledger.view()
        self.assertNotEqual(after['inquiry_model'][0]['version'], old_version)
        self.assertEqual(after['inquiry_model'][0]['status'], 'candidate')
        ep = next(e for e in after['episodes'] if e['id'] == first['episode_id'])
        self.assertFalse(ep['revoked'])
        self.assertEqual(ep['result']['text'], TEXT)
        projected = working_context(after, resources, [])
        self.assertNotIn('inquiry_belief', projected['resources'][0])
        withdrawn = next(e for e in projected['episodes'] if e['id'] == first['episode_id'])
        self.assertNotIn('withdrawn_inquiry_assessment', withdrawn['result'])

    def test_modified_observation_cannot_keep_a_stale_learning_receipt(self):
        first, _ = self.read(1)
        self.read(2)
        with self.ledger.db:
            row = self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?', (first['episode_id'],)).fetchone()
            result = json.loads(row[0]); result['text'] += '来源内容后来被改动'
            self.ledger.db.execute('UPDATE episodes SET result_json=? WHERE id=?', (json.dumps(result), first['episode_id']))
        belief = self.ledger.view()['inquiry_model'][0]
        self.assertEqual(belief['support_count'], 1)
        self.assertEqual(belief['status'], 'candidate')

    def test_uncertain_unquoted_and_repeated_source_do_not_promote(self):
        self.read(1, verdict='uncertain')
        self.assertFalse(self.ledger.view()['inquiry_model'])
        bad, _ = self.read(2, quote='这段文字完全不在实际材料里面')
        self.assertEqual(bad['status'], 'observed')
        self.assertEqual(bad['inquiry_assessment']['status'], 'unassessed')
        self.assertFalse(self.ledger.view()['inquiry_model'])
        self.read(3)
        self.read(3)
        self.assertEqual(self.ledger.view()['inquiry_model'][0]['support_count'], 1)

    def test_budget_exhaustion_keeps_receipt_and_skips_learning(self):
        out, model = self.read(1, cap=1)
        self.assertEqual(model.calls, 1)
        self.assertEqual(out['status'], 'observed')
        self.assertEqual(out['inquiry_assessment']['reason'], 'daily_model_budget_exhausted')
        self.assertEqual(self.ledger.view()['episodes'][0]['result']['text'], TEXT)
        self.assertFalse(self.ledger.view()['inquiry_model'])

    def test_new_counterevidence_updates_a_learned_tendency(self):
        self.read(1)
        self.read(2)
        before = self.ledger.view()['inquiry_model'][0]
        self.read(3, verdict='useful')
        self.read(4, verdict='useful')
        after = self.ledger.view()['inquiry_model'][0]
        self.assertEqual(before['estimate'], .25)
        self.assertEqual(after['estimate'], .5)
        self.assertNotEqual(before['version'], after['version'])
        self.assertEqual(after['failures'], 2)
        self.assertEqual(after['successes'], 2)

    def test_late_result_cannot_resurrect_revoked_evidence_or_judgment(self):
        out, _ = self.read(1)
        assessment, receipt = out['inquiry_assessment'], out['receipt']
        self.ledger.revoke_inquiry(out['episode_id'], '无效判断')
        self.assertFalse(self.ledger.annotate_inquiry(out['episode_id'], assessment, receipt))
        self.ledger.revoke(out['episode_id'])
        self.assertFalse(self.ledger.annotate_inquiry(out['episode_id'], assessment, receipt))
        self.assertFalse(self.ledger.view()['inquiry_model'])

    def test_cancelled_assessment_keeps_already_observed_read(self):
        class CancelDuringAssessment:
            calls = 0

            async def complete(inner, messages):
                inner.calls += 1
                if inner.calls == 1:
                    return {'action': 'read', 'resource_id': 'r', 'reason': '有问题想弄清',
                            'goal': {'id': '', 'question': '风为何增强？', 'need': 'text', 'origin': 'r', 'status': 'active'},
                            'predict_success': True, 'next_check_seconds': 60, 'inquiry': INTENT}
                raise asyncio.CancelledError()

        class Env:
            async def observe(inner):
                return [{'id': 'r', 'family': 'x', 'source_ref': 'fixture:r'}], []

            async def read(inner, resource):
                return {'status': 'ok', 'text': TEXT, 'source_ref': 'fixture:r'}

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(CognitiveLoop(self.ledger, CancelDuringAssessment(), Env()).tick())
        episode = self.ledger.view()['episodes'][0]
        self.assertEqual(episode['status'], 'observed')
        self.assertEqual(episode['result']['text'], TEXT)
        self.assertEqual(episode['result']['inquiry_assessment']['reason'], 'assessment_pending')
        self.assertFalse(self.ledger.view()['inquiry_model'])

    def test_reopen_retains_learning_after_working_memory_window(self):
        self.read(1)
        self.read(2)
        for _ in range(9):
            ep = self.ledger.begin({}, {'action': 'rest'})
            self.ledger.finish(ep, 'rest', {})
        self.ledger.close()
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.sqlite3')
        self.assertEqual(self.ledger.view()['inquiry_model'][0]['support_count'], 2)
        self.assertTrue(self.ledger.view()['inquiry_intents'])

    def test_bad_prediction_and_fabricated_quote_are_rejected(self):
        from cognition.inquiry import validate_intent
        for value in (True, float('nan'), 1.1, -.1):
            with self.assertRaises(ValueError):
                validate_intent({**INTENT, 'expected_progress': value})
        with self.assertRaisesRegex(ValueError, 'ungrounded'):
            validate_assessment({'verdict': 'useful', 'quote': '不在原文里的假证据' * 2,
                                 'answer': '答案', 'reason': '判断'}, TEXT, INTENT, 'receipt')

    def test_extractive_span_cannot_invent_or_modify_the_quote(self):
        raw = {'verdict': 'useful', 'evidence_id': 's0000', 'answer': '压差使风增强', 'reason': '有部分因果解释'}
        checked = validate_assessment(raw, TEXT, INTENT, 'r')
        self.assertIn(checked['quote'], TEXT)
        self.assertEqual(checked['evidence_id'], 's0000')
        with self.assertRaisesRegex(ValueError, 'invalid_evidence_span'):
            validate_assessment({**raw, 'evidence_id': 's9999'}, TEXT, INTENT, 'r')
        with self.assertRaisesRegex(ValueError, 'invalid_evidence_span'):
            validate_assessment({**raw, 'quote': '编造的证据'}, TEXT, INTENT, 'r')

    def test_ablation_and_private_read_do_not_leak_learned_beliefs(self):
        self.read(1)
        self.read(2)
        state = self.ledger.view()
        self.assertFalse(working_context(state, [], [], use_strategies=False)['inquiry_model'])
        # Move the valid source records to one private actor to verify the
        # projection itself, without synthetic QQ messages in a real database.
        with self.ledger.db:
            self.ledger.db.execute("UPDATE episodes SET scope='private:11',actor='11'")
        self.assertFalse(self.ledger.view()['inquiry_model'])
        self.assertFalse(self.ledger.view('private:22', '22')['inquiry_model'])
        self.assertEqual(self.ledger.view('private:11', '11')['inquiry_model'][0]['support_count'], 2)

    def test_detail_cannot_claim_an_unmet_criterion_is_learning(self):
        base={**detail(),'evidence_id':'s0000','answer':'材料支持的部分解释','reason':'有来源'}
        for change in ({'criterion_status':'unmet'}, {'progress_type':'new_answer'},
                       {'novelty':'known'}, {'schema_version':True}, {'progress_type':'confirmation'}):
            with self.assertRaises(ValueError):validate_assessment({**base,**change},TEXT,INTENT,'r')
        checked=validate_assessment(base,TEXT,INTENT,'r')
        self.assertEqual(checked['progress_type'],'partial_answer')

    def test_semantic_confirmation_does_not_create_progress_and_dependency_is_reversible(self):
        first,_=self.read(1,verdict='useful')
        repeat,_=self.read(2,assessment=detail('confirmation',answer='空气受压力差推动而加速',prior_episode_ids=[first['episode_id']]))
        self.assertEqual(repeat['inquiry_assessment']['progress_type'],'confirmation')
        profile=self.ledger.view()['motivation']['goals'][0]
        self.assertEqual(profile['total_new_progress'],1)
        self.assertEqual(profile['recent_partial_answers'],1)
        self.assertEqual(profile['recent_full_answers'],0)
        self.assertEqual(profile['recent_confirmations'],1)
        self.ledger.revoke_inquiry(first['episode_id'],'旧解释有误，保留原文')
        state=self.ledger.view()
        self.assertFalse(state['inquiry_model'])
        self.assertFalse(state['progress_memory'])
        self.assertEqual(state['motivation']['goals'][0]['recent_measured_feedback'],0)
        self.assertEqual(len(state['episodes']),2)

    def test_assessment_rejects_unseen_prior_reference_without_erasing_read(self):
        out,_=self.read(1,assessment=detail('confirmation',answer='已知结论',prior_episode_ids=['invented']))
        self.assertEqual(out['status'],'observed')
        self.assertEqual(out['inquiry_assessment']['validation_code'],'unknown_prior_progress_evidence')
        self.assertFalse(self.ledger.view()['inquiry_model'])

    def test_conservative_known_none_is_not_new_progress(self):
        first,_=self.read(1,verdict='useful')
        out,_=self.read(2,assessment=detail('none',novelty='known',prior_episode_ids=[first['episode_id']]))
        self.assertEqual(out['inquiry_assessment']['status'],'assessed')
        profile=self.ledger.view()['motivation']['goals'][0]
        self.assertEqual(profile['recent_confirmations'],1)
        self.assertEqual(profile['total_new_progress'],1)

    def test_repeated_irrelevance_can_reference_visible_negative_read_without_claiming_known_answer(self):
        first,_=self.read(1)
        out,_=self.read(2,assessment=detail('none',novelty='known',prior_episode_ids=[first['episode_id']]))
        self.assertEqual(out['inquiry_assessment']['status'],'assessed')
        self.assertFalse(self.ledger.view()['progress_memory'])
        belief=self.ledger.view()['inquiry_model'][0]
        self.assertEqual(belief['failures'],2);self.assertEqual(belief['successes'],0)

    def test_partial_progress_may_use_visible_raw_prior_but_loses_effect_when_it_is_revoked(self):
        first,_=self.read(1,cap=1)
        out,_=self.read(2,verdict='useful',assessment=detail('partial_answer',prior_episode_ids=[first['episode_id']]))
        self.assertEqual(out['inquiry_assessment']['status'],'assessed')
        self.assertEqual(self.ledger.view()['inquiry_model'][0]['successes'],1)
        self.ledger.revoke(first['episode_id'])
        self.assertFalse(self.ledger.view()['inquiry_model'])

    def test_new_self_contained_assessment_may_omit_empty_prior_array_with_audit_marker(self):
        raw={**detail(),'evidence_id':'s0000','answer':'压差驱动空气流动','reason':'原文支持部分机制'}
        raw.pop('prior_episode_ids')
        checked=validate_assessment(raw,TEXT,INTENT,'r')
        self.assertEqual(checked['prior_episode_ids'],[]);self.assertTrue(checked['defaulted_empty_prior_refs'])
        self.assertEqual(validate_assessment(checked,TEXT,INTENT,'r'),checked)

    def test_known_or_invalid_explicit_references_are_not_defaulted(self):
        raw={**detail('confirmation'),'evidence_id':'s0000','answer':'已有回答','reason':'重复确认'}
        raw.pop('prior_episode_ids')
        with self.assertRaises(ValueError):validate_assessment(raw,TEXT,INTENT,'r')
        for value in (None,'',False):
            with self.assertRaises(ValueError):validate_assessment({**detail(),'prior_episode_ids':value,'evidence_id':'s0000',
                'answer':'原文回答','reason':'有来源'},TEXT,INTENT,'r')

    def test_failed_assessment_preserves_raw_reply_but_does_not_reinject_it_as_memory(self):
        out,_=self.read(1,assessment={'prior_episode_ids':None})
        self.assertEqual(out['inquiry_assessment']['status'],'unassessed')
        self.assertTrue(out['inquiry_assessment']['raw_output'])
        ctx=working_context(self.ledger.view(),[],[])
        self.assertNotIn('raw_output',ctx['episodes'][0]['result']['inquiry_assessment'])

    def test_current_partial_can_complete_a_chain_without_claiming_full_current_answer(self):
        first,_=self.read(1,verdict='useful')
        out,_=self.read(2,verdict='useful',assessment=detail('partial_answer',criterion_status='met',missing='',
            prior_episode_ids=[first['episode_id']],answer='电机经绳索驱动窗扇'))
        self.assertEqual(out['inquiry_assessment']['status'],'assessed')
        self.assertEqual(out['inquiry_assessment']['progress_type'],'partial_answer')
        self.assertEqual(self.ledger.view()['motivation']['goals'][0]['recent_full_answers'],0)

    def test_same_goal_grounded_memory_survives_recent_window_and_ablation(self):
        first,_=self.read(1,verdict='useful')
        for _ in range(9):
            ep=self.ledger.begin({}, {'action':'rest'});self.ledger.finish(ep,'rest',{})
        out,model=self.read(2,assessment=detail('confirmation',answer='压力梯度让空气流动',prior_episode_ids=[first['episode_id']]))
        payload=json.loads(model.messages[1][1]['content'])
        self.assertFalse(payload['knowledge_before']['prior_reads'])
        memory=payload['knowledge_before']['same_goal_answers']
        self.assertEqual(memory[0]['episode_id'],first['episode_id'])
        self.assertEqual(out['inquiry_assessment']['progress_type'],'confirmation')
        self.assertFalse(working_context(self.ledger.view(),[],[],False)['progress_memory'])

    def test_progress_memory_is_source_scoped_and_revalidated(self):
        first,_=self.read(1,verdict='useful')
        with self.ledger.db:self.ledger.db.execute("UPDATE episodes SET scope='private:11',actor='11'")
        self.assertFalse(self.ledger.view()['progress_memory'])
        self.assertFalse(self.ledger.view('private:22','22')['progress_memory'])
        self.assertTrue(self.ledger.view('private:11','11')['progress_memory'])
        self.ledger.revoke(first['episode_id'])
        self.assertFalse(self.ledger.view('private:11','11')['progress_memory'])


if __name__ == '__main__':
    unittest.main()
