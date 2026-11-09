import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch

from cognition.state import Ledger
from cognition.loop import CognitiveLoop
from cognition.decision import validate,working_context
from cognition.environment import ReadingEnvironment


R={'id':'r','family':'fixture','source_ref':'fixture:r','title':'实际候选'}
TEXT='温度变化时开关会闭合，电机随后转动，但材料没有说明温度阈值由什么决定。'*60


def action(name,**kw):
    return {'action':name,'resource_id':'r' if name=='inspect' else '', 'goal':None,
            'inquiry':None,'predict_success':False,'reason':'看看真实内容再决定','next_check_seconds':60,**kw}


class Model:
    requires_inquiry=True
    def __init__(self,response):self.response=response
    async def complete(self,messages):return self.response


class Env:
    def __init__(self):self.reads=0
    async def observe(self):return [R],[]
    async def read(self,r):
        self.reads+=1
        return {'status':'ok','source_ref':r['source_ref'],'text':TEXT}


class NoticingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.ledger=Ledger(Path(self.tmp.name)/'ledger.sqlite3');self.env=Env()
    def tearDown(self):self.ledger.close();self.tmp.cleanup()
    def tick(self,d):return asyncio.run(CognitiveLoop(self.ledger,Model(d),self.env).tick(opportunity_source='experiment'))
    def wonder(self,eid,**kw):
        return action('wonder',wonder={'source_episode_id':eid,'evidence_id':'s0000','question':'温度阈值是怎样决定的？',
                                      'status':'open','reason':'原文给出了执行链但没有说明阈值',**kw})
    def test_observation_then_question_defer_goal_selection_and_revocation(self):
        e=self.tick(action('inspect'));v=self.ledger.view()
        self.assertEqual(len(v['inspection_sources'][0]['text']),1200)
        self.assertFalse(v['goals']);self.assertFalse(v['strategies']);self.assertFalse(v['open_questions'])
        self.assertFalse(v['motivation']['interest_cues']);self.assertFalse(e['learning_assessed'])
        self.tick(self.wonder(e['episode_id']));q=self.ledger.view()['open_questions'][0]
        self.tick(action('wonder',wonder={'id':q['id'],'status':'deferred','reason':'暂时没有足够依据追究'}))
        v=self.ledger.view();self.assertEqual(v['open_questions'][0]['status'],'deferred')
        c=working_context(v,[R],[]);c.update(scope='public',exploration={'can_search':True,'providers':['bing_rss']})
        d=validate(action('search',goal={'id':'','question':q['question'],'need':'text','origin':e['episode_id'],'status':'active'},
                          predict_success=True,search_query='温控开关 阈值 原理',search_provider='bing_rss'),c)
        chosen=self.ledger.begin(c,d);self.ledger.finish(chosen,'observed',{})
        self.assertFalse(self.ledger.view()['open_questions'])
        self.ledger.revoke(e['episode_id']);self.assertFalse(self.ledger.view()['inspection_sources'])
        self.assertFalse(self.ledger.view()['goals'])
    def test_repeated_content_question_dismissal_and_retraction_do_not_multiply(self):
        a=self.tick(action('inspect'));self.tick(self.wonder(a['episode_id']))
        b=self.tick(action('inspect'));self.tick(self.wonder(b['episode_id']))
        self.assertEqual(len(self.ledger.view()['open_questions']),1)
        self.assertEqual(len(self.ledger.view()['inspection_sources']),1)
        q=self.ledger.view()['open_questions'][0]
        dismiss=self.tick(action('wonder',wonder={'id':q['id'],'status':'dismissed','reason':'不值得继续追究'}))
        self.assertFalse(self.ledger.view()['open_questions'])
        self.ledger.revoke(dismiss['episode_id']);self.assertEqual(len(self.ledger.view()['open_questions']),1)
        self.assertFalse(self.ledger.view()['strategies'])
    def test_private_inspection_is_not_a_public_question_source(self):
        e=self.tick(action('inspect'))
        with self.ledger.db:self.ledger.db.execute("update episodes set scope='private:11',actor='11' where id=?",(e['episode_id'],))
        self.assertFalse(self.ledger.view()['inspection_sources'])
        self.assertFalse(self.ledger.view('private:22','22')['inspection_sources'])
        c=working_context(self.ledger.view('private:11','11'),[R],[])
        self.assertEqual(validate(self.wonder(e['episode_id']),c)['wonder']['source_episode_id'],e['episode_id'])
        self.assertFalse(working_context(self.ledger.view('private:11','11'),[R],[],False)['inspection_sources'])
    def test_rest_stays_legal_and_bad_reference_or_goal_in_inspection_is_rejected(self):
        self.assertEqual(self.tick(action('rest'))['status'],'rest');self.assertEqual(self.env.reads,0)
        c=working_context(self.ledger.view(),[R],[])
        with self.assertRaisesRegex(ValueError,'unauthorized_resource'):validate(action('inspect',resource_id='unknown'),c)
        with self.assertRaisesRegex(ValueError,'observation_is_not_goal_progress'):validate(action('inspect',goal={'id':'x'}),c)
        e=self.tick(action('inspect'));bad=self.tick(self.wonder(e['episode_id'],evidence_id='invented'))
        self.assertEqual(bad['status'],'decision_error');self.assertFalse(self.ledger.view()['open_questions'])
    def test_source_withdrawal_while_deciding_prevents_question_commit(self):
        e=self.tick(action('inspect'));ledger=self.ledger;raw=self.wonder(e['episode_id'])
        class Withdrawing:
            async def complete(self,messages):ledger.revoke(e['episode_id']);return raw
        out=asyncio.run(CognitiveLoop(ledger,Withdrawing(),self.env).tick())
        self.assertEqual(out['status'],'decision_error');self.assertFalse(ledger.view()['open_questions'])
    def test_read_failure_does_not_create_source_or_learning(self):
        self.env.read=AsyncMock(side_effect=RuntimeError('unavailable'))
        self.assertEqual(self.tick(action('inspect'))['status'],'tool_error')
        self.assertFalse(self.ledger.view()['inspection_sources']);self.assertFalse(self.ledger.view()['strategies'])
    def test_preview_prediction_is_about_available_text_and_not_learning(self):
        good=self.tick(action('inspect',predict_success=True))
        self.assertTrue(good['observation_available']);self.assertFalse(good['prediction_error'])
        unexpected=self.tick(action('inspect',predict_success=False))
        self.assertTrue(unexpected['observation_available']);self.assertTrue(unexpected['prediction_error'])
        self.env.read=AsyncMock(return_value={'status':'empty','source_ref':R['source_ref'],'text':''})
        empty=self.tick(action('inspect',predict_success=True))
        self.assertFalse(empty['observation_available']);self.assertTrue(empty['prediction_error'])
        for result in [good,unexpected,empty]:
            self.assertFalse(result['met_need']);self.assertFalse(result['learning_assessed'])
        self.assertFalse(self.ledger.view()['strategies']);self.assertFalse(self.ledger.view()['inquiry_model'])
    def test_preview_of_book_does_not_advance_full_read_cursor(self):
        root=Path(self.tmp.name);books=root/'.runtime/books';books.mkdir(parents=True)
        cfg=root/'.runtime/qq-social';cfg.mkdir()
        (cfg/'config.json').write_text(json.dumps({'persona_user_id':'test','cognition_search_enabled':False}),encoding='utf-8')
        (books/'x-chunks.json').write_text(json.dumps({'book':'x','chunks':[{'text':TEXT},{'text':'第二段'}]}),encoding='utf-8')
        env=ReadingEnvironment(root,{'persona_user_id':'test'},self.ledger);env.persona=AsyncMock(return_value=[])
        with patch('alice_reading.fetch_bilibili',return_value=[]):
            resources,_=asyncio.run(env.observe())
            out=asyncio.run(CognitiveLoop(self.ledger,Model(action('inspect',resource_id=resources[0]['id'])),env).tick())
            self.assertEqual(out['status'],'observed')
            after,_=asyncio.run(env.observe())
        self.assertEqual(resources[0]['id'],after[0]['id'])
