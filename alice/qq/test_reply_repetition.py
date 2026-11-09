import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from alice import Alice
from channel import Store


class RepetitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.db')
        self.brain=Alice.__new__(Alice);self.brain.store=self.store;self.brain.tools=None
        self.history=[{'role':'assistant','content':'你要嫌我贫，我也可以闭嘴。'},
                      {'role':'user','content':'一套一套的'},
                      {'role':'assistant','content':'你要真嫌，我收着点就是了。'},
                      {'role':'user','content':'你讲话一直这么肉麻的吗，还自我感觉良好'}]

    async def asyncTearDown(self):
        self.store.db.close();self.tmp.cleanup()

    async def test_reported_draft_gets_one_revision_without_claiming_it_was_spoken(self):
        self.brain.model_message=AsyncMock(side_effect=[{'content':'直白是真的，装来装去累得慌。你要嫌肉麻，我下次就少绕一点，直接说。'},
                                                       {'content':'刚才贫过头了。'}])
        result=await self.brain.run_tools(self.history,{'event':{'message_id':1}},None)
        self.assertEqual(result,'刚才贫过头了。')
        self.assertEqual(self.brain.model_message.await_count,2)
        self.assertIn('未发送草稿',self.brain.model_message.await_args.args[0][-1]['content'])

    async def test_revision_does_not_loop_forever_or_force_length(self):
        text='你要嫌肉麻，我下次就少绕一点，直接说。'
        self.brain.model_message=AsyncMock(return_value={'content':text})
        self.assertEqual(await self.brain.run_tools(self.history,{},None),text)
        self.assertEqual(self.brain.model_message.await_count,2)
