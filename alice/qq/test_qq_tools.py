import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch
import time

from channel import Store, Gate
from qq_tools import QQTools, media_type, download_media
from alice import Alice


class ToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'test.sqlite3')
        self.api=AsyncMock()
        self.tools=QQTools({},self.store,self.api,lambda w:True)
        self.work={'key':'native:111:2:3','group':'111','actor':'2','text':'请求',
                   'event':{'message_id':3,'message':[]}}
        self.state={'images':{},'catalog':{},'selected':None,'collected':False}

    async def asyncTearDown(self):
        self.store.db.close();self.temp.cleanup()

    async def test_model_cannot_invent_sticker_or_arbitrary_api(self):
        result=await self.tools.execute('qq_choose_expression',{'expression_id':'system:invented'},self.work,self.state)
        self.assertEqual(result['status'],'error')
        result=await self.tools.execute('set_qq_avatar',{'file':'file:///private'},self.work,self.state)
        self.assertEqual(result['status'],'error')
        self.api.assert_not_awaited()

    async def test_permission_revocation_blocks_collection(self):
        self.tools.permitted=lambda w:False
        result=await self.tools.execute('qq_collect_image',{'image_ref':'x','description':'喜欢'},self.work,self.state)
        self.assertEqual(result['reason'],'group_permission_revoked')
        self.api.assert_not_awaited()

    async def test_cannot_collect_unseen_image(self):
        result=await self.tools.execute('qq_collect_image',{'image_ref':'file:///private','description':'喜欢'},self.work,self.state)
        self.assertEqual(result['status'],'error')
        self.api.assert_not_awaited()

    async def test_collection_requires_receipt_then_records_real_id(self):
        self.state['images']['current:3']={'bytes':b'GIF89a content','mime':'image/gif'}
        self.api.side_effect=[{'emoji_id':'native-id'},{}]
        result=await self.tools.execute('qq_collect_image',{'image_ref':'current:3','description':'猫看手机'},self.work,self.state)
        self.assertEqual(result['status'],'collected')
        self.assertTrue(self.state['collected'])
        self.assertEqual(result['expression_id'],'saved:native-id')
        self.assertEqual(self.api.await_args_list[0].args[0],'add_custom_face')
        self.assertTrue(self.api.await_args_list[0].args[1]['file'].startswith('base64://'))

    async def test_missing_collection_receipt_does_not_claim_success(self):
        self.state['images']['current:3']={'bytes':b'GIF89a content','mime':'image/gif'}
        self.api.return_value={}
        result=await self.tools.execute('qq_collect_image',{'image_ref':'current:3','description':'猫'},self.work,self.state)
        self.assertEqual(result['status'],'error')
        self.assertFalse(self.state['collected'])

    async def test_dry_run_never_collects(self):
        self.state['images']['current:3']={'bytes':b'GIF89a content','mime':'image/gif'}
        self.tools.dry_run=True
        result=await self.tools.execute('qq_collect_image',{'image_ref':'current:3','description':'猫'},self.work,self.state)
        self.assertEqual(result['status'],'dry_run_only')
        self.api.assert_not_awaited()

    async def test_catalog_selection_defers_send(self):
        self.api.side_effect=[{'faces':[{'q_sid':'14','q_des':'/微笑'}]},[]]
        result=await self.tools.execute('qq_find_expression',{'query':'微笑'},self.work,self.state)
        self.assertEqual(result['saved_count'],0)
        result=await self.tools.execute('qq_choose_expression',{'expression_id':'system:14'},self.work,self.state)
        self.assertEqual(result['status'],'selected_for_final_reply')
        self.assertEqual(self.state['selected']['segment'],{'type':'face','data':{'id':'14'}})
        self.assertEqual(self.api.await_count,2)

    async def test_expression_layout_and_reason_are_auditable_not_new_persona_facts(self):
        self.state['catalog']['saved:real']={'segment':{'type':'image','data':{'file':'base64://real'}},'label':'摆烂了'}
        result=await self.tools.execute('qq_choose_expression',{'expression_id':'saved:real','text_placement':'together',
            'reason':'刚才说懒得动，这张正好接那个意思。'},self.work,self.state)
        self.assertEqual(result['text_placement'],'together')
        row=self.store.db.execute("SELECT payload FROM metrics WHERE kind='qq_expression_choice'").fetchone()
        decision=json.loads(row[0])
        self.assertEqual(decision['source_message_id'],'3')
        self.assertEqual(decision['reason_self_report'],'刚才说懒得动，这张正好接那个意思。')
        self.assertFalse(decision['reason_is_growth_evidence'])
        self.api.assert_not_awaited()

    async def test_quote_from_other_group_is_not_exposed(self):
        self.work['event']['message']=[{'type':'reply','data':{'id':'7'}}]
        self.api.side_effect=[{'messages':[]},{'group_id':222,'user_id':9,'message':[{'type':'text','data':{'text':'private'}}]}]
        state=await self.tools.prepare(self.work)
        self.assertEqual(state['context'],[])

    async def test_local_recall_keeps_speaker_and_never_crosses_group(self):
        source={'group_id':111,'user_id':2}
        self.store.message('old',source,'user','蓝色大肥鱼是大家给DeepSeek的外号',77,True)
        self.store.message('other',{'group_id':222,'user_id':2},'user','蓝色大肥鱼是隔壁私事',78,True)
        rows=self.store.recall(111,'蓝色大肥鱼是什么')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['speaker'],'2')
        self.assertEqual(rows[0]['message_id'],'77')

    async def test_native_window_does_not_duplicate_acknowledged_local_history(self):
        self.work['event'].update(message_type='group',self_id=1)
        self.work['history_message_ids']={'11','12'}
        messages=[{'message_id':11,'group_id':111,'user_id':2,'time':time.time(),'message':[{'type':'text','data':{'text':'我只问了一句'}}]},
                  {'message_id':12,'group_id':111,'user_id':1,'time':time.time(),'message':[{'type':'text','data':{'text':'又一个重复的自我表态'}}]},
                  {'message_id':13,'group_id':111,'user_id':3,'time':time.time(),'message':[{'type':'text','data':{'text':'其他群友的上下文'}}]}]
        async def api(action,params):
            return {'messages':messages} if action=='get_group_msg_history' else []
        self.tools.api=api
        state=await self.tools.prepare(self.work)
        native=next(i for i in state['context'] if i.get('kind')=='currently_visible_group_history')['messages']
        self.assertEqual([i['message_id'] for i in native],['13'])

    async def test_sticker_shelf_is_short_and_first_person(self):
        self.work['event'].update(message_type='private', self_id=1)
        async def api(action, params):
            if action == 'get_friend_msg_history':
                return {'messages': []}
            if action == 'fetch_custom_face_detail':
                return [{'emoji_id': 'e1', 'desc': '一只猫卡在塑料瓶口，脸被挤变形，画面很暗', 'url': 'https://gchat.qpic.cn/x'},
                        {'emoji_id': 'e2', 'desc': '黄色小玩偶趴在床上', 'url': 'https://gchat.qpic.cn/y'}]
            return []
        self.tools.api = api
        state = await self.tools.prepare(self.work)
        shelf = next(i for i in state['context'] if i.get('kind') == 'my_sticker_shelf')
        self.assertEqual([i['expression_id'] for i in shelf['items']], ['saved:e1', 'saved:e2'])
        self.assertLessEqual(len(shelf['items'][0]['what']), 12)
        self.assertIn('发不发', shelf['note'])

    async def test_media_rejects_local_urls_and_untrusted_hosts(self):
        for url in ['http://127.0.0.1/a','https://example.com/a','file:///private','https://gchat.qpic.cn.evil.test/a']:
            with self.assertRaises(ValueError):await download_media(url)
        self.assertEqual(media_type(b'GIF89a bytes'),'image/gif')

    async def test_capability_request_keeps_original_provenance_no_install(self):
        result=await self.tools.execute('qq_note_capability_need',{'request':'更换头像','benefit':'表达自我'},self.work,self.state)
        self.assertFalse(result['installed'])
        row=self.store.db.execute('SELECT source_key,raw_request FROM capability_requests').fetchone()
        self.assertEqual(row,('native:111:2:3','请求'))
        self.api.assert_not_awaited()

    async def test_search_is_hidden_and_unexecutable_by_default(self):
        self.assertNotIn('web_search',[t['function']['name'] for t in self.tools.definitions()])
        result=await self.tools.execute('web_search',{'query':'蓝色大肥鱼'},self.work,self.state)
        self.assertEqual(result['reason'],'search_disabled_by_owner')
        self.api.assert_not_awaited()

    async def test_explicit_owner_setting_can_expose_search(self):
        self.tools.config['search_enabled']=True
        self.assertIn('web_search',[t['function']['name'] for t in self.tools.definitions()])

    async def test_exact_expression_can_be_selected_in_one_tool_call(self):
        self.api.side_effect=[{'faces':[{'q_sid':'412','q_des':'/开心'}]},[]]
        result=await self.tools.execute('qq_find_expression',{'query':'开心','select_for_reply':True},self.work,self.state)
        self.assertEqual(result['selected_for_final_reply'],'/开心')
        self.assertEqual(self.state['selected']['segment']['data']['id'],'412')
        again=await self.tools.execute('qq_choose_expression',{'expression_id':'system:412'},self.work,self.state)
        self.assertEqual(again['status'],'selected_for_final_reply')

    async def test_guessing_game_uses_verified_qq_catalog_name(self):
        self.api.side_effect=[{'faces':[{'q_sid':'359','q_des':'/包剪锤'}]},[]]
        result=await self.tools.execute('qq_find_expression',{'query':'猜拳','select_for_reply':True},self.work,self.state)
        self.assertEqual(self.api.await_args_list[0].args,('search_sys_faces',{'query':'包剪锤'}))
        self.assertEqual(result['selected_for_final_reply'],'/包剪锤')
        self.assertEqual(self.state['selected']['segment']['data']['id'],'359')
        self.assertIsNone(result['online_search_tool'])

    async def test_generic_sticker_request_lists_real_favorites_without_name_match(self):
        self.api.return_value=[{'emoji_id':'native-favorite','desc':'黑白雨窗前的企鹅表情','url':'https://p.qpic.cn/qq_expression/1/a/0'}]
        result=await self.tools.execute('qq_find_expression',{'query':'表情包','select_for_reply':True},self.work,self.state)
        self.assertEqual(result['saved_count'],1)
        self.assertEqual(result['selected_for_final_reply'],'黑白雨窗前的企鹅表情')
        self.assertEqual(self.api.await_count,1)
        self.assertEqual(self.api.await_args.args[0],'fetch_custom_face_detail')
        self.assertEqual(result['expressions'][0]['animation'],'not_verified')

    async def test_followup_can_see_and_collect_recent_image_from_same_speaker(self):
        self.work['text']='喜欢这个吗'
        image={'message_id':55,'group_id':111,'user_id':2,'time':time.time()-10,
               'message':[{'type':'image','data':{'file':'A'*32+'.gif'}}]}
        async def api(action,params):
            if action=='get_group_msg_history':return {'messages':[image]}
            if action=='get_image':return {'url':'https://gchat.qpic.cn/real'}
            if action=='fetch_custom_face_detail':return []
            if action=='add_custom_face':return {'emoji_id':'real-new-favorite'}
            return {}
        self.api.side_effect=api
        with patch('qq_tools.download_media',AsyncMock(return_value=(b'GIF89a actual original','image/gif'))):
            state=await self.tools.prepare(self.work)
        self.assertIn('recent:55',state['images'])
        result=await self.tools.execute('qq_collect_image',{'image_ref':'recent:55','description':'黄色趴床表情'},self.work,state)
        self.assertEqual(result['status'],'collected')
        self.assertTrue(state['collected'])
        receipt=self.store.db.execute('SELECT image_ref,emoji_id FROM qq_collection_receipts').fetchone()
        self.assertEqual(receipt,('recent:55','real-new-favorite'))

    async def test_generic_send_request_does_not_attach_unrelated_previous_picture(self):
        self.work['text']='发个表情包'
        image={'message_id':55,'group_id':111,'user_id':2,'time':time.time()-10,
               'message':[{'type':'image','data':{'file':'A'*32+'.gif'}}]}
        async def api(action,params):
            if action=='get_group_msg_history':return {'messages':[image]}
            if action=='fetch_custom_face_detail':return []
            raise AssertionError('Generic send request must not download prior image')
        self.api.side_effect=api
        state=await self.tools.prepare(self.work)
        self.assertEqual(state['image_parts'],[])
        self.assertEqual(state['images'],{})

    async def test_seen_image_can_be_sent_without_first_collecting_it(self):
        self.work['event']['message']=[{'type':'image','data':{'file':'A'*32+'.gif'}}]
        async def api(action,params):
            if action=='get_group_msg_history':return {'messages':[]}
            if action=='get_image':return {'url':'https://gchat.qpic.cn/real'}
            if action=='fetch_custom_face_detail':return []
            raise AssertionError('Unexpected mutation')
        self.api.side_effect=api
        with patch('qq_tools.download_media',AsyncMock(return_value=(b'GIF89a original','image/gif'))):
            state=await self.tools.prepare(self.work)
        result=await self.tools.execute('qq_choose_expression',{'expression_id':'visible:current:3'},self.work,state)
        self.assertEqual(result['status'],'selected_for_final_reply')
        self.assertFalse(state['collected'])


    async def test_collected_expression_resolves_to_sendable_url(self):
        self.state['catalog']['collected:emu-1']={'segment':None,'label':'猫卡瓶口','collected_emoji_id':'emu-1'}
        self.api.return_value=[{'emoji_id':'emu-1','url':'https://gchat.qpic.cn/gchatpic_new/x/0','desc':'猫卡瓶口'}]
        result=await self.tools.execute('qq_choose_expression',{'expression_id':'collected:emu-1'},self.work,self.state)
        self.assertEqual(result['status'],'selected_for_final_reply')
        self.assertEqual(self.state['selected']['segment'],
                         {'type':'image','data':{'file':'https://gchat.qpic.cn/gchatpic_new/x/0','sub_type':1,'summary':'[动画表情]'}})

    async def test_collected_expression_missing_from_native_catalog_fails_loudly(self):
        self.state['catalog']['collected:gone']={'segment':None,'label':'删掉的图','collected_emoji_id':'gone'}
        self.api.return_value=[]
        result=await self.tools.execute('qq_choose_expression',{'expression_id':'collected:gone'},self.work,self.state)
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['reason'],'collected_expression_unavailable')
        self.assertIsNone(self.state['selected'])

    async def test_sticker_without_hex_filename_is_still_seen(self):
        self.state['context']=[]
        source={'message_id':9,'message':[{'type':'mface','data':{'file':'https://gchat.qpic.cn/gchatpic_new/y/0'}}]}
        with patch('qq_tools.download_media',AsyncMock(return_value=(b'GIF89a data','image/gif'))):
            parts=await self.tools.read_visual(source,'111',self.state,origin='current',work=self.work)
        self.assertTrue(parts)
        self.assertIn('visible:current:9',self.state['catalog'])
        self.assertEqual(self.state['visual_evidence'][0]['digest'],
                         __import__('hashlib').sha1(b'GIF89a data').hexdigest())

    async def test_expression_can_be_chosen_by_remembered_picture(self):
        self.state['catalog']['saved:a']={'segment':{'type':'image','data':{'file':'base64://a'}},'label':'一只猫卡在塑料瓶口，脸被挤变形'}
        self.state['catalog']['saved:b']={'segment':{'type':'image','data':{'file':'base64://b'}},'label':'黄色毛绒小动物趴在床上，配字摆烂了'}
        result=await self.tools.execute('qq_choose_expression',{'expression_id':'','description':'猫卡瓶口'},self.work,self.state)
        self.assertEqual(result['status'],'selected_for_final_reply')
        self.assertEqual(self.state['selected']['segment']['data']['file'],'base64://a')

    async def test_unknown_picture_returns_candidates_instead_of_guessing(self):
        self.state['catalog']['saved:a']={'segment':{'type':'image','data':{'file':'base64://a'}},'label':'一只猫卡在塑料瓶口'}
        result=await self.tools.execute('qq_choose_expression',{'expression_id':'','description':'外星人跳舞'},self.work,self.state)
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['reason'],'expression_not_found')
        self.assertIsNone(self.state['selected'])

    async def test_skip_reply_only_allowed_when_not_addressed(self):
        state = {**self.state, 'context': []}
        addressed = {**self.work, 'trigger': 'mention'}
        result = await self.tools.execute('qq_skip_reply', {'reason': '他们在聊自己的'}, addressed, state)
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('silent', state)
        self.work = {**self.work, 'trigger': 'conversation_continuation'}
        result = await self.tools.execute('qq_skip_reply', {'reason': '不接'}, self.work, state)
        self.assertEqual(result['status'], 'silent')
        self.assertTrue(state.get('silent'))

    async def test_quote_can_target_an_earlier_seen_message(self):
        state = {**self.state, 'quotable_ids': {'11', '12'}}
        result = await self.tools.execute('qq_quote_current_message', {'message_id': '11'}, self.work, state)
        self.assertEqual(result['status'], 'selected_for_final_reply')
        self.assertEqual(state['quote_target'], '11')
        bad = await self.tools.execute('qq_quote_current_message', {'message_id': '99'}, self.work, state)
        self.assertEqual(bad['status'], 'error')
        self.assertEqual(bad['reason'], 'message_not_seen_this_turn')

class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_reply_uses_one_call(self):
        brain=Alice.__new__(Alice)
        brain.tools=type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:[]})()
        brain.model_message=AsyncMock(return_value={'content':'你好'})
        result=await brain.run_tools([],{}, {})
        self.assertEqual(result,'你好');self.assertEqual(brain.model_message.await_count,1)

    async def test_three_calls_and_three_tools_are_hard_limits(self):
        brain=Alice.__new__(Alice)
        brain.tools=type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:[{'type':'function'}],'execute':AsyncMock(return_value={'status':'ok'})})()
        call={'id':'native','type':'function','function':{'name':'web_search','arguments':'{"query":"梗"}'}}
        brain.model_message=AsyncMock(side_effect=[{'tool_calls':[call]*4},{'content':'完成'}])
        await brain.run_tools([],{}, {})
        self.assertEqual(brain.tools.execute.await_count,3)
        self.assertEqual(brain.model_message.await_count,2)

    async def test_claim_of_sending_without_attachment_gets_one_repair(self):
        brain=Alice.__new__(Alice)
        brain.tools=type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:[]})()
        brain.store=type('Store',(),{'metric':lambda *a,**k:None})()
        brain.model_message=AsyncMock(side_effect=[{'content':'那只鸽子发你了'},{'content':'不对，还没发出去。'}])
        state={'catalog':{'saved:x':{'segment':{'type':'image'},'label':'摆烂了'}},'selected':None}
        result=await brain.run_tools([],{'event':{'message_id':1},'text':'还有别的吗'},state)
        self.assertEqual(brain.model_message.await_count,2)
        self.assertIn('还没发',result)

    async def test_cancellation_blocks_further_model_and_tool_calls(self):
        brain=Alice.__new__(Alice)
        brain.tools=type('Tools',(),{'permitted':lambda s,w:False})()
        brain.model_message=AsyncMock()
        with self.assertRaisesRegex(RuntimeError,'turn_cancelled'):await brain.run_tools([],{}, {})
        brain.model_message.assert_not_awaited()
