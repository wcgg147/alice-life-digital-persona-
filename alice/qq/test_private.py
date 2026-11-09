import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from channel import Store, Gate, OneBot


class PrivateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'private.sqlite3')
        self.config = {'enabled': True, 'allowed_groups': ['111'], 'allowed_private_users': ['2']}
        self.brain = type('Brain', (), {'reply': AsyncMock(return_value=('你好呀', 'qq-private-test'))})()
        self.bot = OneBot(self.config, self.store, self.brain)
        self.bot.ws, self.bot.account = object(), '1'
        self.bot.api = AsyncMock(return_value={'message_id': 99})

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def event(self, mid=3, actor=2, age=0, text='你好'):
        return {'post_type':'message', 'message_type':'private', 'user_id':actor,
                'self_id':1, 'message_id':mid, 'time':time.time()-age,
                'message':[{'type':'text','data':{'text':text}}]}

    async def test_allowed_friend_needs_no_mention_and_is_deduplicated(self):
        e = self.event()
        work = self.bot.gate.admit(e)
        self.assertEqual(work['group'], 'private:2')
        self.assertEqual(work['trigger'], 'private')
        self.assertNotIn('group_id', e)
        self.assertIsNone(self.bot.gate.admit(e))

    async def test_other_friend_self_and_old_messages_stay_silent(self):
        for e in [self.event(actor=9), self.event(actor=1), self.event(age=60)]:
            self.assertIsNone(self.bot.gate.admit(e))
        self.brain.reply.assert_not_awaited()

    async def test_private_sends_to_friend_and_keeps_group_history_separate(self):
        work = self.bot.gate.admit(self.event())
        await self.bot.queue.put(work)
        task = asyncio.create_task(self.bot.worker())
        try:
            await asyncio.wait_for(self.bot.queue.join(), 1)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.bot.api.await_args.args, ('send_private_msg', {'user_id':2, 'message':[{'type':'text','data':{'text':'你好呀'}}]}))
        self.assertEqual(len(self.store.history('private:2',2)),2)
        self.assertEqual(self.store.history(111,2),[])

    async def test_revoked_private_permission_blocks_queued_model_and_send(self):
        work = self.bot.gate.admit(self.event())
        self.config['allowed_private_users'] = []
        await self.bot.queue.put(work)
        task = asyncio.create_task(self.bot.worker())
        try:
            await asyncio.wait_for(self.bot.queue.join(),1)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.brain.reply.assert_not_awaited()
        self.bot.api.assert_not_awaited()

    async def test_native_history_poll_admits_new_owner_only_once(self):
        self.bot.api.return_value = {'messages':[self.event(), self.event(mid=4,actor=1), self.event(mid=5,actor=9), self.event(mid=6,age=80)]}
        await self.bot.poll_private_once()
        await self.bot.poll_private_once()
        self.assertEqual(self.bot.queue.qsize(),1)
        self.brain.reply.assert_not_awaited()

    async def test_stop_cancels_private_work_and_new_message_can_resume(self):
        work = self.bot.gate.admit(self.event())
        self.assertIsNone(self.bot.gate.admit(self.event(mid=4,text='先不聊了')))
        self.assertFalse(self.bot.work_permitted(work))
        self.assertIsNotNone(self.bot.gate.admit(self.event(mid=5,text='回来聊聊')))
