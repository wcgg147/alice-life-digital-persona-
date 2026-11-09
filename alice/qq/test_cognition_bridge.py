import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock

from channel import Store
from cognition_bridge import Observer, source_event
from alice import Alice


class BridgeClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root/'.runtime').mkdir()
        (root/'.runtime/cognition-bridge.json').write_text('{}')
        self.store = Store(root/'channel.sqlite3')
        self.config = {'enabled':True,'product_root':str(root),'expected_qq':'1',
                       'allowed_groups':['100'],'allowed_private_users':['11']}

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def event(self, **extra):
        return {'post_type':'message','message_type':'private','self_id':1,'user_id':11,
                'message_id':123,'time':time.time(),'message':[{'type':'text','data':{'text':'你别总重复了'}}],**extra}

    def test_source_scope_and_voice_provenance(self):
        self.assertEqual(source_event(self.config,self.event())['scope'],'private:11')
        self.assertIsNone(source_event(self.config,self.event(user_id=99)))
        self.assertIsNone(source_event(self.config,self.event(self_id=99)))
        self.assertIsNone(source_event(self.config,self.event(message=[{'type':'text','data':None}])))
        voice=self.event(message=[{'type':'record','data':{'file':'voice'}},{'type':'text','data':{'text':'转写原句'}}])
        self.assertIsNone(source_event(self.config,voice))
        voice['voice_input']={'status':'transcribed','source':'unreviewed'}
        self.assertEqual(source_event(self.config,voice)['voice_source']['source'],'unreviewed')

    async def test_outbox_survives_observer_restart_and_deduplicates(self):
        first=Observer(self.config,self.store)
        first.offer(self.event());first.offer(self.event())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cognition_outbox').fetchone()[0],1)
        resumed=Observer(self.config,self.store)
        with patch('cognition_bridge.request',return_value={'event_id':'ack'}):
            task=asyncio.create_task(resumed.run())
            await asyncio.sleep(.1)
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        row=self.store.db.execute('SELECT status,attempts FROM cognition_outbox').fetchone()
        self.assertEqual(row,('delivered',1))

    async def test_failed_local_transfer_keeps_durable_source_and_permission_removal_cancels(self):
        observer=Observer(self.config,self.store);observer.offer(self.event())
        with patch('cognition_bridge.request',return_value={}):
            task=asyncio.create_task(observer.run());await asyncio.sleep(.1);task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(self.store.db.execute('SELECT status FROM cognition_outbox').fetchone()[0],'pending')
        self.config['allowed_private_users']=[]
        with self.store.db:self.store.db.execute('UPDATE cognition_outbox SET next_at=0')
        task=asyncio.create_task(observer.run());await asyncio.sleep(.1);task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(self.store.db.execute('SELECT status FROM cognition_outbox').fetchone()[0],'cancelled')

    def test_only_acknowledged_parts_and_context_revision_are_exported(self):
        observer=Observer(self.config,self.store)
        work={'group':'private:11','actor':'11','event':{'message_id':4},'_cognition_basis':{'revision':7,'policy_ids':['p']}}
        observer.offer_delivery(work,[(8,'已送达')])
        data=json.loads(self.store.db.execute('SELECT payload FROM cognition_outbox').fetchone()[0])
        self.assertEqual(data['text'],'已送达')
        self.assertEqual(data['receipts'],['8'])
        self.assertEqual(data['basis']['revision'],7)

    def test_recent_saved_messages_are_bounded_and_keep_original_source(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO messages(conv,group_id,actor,role,content,message_id,at,delivered,consolidated) VALUES ('c','private:11','11','user','你别总重复说那句话','saved-id',?,1,0)",(time.time()-30,))
        Observer(self.config,self.store)
        row=self.store.db.execute('SELECT payload FROM cognition_outbox').fetchone()
        value=json.loads(row[0])
        self.assertEqual(value['source_record']['kind'],'saved_qq_message')
        self.assertEqual(value['message_id'],'saved-id')
        self.assertLess(value['at'],time.time()-20)
        Observer(self.config,self.store)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cognition_outbox').fetchone()[0],1)

    async def test_actual_reply_uses_shared_packet_and_current_input_after_history(self):
        brain=Alice.__new__(Alice)
        brain.config={**self.config,'persona_user_id':'11111111-1111-4111-8111-111111111111'}
        brain.store=self.store;brain.tools=None;brain.social_memory=None
        brain.facts=AsyncMock(return_value=[])
        brain.l2=type('Memory',(),{'list_companion_persona_facts':AsyncMock(return_value=[]),'list_life_items':AsyncMock(return_value=[])})()
        brain.today_activity=AsyncMock(return_value={})
        brain.run_tools=AsyncMock(return_value='收到这条新消息了。')
        work={'group':'private:11','actor':'11','key':'test-current','text':'NEW_TOPIC_SENTINEL',
              'event':self.event(message=[{'type':'text','data':{'text':'NEW_TOPIC_SENTINEL'}}])}
        packet={'kind':'shared_cognitive_state','revision':9,'policy_trials':[{'id':'p1','guidance':'LOCAL_TRIAL_SENTINEL'}]}
        with patch('cognition_bridge.context',AsyncMock(return_value=packet)):
            await brain.reply(work,[{'role':'assistant','content':'HISTORY_SHOULD_NOT_REPLACE_NEW_TURN','at':time.time()-60}])
        messages=brain.run_tools.await_args.args[0]
        self.assertIn('NEW_TOPIC_SENTINEL',messages[-1]['content'])
        self.assertNotIn('HISTORY_SHOULD_NOT_REPLACE_NEW_TURN',messages[-1]['content'])
        self.assertIn('LOCAL_TRIAL_SENTINEL',messages[0]['content'])
        self.assertEqual(work['_cognition_basis'],{'revision':9,'policy_ids':['p1']})

    def test_short_conversational_reactions_do_not_recall_unrelated_old_topics(self):
        from social_memory import SocialMemory
        mem = SocialMemory(self.config, self.store)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO messages(conv,group_id,actor,role,content,message_id,at,delivered,consolidated) "
                "VALUES ('c','private:11','11','user','那个电影是讲时尚服装的，人们以前还感兴趣','old-fashion',?,1,1)",
                (time.time() - 86400,),
            )
        mem.observe({
            'post_type': 'message', 'message_type': 'private', 'self_id': 1, 'user_id': 11,
            'message_id': 'old-fashion', 'time': time.time() - 86400,
            'message': [{'type': 'text', 'data': {'text': '那个电影是讲时尚服装的，人们以前还感兴趣'}}],
        }, scope='private:11')
        for reaction in ['不感兴趣', '真不感兴趣', '好无聊', '没意思', '算了吧']:
            self.assertEqual(self.store.recall('private:11', reaction), [])
            pkt = mem.recall({'group': 'private:11', 'actor': '11', 'text': reaction, 'event': {'message_id': 'cur'}})
            self.assertEqual(pkt['attributed_observations'], [])
            self.assertEqual(pkt['shared_experiences'], [])
        self.assertTrue(self.store.recall('private:11', '讲时尚服装的电影'))


if __name__=='__main__':unittest.main()
