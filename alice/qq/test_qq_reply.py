import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from channel import OneBot, Store
from qq_reply import expression_invitation, reply_parts, claims_attachment
from alice import Alice


class PresentationTests(unittest.TestCase):
    def test_natural_mood_invitations_and_explicit_requests(self):
        for text in ['有啥表情包能表达你的心情','哪张动图适合你现在的状态','你的心情用什么表情比较合适',
                     '发个表情包','来张动态表情','把这个表情发一下',
                     '分享分享你的新图','发张图片看看','换一张图','能换个表情包吗']:
            with self.subTest(text=text):self.assertTrue(expression_invitation(text))

    def test_explanations_and_negations_are_not_send_requests(self):
        for text in ['这个表情包是什么意思','介绍一下GIF怎么制作','为什么大家用这张表情',
                     '不要发表情包','不用再给我发表情','别用表情包','你开心吗',
                     '给我看看这个地图','这张图怎么画']:
            with self.subTest(text=text):self.assertFalse(expression_invitation(text))

    def test_claims_of_sending_are_recognised(self):
        for text in ['这张发你了','给你发一张','所以鸽子那张发你了','图发出去了','这就发']:
            with self.subTest(text=text):self.assertTrue(claims_attachment(text))

    def test_denials_and_questions_are_not_send_claims(self):
        for text in ['我不发了','没发出去','图发不出去','你还没收到吗','这张好看吗','我没收到图']:
            with self.subTest(text=text):self.assertFalse(claims_attachment(text))

    def test_long_single_thought_is_not_chopped_by_length_or_punctuation(self):
        text='这件事确实有一点复杂。'*30
        self.assertEqual(len(reply_parts(text)),1)
        self.assertEqual(reply_parts(text)[0]['spoken'],text)

    def test_model_chosen_paragraphs_split_without_dropping_excess_content(self):
        filler='这一段用来把气泡撑到合并阈值以上，重复也不丢内容。'*2
        parts=reply_parts(f'第一点。{filler}\n\n顺便想到另一点。{filler}\n\n第三点。{filler}\n\n最后这点。{filler}')
        self.assertEqual(len(parts),3)
        self.assertTrue(parts[-1]['spoken'].startswith('第三点。'))
        self.assertIn('最后这点。', parts[-1]['spoken'])

    def test_short_multi_paragraph_reply_is_one_bubble(self):
        # 真人一条说完就完了：短回复不按空行拆成两三条完整句（那是最像 AI 的地方）
        parts=reply_parts('懂，刚看完翻拍又回头补原版，容易看得没劲。\n\n不过原版更偏那种冷冰冰的都市感。\n\n要不先放放。')
        self.assertEqual(len(parts),1)
        self.assertIn('不过原版', parts[0]['spoken'])

    def test_expression_can_stand_alone_or_be_separate_from_text(self):
        face={'type':'face','data':{'id':'14'}}
        self.assertEqual(reply_parts({'text':'','segments':[face]})[0]['segments'],[face])
        parts=reply_parts({'text':'就这个心情。','segments':[face],'text_placement':'separate'})
        self.assertEqual([[s['type'] for s in p['segments']] for p in parts],[['text'],['face']])
        together=reply_parts({'text':'就这个心情。','segments':[face],'text_placement':'together'})
        self.assertEqual([s['type'] for s in together[0]['segments']],['text','face'])


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'channel.sqlite3'
        self.store=Store(self.path)
        self.bot=OneBot({'enabled':True,'allowed_groups':['111']},self.store,object())
        self.bot.ws=object();self.bot.api=AsyncMock(side_effect=[{'message_id':42},{'message_id':43},{'message_id':44}])
        self.work={'group':'111','actor':'2','received_at':time.time(),'text':'聊聊',
                   'event':{'group_id':111,'user_id':2,'message_id':3}}

    async def asyncTearDown(self):
        self.store.db.close();self.temp.cleanup()

    async def test_every_bubble_is_quotable_but_history_keeps_one_logical_turn(self):
        await self.bot.send_reply(self.work,{'text':'一句。\n\n另一句。','segments':[{'type':'face','data':{'id':'14'}}],
                                            'display':'[QQ表情：微笑]','quote_current':True,'text_placement':'separate'},'turn')
        calls=self.bot.api.await_args_list
        self.assertEqual(calls[0].args[1]['message'][0]['type'],'reply')
        self.assertNotIn('reply',[s['type'] for s in calls[1].args[1]['message']])
        self.assertEqual([r['role'] for r in self.store.history(111,2)],['user','assistant'])
        self.store.db.close();self.store=Store(self.path)
        for mid in (42,43,44):
            self.assertTrue(self.store.is_own_message(111,mid))
            self.assertFalse(self.store.is_own_message(222,mid))

    async def test_partial_send_failure_records_only_acknowledged_content(self):
        self.bot.api=AsyncMock(side_effect=[{'message_id':42},RuntimeError('send_failed')])
        long_a='已发出的第一句。'+'这一段用来把气泡撑长，确保仍然分条发送。'*8
        long_b='没发出的第二句。'+'这一段同样用来把气泡撑长。'*8
        with self.assertRaises(RuntimeError):
            await self.bot.send_reply(self.work,f'{long_a}\n\n{long_b}','turn')
        history=self.store.history(111,2)
        self.assertEqual(history[-1]['content'],long_a)
        self.assertEqual(self.bot.api.await_count,2)

    async def test_permission_is_checked_between_bubbles(self):
        async def send(action,params):
            self.bot.config['enabled']=False
            return {'message_id':42}
        self.bot.api=AsyncMock(side_effect=send)
        long_a='第一句。'+'这一段用来把气泡撑长，确保会分成两条发送。'*8
        long_b='第二句。'+'这一段同样用来把气泡撑长。'*8
        await self.bot.send_reply(self.work,f'{long_a}\n\n{long_b}','turn')
        self.bot.api.assert_awaited_once()
        self.assertEqual(self.store.history(111,2)[-1]['content'],long_a)


class InvitationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_persona_can_choose_words_instead_of_an_expression(self):
        brain=Alice.__new__(Alice)
        brain.tools=type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:[]})()
        brain.model_message=AsyncMock(return_value={'content':'我现在不想发表情包，想把这件事说完。'})
        state={'catalog':{'saved:real':{}},'selected':None}
        result=await brain.run_tools([],{'text':'有啥表情包能表达你的心情'},state)
        self.assertIn('不想',result)
        brain.model_message.assert_awaited_once()

    async def test_text_description_of_mood_gets_one_bounded_action_repair(self):
        brain=Alice.__new__(Alice)
        state={'catalog':{'saved:real':{}},'selected':None}
        definitions=[{'type':'function','function':{'name':'qq_choose_expression'}}]
        async def choose(name,args,work,current):
            current['selected']={'label':'real'}
            return {'status':'selected_for_final_reply'}
        brain.tools=type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:definitions,
                                    'execute':AsyncMock(side_effect=choose)})()
        brain.model_message=AsyncMock(side_effect=[{'content':'就那张摆烂了吧。'},
            {'tool_calls':[{'id':'choose','function':{'name':'qq_choose_expression','arguments':'{"expression_id":"saved:real"}'}}]},
            {'content':''}])
        text=await brain.run_tools([],{'text':'有啥表情包能表达你的心情'},state)
        self.assertEqual(text,'')
        self.assertIsNotNone(state['selected'])
        self.assertEqual(brain.model_message.await_count,3)

class StripTimePrefixTest(unittest.TestCase):
    def test_strips_clock_prefix(self):
        from qq_reply import strip_time_prefix
        self.assertEqual(strip_time_prefix('[21:05] 行，那我不认了。'), '行，那我不认了。')

    def test_strips_today_prefix(self):
        from qq_reply import strip_time_prefix
        self.assertEqual(strip_time_prefix('[今天 15:46] 啊？什么味？'), '啊？什么味？')

    def test_keeps_non_time_brackets(self):
        from qq_reply import strip_time_prefix
        self.assertEqual(strip_time_prefix('[QQ表情：无语] 行'), '[QQ表情：无语] 行')
        self.assertEqual(strip_time_prefix('【不是时间】留着'), '【不是时间】留着')


class PerformativeRepairTest(unittest.TestCase):
    def test_detects_apology_and_commitment(self):
        from qq_reply import performative_repair
        for text in ['是挺无语的，我认。', '行，是我跳太远了。', '我记下了，下次直接嗯。', '行，不认了。']:
            self.assertTrue(performative_repair(text), text)

    def test_keeps_normal_reply(self):
        from qq_reply import performative_repair
        for text in ['这兔子流汗流得挺传神。', '行，那这页翻过去。', '没上过班，也没在上学。']:
            self.assertFalse(performative_repair(text), text)
