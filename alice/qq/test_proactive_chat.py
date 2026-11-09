import asyncio,json,sys,tempfile,time,types,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from channel import OneBot,Store


class ProactiveConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'state.sqlite3')
        self.config={'enabled':True,'allowed_groups':['111','222'],'proactive_groups':['111']}
        self.brain=types.SimpleNamespace(facts=AsyncMock(return_value=[]),
            completion=AsyncMock(return_value=json.dumps({'speak':True,'reason':'想接一句'})),
            reply=AsyncMock(return_value=('好笑','testconv')))
        self.bot=OneBot(self.config,self.store,self.brain);self.bot.ws=object();self.bot.account='9'
        self.bot.api=AsyncMock(return_value={'message_id':888});self.chat=self.bot.proactive
        self.memory=types.ModuleType('memory.companion_persona')
        self.memory.build_persona_packet=lambda *a,**k:{}
        self.memory.supported_growth_fact_ids=lambda facts:set()

    async def asyncTearDown(self):self.store.db.close();self.tmp.cleanup()

    def event(self,**changes):
        event={'post_type':'message','message_type':'group','group_id':111,'user_id':2,'self_id':9,
            'message_id':3,'time':time.time(),'message':[{'type':'text','data':{'text':'笑死'}}]}
        event.update(changes);return event

    def offer(self,event=None):
        event=event or self.event();self.bot.social_memory.observe(event);self.chat.offer(event)
        if '111' in self.chat.latest:self.chat.latest['111']['received_at']-=6
        return event

    async def consider(self):
        with patch.dict(sys.modules,{'memory.companion_persona':self.memory}):await self.chat.consider_latest()

    def status(self):return self.store.db.execute('SELECT status FROM qq_proactive_choices').fetchone()[0]

    async def test_ungranted_group_self_stale_malformed_and_directed_are_not_sampled(self):
        for event in (self.event(group_id=222),self.event(user_id=9),self.event(time=time.time()-100),
                self.event(time=float('nan')),self.event(message=[None]),
                self.event(message=[{'type':'at','data':{'qq':'9'}}])):self.chat.offer(event)
        self.assertFalse(self.chat.latest);await self.consider()
        self.brain.completion.assert_not_awaited();self.bot.api.assert_not_awaited()

    async def test_real_gate_still_requires_mention_or_actual_quote(self):
        event=self.offer();self.assertIsNone(self.bot.gate.admit(event));self.assertIn('111',self.chat.latest)
        event=self.event(group_id=222);self.assertIsNone(self.bot.gate.admit(event))

    async def test_personality_can_choose_silence_without_generating_or_sending_a_reply(self):
        self.brain.completion.return_value=json.dumps({'speak':False,'reason':'继续看'})
        self.offer();await self.consider();self.assertEqual(self.status(),'quiet')
        self.brain.reply.assert_not_awaited();self.bot.api.assert_not_awaited()

    async def test_speak_uses_real_send_path_and_records_real_receipt_without_synthetic_mention(self):
        event=self.offer();await self.consider()
        self.assertEqual(self.status(),'completed');self.bot.api.assert_awaited_once()
        self.assertEqual(self.bot.api.await_args.args[0],'send_group_msg')
        self.assertEqual(str(self.bot.api.await_args.args[1]['group_id']),'111')
        self.assertTrue(self.store.is_own_message('111',888))
        self.assertFalse(any(s['type']=='at' for s in event['message']))
        self.assertEqual(self.brain.reply.await_args.args[0]['trigger'],'proactive')

    async def test_revoke_before_model_is_free(self):
        self.offer();self.config['proactive_groups']=[];await self.consider()
        self.brain.completion.assert_not_awaited();self.bot.api.assert_not_awaited()

    async def test_revoke_during_choice_prevents_generation_and_send(self):
        async def revoked(*a,**k):
            self.config['proactive_groups']=[];return json.dumps({'speak':True,'reason':'想说'})
        self.brain.completion.side_effect=revoked;self.offer();await self.consider()
        self.assertEqual(self.status(),'cancelled');self.brain.reply.assert_not_awaited();self.bot.api.assert_not_awaited()

    async def test_existing_call_preserves_opportunity_without_spending_model_calls(self):
        from activity import claim,release
        self.offer();self.assertTrue(claim(self.store,'existing-call'))
        try:
            await self.consider();self.brain.completion.assert_not_awaited();self.assertIn('111',self.chat.latest)
        finally:release(self.store,'existing-call')

    async def test_dequeued_direct_turn_still_takes_priority(self):
        self.offer();self.bot.foreground_work={}
        await self.consider();self.brain.completion.assert_not_awaited()

    async def test_real_worker_dequeued_direct_message_keeps_proactive_waiting(self):
        from activity import claim,release
        self.offer();direct=self.event(message_id=4)
        direct['message'].insert(0,{'type':'at','data':{'qq':'9'}})
        self.bot.queue.put_nowait(self.bot.gate.admit(direct))
        self.assertTrue(claim(self.store,'existing-call'))
        worker=asyncio.create_task(self.bot.worker())
        try:
            await asyncio.sleep(.01)
            self.assertTrue(self.bot.queue.empty());self.assertIsNotNone(self.bot.foreground_work)
            await self.consider();self.brain.completion.assert_not_awaited()
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True);release(self.store,'existing-call')

    async def test_real_worker_dequeues_new_mention_while_proactive_reply_is_being_generated(self):
        self.offer();worker=asyncio.create_task(self.bot.worker())
        async def new_mention(*a,**k):
            direct=self.event(message_id=4)
            direct['message'].insert(0,{'type':'at','data':{'qq':'9'}})
            self.bot.queue.put_nowait(self.bot.gate.admit(direct));await asyncio.sleep(.01)
            self.assertTrue(self.bot.queue.empty());self.assertIsNotNone(self.bot.foreground_work)
            return ('准备发的闲聊','testconv')
        self.brain.reply.side_effect=new_mention
        try:
            await self.consider();self.assertEqual(self.status(),'yielded_to_direct_message')
            self.bot.api.assert_not_awaited()
        finally:worker.cancel();await asyncio.gather(worker,return_exceptions=True)

    async def test_direct_queue_takes_priority_before_and_after_generation(self):
        self.offer();self.bot.queue.put_nowait({});await self.consider();self.brain.completion.assert_not_awaited()
        self.bot.queue.get_nowait();self.bot.queue.task_done()
        async def direct_arrival(*a,**k):self.bot.queue.put_nowait({});return ('好笑','testconv')
        self.brain.reply.side_effect=direct_arrival;await self.consider()
        self.assertEqual(self.status(),'yielded_to_direct_message');self.bot.api.assert_not_awaited()

    async def test_stop_requires_real_explicit_reopening_for_same_actor(self):
        self.bot.gate.stopped[('111','2')]=time.time()-1;self.chat.offer(self.event());self.assertFalse(self.chat.latest)
        direct=self.event(message_id=4);direct['message'].insert(0,{'type':'at','data':{'qq':'9'}})
        self.assertIsNotNone(self.bot.gate.admit(direct));self.chat.offer(self.event());self.assertIn('111',self.chat.latest)

    async def test_real_proactive_delivery_allows_unmentioned_stop(self):
        self.offer();await self.consider()
        stop=self.event(message_id=4,message=[{'type':'text','data':{'text':'先不聊了'}}])
        self.assertIsNone(self.bot.gate.admit(stop));self.assertIn(('111','2'),self.bot.gate.stopped)
        self.chat.offer(self.event(message_id=5));self.assertFalse(self.chat.latest)

    async def test_stop_during_choice_cancels_reply(self):
        async def stopped(*a,**k):
            self.bot.gate.stopped[('111','2')]=time.time();return json.dumps({'speak':True,'reason':'接话'})
        self.brain.completion.side_effect=stopped;self.offer();await self.consider()
        self.brain.reply.assert_not_awaited();self.bot.api.assert_not_awaited()

    async def test_same_choice_is_not_retried(self):
        self.offer();self.store.db.execute('INSERT INTO qq_proactive_choices VALUES (?,?,?,?,?,?)',
            ('111','3','2',time.time()-1000,'quiet','已看过'));self.store.db.commit()
        await self.consider();self.brain.completion.assert_not_awaited();self.assertEqual(self.status(),'quiet')

    async def test_runtime_revocation_between_bubbles_keeps_only_acknowledged_delivery(self):
        long_a='第一句'+'这一段用来把气泡撑长，确保会分成两条发送。'*8
        long_b='第二句'+'这一段同样用来把气泡撑长。'*8
        self.brain.reply.return_value=(f'{long_a}\n\n{long_b}','testconv')
        async def revoke_after_first(*a,**k):self.config['proactive_groups']=[];return {'message_id':888}
        self.bot.api.side_effect=revoke_after_first;self.offer();await self.consider()
        self.bot.api.assert_awaited_once();self.assertTrue(self.store.is_own_message('111',888))
        self.assertEqual(self.status(),'partial')
