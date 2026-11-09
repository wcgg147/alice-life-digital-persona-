import copy
import tempfile
import unittest
import asyncio
import time
from pathlib import Path
from channel import Gate, Store, OneBot


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.config = {'enabled': True, 'allowed_groups': ['111']}
        self.gate = Gate(self.config, self.store)
        self.i = 0

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def event(self, actor=2, at=True, **kw):
        self.i += 1
        e = {'post_type': 'message', 'message_type': 'group', 'group_id': 111,
             'user_id': actor, 'self_id': 1, 'message_id': self.i, 'time': 1000,
             'message': ([{'type': 'at', 'data': {'qq': '1'}}] if at else []) +
                        [{'type': 'text', 'data': {'text': '你好'}}]}
        e.update(kw)
        return e

    def test_unrelated_group_has_zero_admissions(self):
        self.assertIsNone(self.gate.admit(self.event(group_id=999), 1000))
        self.assertIsNone(self.gate.admit(self.event(at=False), 1000))

    def test_strangers_stay_silent_while_the_speaker_continues(self):
        self.assertIsNotNone(self.gate.admit(self.event(), 1000))
        # 第三个人随便聊：不打断这条线，也不进模型
        self.assertIsNone(self.gate.admit(self.event(actor=3, at=False), 1001))
        # 原来那个人接着说话：仍在窗口里，不用重新 @
        work = self.gate.admit(self.event(at=False), 1002)
        self.assertIsNotNone(work)
        self.assertEqual(work['trigger'], 'conversation_continuation')
        # 窗口过期就散场
        self.assertIsNone(self.gate.admit(self.event(at=False, time=1200), 1200))

    def test_replying_into_the_thread_needs_no_mention(self):
        first = self.event(actor=2, at=True)
        self.assertIsNotNone(self.gate.admit(first, 1000))
        reply_event = self.event(actor=3, at=False, time=1030, message=[
            {'type': 'reply', 'data': {'id': str(first['message_id'])}},
            {'type': 'text', 'data': {'text': '我也想问这个'}}])
        work = self.gate.admit(reply_event, 1030)
        self.assertIsNotNone(work)
        self.assertEqual(work['trigger'], 'conversation_continuation')

    def test_other_members_plain_chat_never_enters(self):
        self.assertIsNotNone(self.gate.admit(self.event(actor=2, at=True), 1000))
        for i in range(5):
            self.assertIsNone(self.gate.admit(self.event(actor=7 + i, at=False, time=1005 + i), 1005 + i))

    def test_group_continuous_dialogue_without_repeat_mention(self):
        work1 = self.gate.admit(self.event(actor=2, at=True), 1000)
        self.assertIsNotNone(work1)
        self.assertEqual(work1['trigger'], 'mention')
        work2 = self.gate.admit(self.event(actor=2, at=False, time=1020), 1020)
        self.assertIsNotNone(work2)
        self.assertEqual(work2['trigger'], 'conversation_continuation')
        work3 = self.gate.admit(self.event(actor=2, at=False, time=1040), 1040)
        self.assertIsNotNone(work3)
        self.assertEqual(work3['trigger'], 'conversation_continuation')
        interruption = self.gate.admit(self.event(actor=3, at=False, time=1050), 1050)
        self.assertIsNone(interruption)
        # 别人插一句不打断这条线：原来那个人不用重新 @
        work4 = self.gate.admit(self.event(actor=2, at=False, time=1060), 1060)
        self.assertIsNotNone(work4)
        self.assertEqual(work4['trigger'], 'conversation_continuation')

    def test_quote_alice_needs_no_mention(self):
        source=self.event()
        self.store.message('source',source,'assistant','own reply',42,True)
        event=self.event(at=False)
        event['message'].insert(0,{'type':'reply','data':{'id':'42'}})
        work=self.gate.admit(event,1000)
        self.assertEqual(work['trigger'],'reply_to_alice')
        # Quoting Alice explicitly remains meaningful outside the old 120s window.
        event=self.event(at=False,time=1500)
        event['message'].insert(0,{'type':'reply','data':{'id':'42'}})
        self.assertIsNotNone(self.gate.admit(event,1500))

    def test_quotes_of_other_people_or_other_groups_stay_silent(self):
        source=self.event()
        self.store.message('source',source,'user','someone else',41,True)
        self.store.message('source',self.event(group_id=222),'assistant','own other group',42,True)
        for message_id in ['41','42','999']:
            event=self.event(at=False)
            event['message'].insert(0,{'type':'reply','data':{'id':message_id}})
            self.assertIsNone(self.gate.admit(event,1000))

    def test_other_mentions_do_not_trigger_continuation(self):
        self.gate.admit(self.event(), 1000)
        e = self.event(at=False)
        e['message'].insert(0, {'type': 'at', 'data': {'qq': '3'}})
        self.assertIsNone(self.gate.admit(e, 1000))

    def test_stop_and_natural_stop_close_pending_replies(self):
        self.gate.admit(self.event(), 1000)
        e = self.event(at=False)
        e['message'] = [{'type': 'text', 'data': {'text': '先不聊了'}}]
        self.assertIsNone(self.gate.admit(e, 1000))
        self.assertIsNone(self.gate.admit(self.event(at=False), 1000))
        self.assertEqual(self.gate.stopped[('111','2')],1000)
        self.gate.admit(self.event(),1001)
        e=self.event(at=False)
        e['message']=[{'type':'text','data':{'text':'你可以退下了'}}]
        self.assertIsNone(self.gate.admit(e,1002))
        self.assertEqual(self.gate.stopped[('111','2')],1002)
        # A new @ does not revive replies queued before stop.
        self.assertIsNotNone(self.gate.admit(self.event(),1003))
        self.assertEqual(self.gate.stopped[('111','2')],1002)

    def test_self_backlog_disabled_and_cq_string(self):
        for e in [self.event(actor=1), self.event(time=900), self.event(time=float('nan')),
                  self.event(time='bad'), self.event(message=[None]), self.event(message='[CQ:at,qq=1]你好')]:
            self.assertIsNone(self.gate.admit(e, 1000))
        self.config['enabled'] = False
        self.assertIsNone(self.gate.admit(self.event(), 1000))

    def test_dedup_survives_restart(self):
        e = self.event()
        self.assertIsNotNone(self.gate.admit(e, 1000))
        self.store.db.close()
        self.store = Store(self.path)
        self.gate = Gate(self.config, self.store)
        self.assertIsNone(self.gate.admit(e, 1000))

    def test_budget_is_persistent_and_failures_do_not_refund(self):
        self.store.reserve(1)
        self.store.db.close()
        self.store = Store(self.path)
        with self.assertRaisesRegex(RuntimeError, 'daily_model_call_cap'):
            self.store.reserve(1)

    def test_histories_are_isolated(self):
        e = self.event()
        self.store.message('qq-111-2', e, 'user', 'unique', 1, True)
        self.assertEqual(len(self.store.history(111,2)), 1)
        self.assertEqual(self.store.history(111,3), [])
        self.assertEqual(self.store.history(222,2), [])


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'state.sqlite3')
        self.calls = []
        owner = self
        class Brain:
            async def reply(inner, work, history):
                owner.calls.append(work)
                return '[CQ:at,qq=all] literal text', 'qq-conversation'
        self.bot = OneBot({'enabled':True,'allowed_groups':['111']}, self.store, Brain())
        self.bot.ws = object()
        self.sent = []
        async def api(action, params):
            self.sent.append((action,params))
            return {'message_id':42}
        self.bot.api = api
        self.task = asyncio.create_task(self.bot.worker())

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task,return_exceptions=True)
        self.store.db.close()
        self.temp.cleanup()

    def work(self):
        return {'received_at':time.time(),'group':'111','actor':'2','text':'hi',
                'event':{'group_id':111,'user_id':2,'message_id':3}}

    async def test_outbound_is_literal_text_and_receipted(self):
        await self.bot.queue.put(self.work())
        await asyncio.wait_for(self.bot.queue.join(),1)
        self.assertEqual(len(self.calls),1)
        segments = self.sent[0][1]['message']
        self.assertEqual(segments,[{'type':'text','data':{'text':'[CQ:at,qq=all] literal text'}}])
        self.assertEqual(len(self.store.history(111,2)),2)

    async def test_revoked_group_does_not_call_brain(self):
        self.bot.config['allowed_groups']=[]
        await self.bot.queue.put(self.work())
        await asyncio.wait_for(self.bot.queue.join(),1)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.sent,[])

    async def test_native_face_is_sent_separately_without_forced_quote(self):
        async def reply(work,history):
            return {'text':'给你','segments':[{'type':'face','data':{'id':'412'}}],
                    'display':'[QQ表情：/开心]','quote_current':False,'text_placement':'separate'},'native-face'
        self.bot.brain.reply=reply
        await self.bot.queue.put(self.work())
        await asyncio.wait_for(self.bot.queue.join(),1)
        self.assertEqual([[s['type'] for s in p['message']] for _,p in self.sent],[['text'],['face']])
        self.assertIn('[QQ表情：/开心]',self.store.history(111,2)[1]['content'])

    async def test_quote_is_optional_and_can_be_selected(self):
        async def reply(work,history):
            return {'text':'回这条','quote_current':True},'quote-selected'
        self.bot.brain.reply=reply
        await self.bot.queue.put(self.work())
        await asyncio.wait_for(self.bot.queue.join(),1)
        self.assertEqual(self.sent[0][1]['message'][0],{'type':'reply','data':{'id':'3'}})

    async def test_stopped_queue_does_not_call_brain(self):
        work=self.work()
        self.bot.gate.stopped[('111','2')]=work['received_at']+1
        await self.bot.queue.put(work)
        await asyncio.wait_for(self.bot.queue.join(),1)
        self.assertEqual(self.calls,[])

    async def test_missing_receipt_is_not_retried_or_recorded_as_spoken(self):
        async def no_receipt(action,params):
            self.sent.append((action,params))
            return {}
        self.bot.api=no_receipt
        await self.bot.queue.put(self.work())
        await asyncio.wait_for(self.bot.queue.join(),1)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(len(self.calls),1)
        self.assertEqual([r['role'] for r in self.store.history(111,2)],['user'])


class UserTermTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_terms_are_scoped_and_disableable(self):
        self.store.db.execute(
            "INSERT INTO qq_user_terms(scope,term,meaning,source_message_id,status,created_at) "
            "VALUES(?,?,?,?, 'active', ?)", ('private:1', 'a味', 'AI味', 'm1', time.time()))
        self.store.db.execute(
            "INSERT INTO qq_user_terms(scope,term,meaning,source_message_id,status,created_at) "
            "VALUES(?,?,?,?, 'active', ?)", ('private:2', 'a味', '别人的说法', 'm2', time.time()))
        self.store.db.commit()
        self.assertEqual([t['term'] for t in self.store.user_terms('private:1')], ['a味'])
        self.store.db.execute("UPDATE qq_user_terms SET status='disabled' WHERE scope='private:1'")
        self.store.db.commit()
        self.assertEqual(self.store.user_terms('private:1'), [])


if __name__ == '__main__':
    unittest.main()
