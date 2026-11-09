import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from channel import Store
from sticker_observer import StickerObserver


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'state.sqlite3')
        self.config={'enabled':True,'allowed_groups':['111']}
        self.api=AsyncMock(return_value=[])
        self.brain=types.SimpleNamespace(
            tools=types.SimpleNamespace(prepare=AsyncMock(return_value={'image_parts':[{'type':'image_url','image_url':{'url':'data:image/gif;base64,fixture'}}],
                'images':{'current:3':{}},'catalog':{},'collected':False}),
                execute=AsyncMock(return_value={'status':'collected','emoji_id':'real-receipt'})),
            facts=AsyncMock(return_value=[]),
            completion=AsyncMock(return_value=json.dumps({'collect':False,'description':'企鹅','reason':'暂时没有想留下的感觉'})))
        self.observer=StickerObserver(self.config,self.store,self.brain,self.api,lambda w:True)
        self.fake_memory=types.ModuleType('memory.companion_persona')
        self.fake_memory.build_persona_packet=lambda *a,**k:{}
        self.fake_memory.supported_growth_fact_ids=lambda facts:set()

    async def asyncTearDown(self):
        self.store.db.close();self.temp.cleanup()

    def event(self,**changes):
        result={'post_type':'message','message_type':'group','group_id':111,'user_id':2,'self_id':1,
            'message_id':3,'time':time.time(),
            'message':[{'type':'image','data':{'file':'A'*32+'.gif','sub_type':1}}]}
        result.update(changes);return result

    async def consider(self,event=None):
        self.observer.offer(event or self.event())
        with patch.dict(sys.modules,{'memory.companion_persona':self.fake_memory}):
            await self.observer.consider_latest()

    async def test_normal_text_other_groups_self_old_and_nan_are_free(self):
        events=[self.event(message=[{'type':'text','data':{'text':'闲聊'}}]),self.event(group_id=222),
                self.event(user_id=1),self.event(time=0),self.event(time=float('nan'))]
        for event in events:
            await self.consider(event)
        self.brain.completion.assert_not_awaited();self.api.assert_not_awaited()

    async def test_directed_images_use_foreground_path_only(self):
        event=self.event();event['message'].append({'type':'at','data':{'qq':'1'}})
        await self.consider(event)
        self.brain.completion.assert_not_awaited()

    async def test_not_liking_image_does_not_collect_or_send(self):
        await self.consider()
        self.brain.completion.assert_awaited_once()
        self.brain.tools.execute.assert_not_awaited()
        self.assertEqual(self.store.db.execute('SELECT status FROM sticker_observations').fetchone()[0],'passed')
        self.assertEqual([c.args[0] for c in self.api.await_args_list],['fetch_custom_face_detail'])

    async def test_liking_image_collects_without_chat_message(self):
        self.brain.completion.return_value=json.dumps({'collect':True,'description':'企鹅淋雨','reason':'有点好笑又有点惨，我想留着用'})
        await self.consider()
        self.brain.tools.execute.assert_awaited_once()
        self.assertEqual(self.brain.tools.execute.await_args.args[0],'qq_collect_image')
        self.assertEqual(self.store.db.execute('SELECT status,emoji_id FROM sticker_observations').fetchone(),('collected','real-receipt'))
        self.assertFalse(any(c.args[0].startswith('send_') for c in self.api.await_args_list))

    async def test_same_image_is_not_rejudged(self):
        await self.consider();await self.consider()
        self.brain.completion.assert_awaited_once()

    async def test_native_existing_sticker_skips_model(self):
        self.api.return_value=[{'md5':'A'*32}]
        await self.consider()
        self.brain.completion.assert_not_awaited()

    async def test_daily_limit_and_interval_block_model_calls(self):
        self.config['ambient_sticker_views_per_day']=0
        await self.consider();self.brain.completion.assert_not_awaited()
        self.config['ambient_sticker_views_per_day']=3
        await self.consider()
        event=self.event(message_id=4);event['message'][0]['data']['file']='B'*32+'.gif'
        await self.consider(event)
        self.brain.completion.assert_awaited_once()

    async def test_per_group_quota_keeps_other_groups_available_with_global_gap(self):
        self.config['ambient_sticker_limits_per_group']=True
        now=int(time.time()//86400)*86400+12*3600
        for i in range(3):
            self.store.db.execute('INSERT INTO sticker_observations VALUES (?,?,?,?,?,?,?,?,?)',
                ('old'+str(i),'111','2',str(i),now-3600,'passed','','',''))
        self.store.db.commit()
        self.assertFalse(self.observer.slots_available(now,'111'))
        self.assertTrue(self.observer.slots_available(now,'222'))
        self.store.db.execute('UPDATE sticker_observations SET observed_at=? WHERE digest=?',(now,'old0'));self.store.db.commit()
        self.assertFalse(self.observer.slots_available(now,'222'))

    async def test_disabled_observer_stays_quiet(self):
        self.config['ambient_stickers_enabled']=False
        await self.consider()
        self.api.assert_not_awaited();self.brain.completion.assert_not_awaited()

    async def test_existing_call_keeps_observation_pending_and_does_not_call_model(self):
        from activity import claim,release
        self.observer.offer(self.event())
        self.assertTrue(claim(self.store,'active-call'))
        try:
            await self.observer.consider_latest()
            self.brain.completion.assert_not_awaited()
            self.assertIsNotNone(self.observer.latest)
        finally:release(self.store,'active-call')
