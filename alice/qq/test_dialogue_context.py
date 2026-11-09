import tempfile
import unittest
from pathlib import Path
from channel import Store,Gate
from dialogue_context import clock_context,default_persona_facts,earlier_feedback,repeated_self_frame,specific_comparison_evidence,ungrounded_comparison_advice,current_message_materials


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'qq.db')

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def test_clock_is_beijing_and_does_not_depend_on_old_dialogue(self):
        clock=clock_context(0,now=1793253600)
        self.assertIn('2026-10-29T14:',clock['now'])
        self.assertTrue(clock['now'].endswith('+08:00'))
        self.assertEqual(clock['incoming_message_at'],'1970-01-01T08:00:00+08:00')
        self.assertNotIn('incoming_message_at',clock_context(float('nan')))

    def test_retrieved_question_cannot_occupy_the_current_message_position(self):
        current='你讲话一直这么肉麻的吗，还自我感觉良好'
        material=current_message_materials(current,[{'kind':'past','text':'你讲话都这么直白吗'}])
        self.assertTrue(material.endswith(current))
        self.assertLess(material.index('你讲话都这么直白吗'),material.index('[当前实际收到的QQ消息'))

    def test_single_reaction_is_not_a_fixed_trait_but_evidenced_trait_is_kept(self):
        facts=[{'fact_id':'one','kind':'trait','facet_key':'growth:expression:give-up'},
               {'fact_id':'supported','kind':'trait','facet_key':'growth:expression:teasing'},
               {'fact_id':'identity','kind':'identity','facet_key':'name'},
               {'fact_id':'taste','kind':'preference','facet_key':'growth:preference:meme'}]
        self.assertEqual([f['fact_id'] for f in default_persona_facts(facts,{'supported'})],['supported','identity','taste'])

    def test_feedback_survives_recent_window_and_does_not_cross_people_or_groups(self):
        event={'group_id':'private:2','user_id':'2'}
        self.store.message('turn',event,'assistant','CS比赛后来怎么样了',1,True)
        self.store.message('turn',event,'user','哪壶不开提哪壶',2,True)
        for i in range(30):self.store.message('turn',event,'user','随便聊聊'+str(i),i+3,True)
        records=earlier_feedback(self.store,'private:2','2')
        self.assertEqual(records[0]['source_message_id'],'2')
        self.assertIn('CS比赛',records[0]['preceding_alice_reply'])
        self.assertEqual(earlier_feedback(self.store,'111','2'),[])
        self.assertEqual(earlier_feedback(self.store,'private:3','3'),[])

    def test_all_four_groups_accept_only_mentions_and_receipted_quotes(self):
        groups=['20001','20002','20003','20004']
        gate=Gate({'enabled':True,'allowed_groups':groups},self.store)
        for group in groups:
            base={'self_id':1,'user_id':2,'group_id':group,'post_type':'message','message_type':'group','time':1000}
            text={'type':'text','data':{'text':'你好'}}
            self.assertIsNone(gate.admit({**base,'message_id':'ordinary','message':[text]},1000))
            self.assertIsNotNone(gate.admit({**base,'message_id':'ping','message':[{'type':'at','data':{'qq':'1'}},text]},1000))
            self.store.sent_receipt(group,'2','test',42)
            self.assertIsNotNone(gate.admit({**base,'message_id':'quote','message':[{'type':'reply','data':{'id':'42'}},text]},1000))

    def test_self_frame_is_recurrence_not_a_banned_phrase(self):
        text='你要嫌肉麻，我下次就少绕一点，直接说。'
        old=['你要嫌我贫，我也可以闭嘴。','你要真嫌，我收着点就是了。']
        self.assertIsNone(repeated_self_frame(text,old[:1]))
        self.assertEqual(repeated_self_frame(text,old[:1],'你讲话一直这么肉麻吗')['prior_occurrences'],1)
        self.assertEqual(repeated_self_frame(text,old)['prior_occurrences'],2)
        self.assertIsNone(repeated_self_frame(text,old,'你打算怎么调整？'))
        self.assertIsNone(repeated_self_frame('这段电影我还挺喜欢的。',old))

    def test_specific_models_do_not_inherit_expertise_from_unrelated_reading_or_own_reply(self):
        context=[{'kind':'scoped_qq_social_memory','attributed_observations':[{'role':'alice','quotation':'雨75我以前推荐过'}]}]
        result=specific_comparison_evidence('雨75和nut75哪个好',[{'source_kind':'reading','value_text':'看过手机拆机'}],[],context)
        self.assertEqual(result['models_without_sources'],['雨75','nut75'])
        self.assertIsNone(specific_comparison_evidence('这段电影你觉得好看吗',[],[],[]))
        result=specific_comparison_evidence('雨75和nut75哪个好',[],[{'text':'看过雨75拆机视频','ref':'actual-reading'}],[])
        self.assertEqual(result['models_without_sources'],['nut75'])

    def test_questions_are_not_model_knowledge_and_unknown_products_do_not_create_consultancy(self):
        context=[{'kind':'scoped_qq_social_memory','attributed_observations':[{'role':'person','quotation':'雨75和nut75哪个好？'}]}]
        evidence=specific_comparison_evidence('雨75和nut75哪个好',[],[],context)
        self.assertEqual(evidence['professional_task_accepted'],False)
        self.assertTrue(ungrounded_comparison_advice('按经验，填充、定位板和做工都要看。',evidence))
        self.assertTrue(ungrounded_comparison_advice('差别主要在结构和填充，做工如何最好去翻实拆。',evidence))
        self.assertFalse(ungrounded_comparison_advice('这两个我没研究过，刚看你发的图倒是挺好看。',evidence))
