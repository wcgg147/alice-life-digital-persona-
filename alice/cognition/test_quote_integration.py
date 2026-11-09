import json
import asyncio
import unittest

from cognition.question_state import validate_quote_evidence
from cognition import test_question_state as fixtures
from cognition.inquiry import receipt_matches


class QuoteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.goal = {'id': 'g'}
        self.text = 'x' * 240 + 'The release date is August 5, 2025. This is a saved statement.'
        self.sources = {'e': {'goal_id': 'g', 'text': self.text, 'source_ref': 'test:source', 'receipt': 'receipt'}}
        self.raw = {'parts': [{'id': '', 'question': '发布日期是什么？', 'conclusion': '2025年8月5日',
                              'coverage': 'answer', 'evidence': [{'quote': 'The release date is August 5, 2025.'}], 'challenges': []}]}

    def test_quote_derives_original_span_and_preserves_receipt(self):
        parts, audit = validate_quote_evidence(self.raw, self.goal, self.sources, {})
        binding = parts[0]['claim']['evidence'][0]
        self.assertEqual((binding['episode_id'], binding['span_id'], binding['receipt']), ('e', 's0240', 'receipt'))
        self.assertEqual(audit[0]['start'], 240)
        self.assertNotIn('span_id', self.raw['parts'][0]['evidence'][0])

    def test_model_ids_or_unknown_quotes_are_not_repaired(self):
        for ref in ({'quote': 'invented evidence'}, {'quote': 'The release date', 'span_id': 's0000'}):
            raw = json.loads(json.dumps(self.raw)); raw['parts'][0]['evidence'] = [ref]
            with self.assertRaises(ValueError):
                validate_quote_evidence(raw, self.goal, self.sources, {})

    def test_duplicate_sources_and_foreign_goal_are_rejected(self):
        sources = {**self.sources, 'other': {**self.sources['e'], 'source_ref': 'test:other'}}
        with self.assertRaises(ValueError):
            validate_quote_evidence(self.raw, self.goal, sources, {})
        sources = {'e': {**self.sources['e'], 'goal_id': 'foreign'}}
        with self.assertRaises(ValueError):
            validate_quote_evidence(self.raw, self.goal, sources, {})

    def test_prior_source_requires_same_part_receipt_and_visible_span(self):
        parts, _ = validate_quote_evidence(self.raw, self.goal, self.sources, {})
        previous = {'parts': [{'id': parts[0]['id'], 'question': self.raw['parts'][0]['question'], 'claims': [parts[0]['claim']]}]}
        raw = json.loads(json.dumps(self.raw)); raw['parts'][0]['id'] = parts[0]['id']
        reused, _ = validate_quote_evidence(raw, self.goal, {}, previous, prior_sources=self.sources)
        self.assertEqual(reused[0]['claim']['evidence'], parts[0]['claim']['evidence'])
        with self.assertRaises(ValueError):
            validate_quote_evidence(raw, self.goal, {}, previous, prior_sources={})
        raw['parts'][0]['id'] = ''; raw['parts'][0]['question'] = '另一子问题？'
        with self.assertRaises(ValueError):
            validate_quote_evidence(raw, self.goal, {}, previous, prior_sources=self.sources)

    def test_exact_contiguous_quote_uses_both_original_spans(self):
        text='x'*230+'This is continuous evidence across the split.'
        sources={'e':{**self.sources['e'],'text':text}}
        raw=json.loads(json.dumps(self.raw));raw['parts'][0]['evidence']=[{'quote':'This is continuous evidence across the split.'}]
        parts,audit=validate_quote_evidence(raw,self.goal,sources,{})
        self.assertEqual([b['span_id'] for b in parts[0]['claim']['evidence']],['s0000','s0240'])
        self.assertEqual(''.join(p['quote'] for p in audit),'This is continuous evidence across the split.')

    def test_contiguous_support_still_obeys_two_ref_limit(self):
        text='a'*240+'b'*240+'c'*240
        sources={'e':{**self.sources['e'],'text':text}}
        raw=json.loads(json.dumps(self.raw));raw['parts'][0]['evidence']=[{'quote':text}]
        with self.assertRaises(ValueError):
            validate_quote_evidence(raw,self.goal,sources,{})


class QuoteIntegrationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.QuestionStateTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def run_candidate(self, withdraw=False):
        from cognition.question_state import integrate
        ledger=self.fixture.ledger;view=ledger.view();option=view['question_integrations'][0]
        eid=ledger.begin({}, {'action':'integrate','integration_goal_id':option['goal_id']})
        class Model:
            async def complete(inner,messages):
                payload=json.loads(messages[1]['content']);source=payload['sources'][0]
                quote=source['spans'][0]['text']
                if withdraw:ledger.revoke(source['episode_id'])
                return {'parts':[{'id':'','question':'触发条件是什么？','conclusion':'温度变化触发开关',
                    'coverage':'partial','evidence':[{'quote':quote}],'challenges':[]}]}
        return asyncio.run(integrate(Model(),ledger,eid,option['goal_id'],option['input_version'],
                                     'public','',0,lambda:True,citation_mode='quote'))

    def test_candidate_writes_receipt_bound_state_and_withdraws(self):
        out=self.run_candidate();self.assertEqual(out['status'],'integrated')
        view=self.fixture.ledger.view();part=view['question_states']['a']['parts'][0]
        source=part['claims'][0]['evidence'][0]['episode_id']
        ep=next(e for e in view['episodes'] if e['id']==out['episode_id'])
        self.assertTrue(receipt_matches(ep['result']))
        self.assertIn('raw_output',ep['result']);self.assertTrue(ep['result']['quote_locations'])
        self.fixture.ledger.revoke(source)
        self.assertFalse(self.fixture.ledger.view()['question_states']['a']['parts'][0]['claims'])

    def test_late_revocation_cannot_install_quote_candidate(self):
        out=self.run_candidate(withdraw=True)
        self.assertEqual(out['status'],'integration_error')
        self.assertFalse(self.fixture.ledger.view()['question_states'])


if __name__ == '__main__':
    unittest.main()
