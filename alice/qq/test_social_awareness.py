import base64
import io
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock,patch

from channel import Store,OneBot,Gate
from social_memory import SocialMemory
from qq_tools import QQTools
from qq_reply import reply_parts
from voice_messages import reserve_call,finish_call,pcm_for_asr


class AwarenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.db')
        self.cfg={'enabled':True,'expected_qq':'1','allowed_groups':['111','222'],
                  'allowed_private_users':['2','3'],'account_owner_qq':'2'}
        self.memory=SocialMemory(self.cfg,self.store)
        self.work={'key':'test','group':'private:2','actor':'2','received_at':time.time(),
                   'text':'群里不是有人艾特你吗','event':{'message_id':999,'self_id':1,'user_id':2,'message_type':'private','group_id':'private:2','message':[]}}

    async def asyncTearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def event(self,mid,text='',segments=None,scope='111',actor=4):
        return {'post_type':'message','message_type':'group','self_id':1,'group_id':scope,'user_id':actor,
                'message_id':mid,'time':time.time(),'message':segments if segments is not None else [{'type':'text','data':{'text':text}}]}

    async def test_owner_can_recall_group_speakers_without_prior_owner_participation(self):
        self.memory.observe(self.event(1,'今晚打守望先锋吗'))
        self.work['text']='群里有人说打守望先锋吗'
        packet=self.memory.recall(self.work)
        self.assertEqual(packet['attributed_observations'][0]['speaker_qq'],'4')
        self.assertEqual(packet['attributed_observations'][0]['scope'],'111')
        self.assertNotIn('111',self.memory._shared_scopes('private:3','3'))

    async def test_image_only_and_mention_only_are_retained_without_invented_text(self):
        digest='a'*32
        self.memory.observe(self.event(1,segments=[{'type':'image','data':{'file':digest+'.gif'}}]))
        ping=self.event(2,segments=[{'type':'at','data':{'qq':'1'}}])
        self.memory.observe(ping)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_image_occurrences').fetchone()[0],1)
        self.assertEqual(self.memory.recall(self.work)['observed_mentions'][0]['message_id'],'2')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_observed_messages').fetchone()[0],0)
        self.assertIsNotNone(Gate(self.cfg,self.store).admit(ping))

    async def test_short_meme_has_old_source_and_repetition_does_not_invent_meaning(self):
        self.memory.observe(self.event(1,'ccb'));self.memory.observe(self.event(2,'ccb'))
        self.work['text']='ccb'
        packet=self.memory.recall(self.work)
        self.assertEqual(packet['expression_exposure'][0]['earlier_occurrences'],2)
        self.assertEqual(packet['attributed_observations'][0]['quotation'],'ccb')

    async def test_private_reaction_to_public_image_stays_in_private(self):
        digest='a'*32
        evidence=[{'scope':'111','message_id':'1','digest':digest}]
        self.memory.remember_visual_reply(self.work,evidence,[(5,'这张让我想起私聊的秘密')])
        self.assertEqual(self.memory.visual_context('111',digest,'2')['previous_reactions'],[])
        self.assertIn('秘密',self.memory.visual_context('private:2',digest,'2')['previous_reactions'][0]['text'])

    async def test_native_group_read_can_observe_image_and_never_sends(self):
        event=self.event(1,segments=[{'type':'at','data':{'qq':'1'}},{'type':'image','data':{'file':'a'*32+'.gif'}}])
        self.memory.observe(event)
        api=AsyncMock(side_effect=[{'messages':[event]},{'url':'https://gchat.qpic.cn/test'}])
        tools=QQTools(self.cfg,self.store,api,lambda w:True);tools.social_memory=self.memory
        state={'images':{},'catalog':{},'context':[]}
        with patch('qq_tools.download_media',AsyncMock(return_value=(b'GIF89a actual','image/gif'))):
            result=await tools.execute('qq_read_group_history',{'group':'111'},self.work,state)
        self.assertEqual(result['status'],'observed')
        self.assertTrue(result['image_observed'])
        self.assertTrue(state['observation_previews'])
        self.assertEqual([c.args[0] for c in api.await_args_list],['get_group_msg_history','get_image'])
        result=await tools.execute('qq_read_group_history',{'group':'333'},self.work,state)
        self.assertNotEqual(result['status'],'observed')


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AwarenessTests.asyncSetUp
    asyncTearDown=AwarenessTests.asyncTearDown
    def wav(self,silent=False):
        out=io.BytesIO()
        with wave.open(out,'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
            f.writeframes((b'\x00\x00' if silent else b'\x80\x10')*1600)
        return out.getvalue()

    async def test_voice_quota_also_reserves_global_budget_and_failed_calls_count(self):
        self.cfg['voice_calls_per_day']=1
        call=reserve_call(self.store,self.cfg,'synthesize','test');finish_call(self.store,call,False)
        self.assertEqual(self.store.db.execute('SELECT calls FROM budget').fetchone()[0],1)
        with self.assertRaisesRegex(RuntimeError,'voice_daily_cap'):reserve_call(self.store,self.cfg,'transcribe','test')

    async def test_silence_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'silent_voice'):pcm_for_asr(self.wav(True))

    async def test_reported_voice_duplicate_is_sent_once_and_distinct_caption_survives(self):
        voice='行吧，之前说发不了语音，那是我嘴硬。现在你听着——声音嘛，就这样，普通话还算标准。别指望我多温柔，我平时说话就这个调。'
        reply={'text':'行吧，之前还跟你说发不了语音，是我嘴硬。这回你听着就是了。\n\n别指望我说得多温柔，平时就这个调。',
               'voice_text':voice,'segments':[{'type':'record','data':{'file':'real'}}]}
        self.assertEqual([p['segments'][0]['type'] for p in reply_parts(reply)],['record'])
        reply['text']='试试这个声音怎么样？'
        self.assertEqual([p['segments'][0]['type'] for p in reply_parts(reply)],['text','record'])

    async def test_voice_preparation_is_deferred_to_acknowledged_send(self):
        self.cfg['voice_messages_enabled']=True
        api=AsyncMock();tools=QQTools(self.cfg,self.store,api,lambda w:True)
        state={'catalog':{},'images':{},'context':[]}
        with patch('voice_messages.synthesize',AsyncMock(return_value={'audio':self.wav(),'model':'project','voice':'approved'})):
            result=await tools.execute('qq_voice_reply',{'text':'我在呢'},self.work,state)
        self.assertEqual(result['status'],'prepared_for_final_reply')
        self.assertEqual(state['voice_reply']['segment']['type'],'record');api.assert_not_awaited()
        record=state['voice_reply']['segment'];image={'type':'image','data':{'file':'actual'}}
        parts=reply_parts({'text':'一小句','segments':[record,image],'attachment_displays':['[QQ语音：我在呢]','[QQ表情：开心]']})
        self.assertEqual([[s['type'] for s in p['segments']] for p in parts],[['text'],['record'],['image']])
        self.assertNotIn('表情',parts[1]['spoken'])

    async def test_voice_input_falls_back_to_project_asr_and_keeps_provenance(self):
        self.cfg['voice_messages_enabled']=True
        bot=OneBot(self.cfg,self.store,object());bot.ws=object()
        bot.api=AsyncMock(side_effect=[RuntimeError('native_unavailable'),{'base64':base64.b64encode(self.wav()).decode()}])
        self.work['event']['message']=[{'type':'record','data':{'file':'a.amr'}}]
        with patch('voice_messages.transcribe_wav',AsyncMock(return_value={'text':'你在吗','source':'project_voice_asr'})):
            work=await bot.prepare_voice_input(self.work)
        self.assertEqual(work['text'],'你在吗')
        self.assertFalse(work['event']['voice_input']['human_verified'])
        self.memory.observe(work['event'],scope=work['group'])
        self.assertEqual(self.store.db.execute('SELECT source_kind FROM qq_observed_messages').fetchone()[0],'qq_voice_transcription_unreviewed')
