import io
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from alice import Alice
from channel import Store
from qq_tools import QQTools
from sticker_search import search_stickers, native_catalog, image_details


def gif_bytes():
    out = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(out, format='GIF', save_all=True,
        append_images=[Image.new('RGB', (8, 8), 'blue')], duration=100, loop=0)
    return out.getvalue()


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def client_factory(self, handler):
        original = httpx.AsyncClient
        return lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)

    async def test_gif_search_preserves_multiple_frames_and_rejects_fake_gif(self):
        gif = gif_bytes()
        png = io.BytesIO()
        Image.new('RGB', (8, 8)).save(png, format='PNG')
        def handler(request):
            return httpx.Response(200, content=png.getvalue() if request.url.path == '/fake.gif' else gif)
        api=AsyncMock(return_value=native_response(['https://wa.qq.com/fake.gif','https://wa.qq.com/real.gif']))
        with patch('httpx.AsyncClient', self.client_factory(handler)):
            rows = await search_stickers('开心', api=api, animated_only=True)
        api.assert_awaited_once_with('qq_search_native_expressions',{'query':'开心','tab':'all'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['bytes'], gif)
        self.assertTrue(rows[0]['animated'])
        self.assertEqual(rows[0]['mime'], 'image/gif')

    async def test_search_cannot_fetch_untrusted_result_host(self):
        requests = []
        def handler(request):
            requests.append(str(request.url.host))
            return httpx.Response(200, content=gif_bytes())
        api=AsyncMock(return_value=native_response(['http://127.0.0.1/secret','https://wa.qq.com.evil.test/a']))
        with patch('httpx.AsyncClient', self.client_factory(handler)):
            self.assertEqual(await search_stickers('开心',api=api), [])
        self.assertEqual(requests, [])

    async def test_qq_identity_is_not_sent_as_expression_query(self):
        api=AsyncMock()
        with patch('httpx.AsyncClient') as client:
            with self.assertRaises(ValueError):
                await search_stickers('10001 开心',api=api)
            client.assert_not_called()
        api.assert_not_awaited()

    async def test_api_failure_is_not_reported_as_successful_empty_search(self):
        api=AsyncMock(return_value={'source':'qq_native_panel','native_success':False})
        with self.assertRaises(ValueError):
            await search_stickers('开心',api=api)

    async def test_gif_recommendations_have_no_second_search_keyword(self):
        api=AsyncMock(return_value=native_response(['https://wa.qq.com/real.gif'],recommendations=True))
        with patch('sticker_search.download_media',AsyncMock(return_value=(gif_bytes(),'image/gif'))):
            rows=await search_stickers('',api=api,tab='gif')
        self.assertEqual(len(rows),1)
        api.assert_awaited_once_with('qq_search_native_expressions',{'query':'','tab':'gif'})

    async def test_composition_background_is_not_sent_as_completed_expression(self):
        api=AsyncMock(return_value=native_response(['https://wa.qq.com/base.png'],composed=True))
        with patch('sticker_search.download_media') as download:
            with self.assertRaisesRegex(ValueError,'native_text_composition_not_connected'):
                await search_stickers('开心猫',api=api,tab='composed')
            download.assert_not_called()

    def test_rejects_truncated_or_error_envelope(self):
        for raw in [b'\x22\xff', pb(1,0x9038)+pb(2,1)+pb(3,5)]:
            with self.assertRaises(ValueError):
                native_catalog({'source':'qq_native_panel','native_success':True,'got_response':True,'native_error':0,
                                'body_base64':base64.b64encode(raw).decode()})

    def test_ten_second_nearly_static_gif_is_not_called_animated(self):
        out=io.BytesIO()
        Image.new('RGB',(80,80),'red').save(out,format='GIF',save_all=True,
            append_images=[Image.new('RGB',(80,80),'blue')],duration=[10000,0],loop=0)
        self.assertFalse(image_details(out.getvalue())['animated'])


def pb(field,value):
    def vi(n):
        out=bytearray()
        while n>127:out.append((n&127)|128);n>>=7
        out.append(n);return bytes(out)
    if isinstance(value,int):return vi(field*8)+vi(value)
    value=value.encode() if isinstance(value,str) else value
    return vi(field*8+2)+vi(len(value))+value


def native_response(urls,*,recommendations=False,composed=False):
    rows=b''.join(pb(4 if recommendations else 7 if composed else 3,
                    pb(11 if recommendations else 10,url)+pb(7,'')) for url in urls)
    raw=pb(1,pb(1,0)+rows) if recommendations else pb(1,0x9038)+pb(2,1)+pb(3,0)+pb(4,rows)
    return {'source':'qq_native_panel','native_success':True,'got_response':True,'native_error':0,
            'body_base64':base64.b64encode(raw).decode()}


class ToolPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.api = AsyncMock()
        self.tools = QQTools({'sticker_search_enabled': True}, self.store, self.api, lambda w: True)
        self.work = {'key': 'test', 'group': '111', 'actor': '222', 'event': {'message_id': 3}}
        self.state = {'images': {}, 'catalog': {}, 'selected': None, 'collected': False}
        self.candidate = {'id': 'real-image-hash', 'bytes': gif_bytes(), 'mime': 'image/gif',
                          'animated': True, 'width': 8, 'height': 8}

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_search_previews_then_choice_without_sending_or_general_search(self):
        with patch('sticker_search.search_stickers', AsyncMock(return_value=[self.candidate])) as search:
            result = await self.tools.execute('qq_search_sticker', {'query': '开心', 'animated_only': True}, self.work, self.state)
            identity = result['expressions'][0]['expression_id']
            self.assertIsNone(self.state['selected'])
            denied = await self.tools.execute('qq_choose_expression', {'expression_id': identity}, self.work, self.state)
            self.assertEqual(denied['reason'], 'search_preview_not_seen_yet')
            self.state.pop('expression_previews')
            chosen = await self.tools.execute('qq_choose_expression', {'expression_id': identity}, self.work, self.state)
            self.assertEqual(chosen['status'], 'selected_for_final_reply')
            self.assertTrue(self.state['selected']['segment']['data']['file'].startswith('base64://'))
            again = await self.tools.execute('qq_search_sticker', {'query': '难过'}, self.work, self.state)
            self.assertEqual(again['reason'], 'one_expression_search_per_turn')
            self.assertEqual(search.await_count, 1)
        self.api.assert_not_awaited()
        self.assertNotIn('web_search', [t['function']['name'] for t in self.tools.definitions()])

    async def test_owner_can_disable_expression_search_separately(self):
        self.tools.config['sticker_search_enabled'] = False
        self.assertNotIn('qq_search_sticker', [t['function']['name'] for t in self.tools.definitions()])
        result = await self.tools.execute('qq_search_sticker', {'query': '开心'}, self.work, self.state)
        self.assertEqual(result['reason'], 'sticker_search_disabled_by_owner')
        self.api.assert_not_awaited()

    async def test_generic_gif_does_not_select_unverified_saved_image(self):
        with patch('sticker_search.search_stickers', AsyncMock(return_value=[self.candidate])) as search:
            result=await self.tools.execute('qq_find_expression',{'query':'动态表情包','select_for_reply':True},self.work,self.state)
        self.assertEqual(result['source'],'qq_native_panel')
        self.assertEqual(search.await_args.kwargs['tab'],'gif')
        self.assertEqual(search.await_args.args[0],'')
        self.assertIsNone(self.state['selected'])
        self.api.assert_not_awaited()

    async def test_missing_local_expression_falls_back_within_call_budget(self):
        self.api.side_effect = [{'faces': []}, []]
        with patch('sticker_search.search_stickers', AsyncMock(return_value=[self.candidate])):
            result = await self.tools.execute('qq_find_expression', {'query': '柔弱', 'select_for_reply': True}, self.work, self.state)
        self.assertEqual(result['search_scope'], 'qq_native_panel_search_fallback')
        self.assertEqual(len(result['expressions']), 1)
        self.assertTrue(self.state['expression_previews'])
        self.assertIsNone(self.state['selected'])

    async def test_same_model_receives_preview_before_choosing_in_bounded_loop(self):
        brain = Alice.__new__(Alice)
        brain.tools = self.tools
        def call(name, args):
            import json
            return {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        async def model(messages, **kwargs):
            count = brain.model_message.await_count
            if count == 1:
                return {'tool_calls': [call('qq_search_sticker', {'query': '开心', 'animated_only': True})]}
            if count == 2:
                self.assertEqual(messages[-1]['role'], 'user')
                self.assertTrue(any(p['type'] == 'image_url' for p in messages[-1]['content']))
                self.assertTrue(any('search:real-image-hash' in p.get('text', '') for p in messages[-1]['content']))
                return {'tool_calls': [call('qq_choose_expression', {'expression_id': 'search:real-image-hash'})]}
            return {'content': '哈哈，这个就很像我现在的心情'}
        brain.model_message = AsyncMock(side_effect=model)
        with patch('sticker_search.search_stickers', AsyncMock(return_value=[self.candidate])):
            reply = await brain.run_tools([], self.work, self.state)
        self.assertEqual(reply, '哈哈，这个就很像我现在的心情')
        self.assertEqual(brain.model_message.await_count, 3)
        self.assertIsNotNone(self.state['selected'])
        self.api.assert_not_awaited()
