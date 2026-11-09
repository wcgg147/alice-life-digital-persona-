import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from channel import Store
from qq_tools import QQTools
from alice import Alice


class PublicSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'trial.sqlite3')
        self.cfg={'search_enabled':True,'public_source_read_enabled':True}
        self.api=AsyncMock();self.allowed=True
        self.tools=QQTools(self.cfg,self.store,self.api,lambda w:self.allowed)
        self.work={'key':'trial:1','group':'111111','actor':'222222','text':'查查这个说法',
                   'event':{'message_id':'original'}}
        self.state={}

    async def asyncTearDown(self):self.store.db.close();self.tmp.cleanup()

    async def search(self):
        with patch('agent.web_search_tool.WebSearchTool.search',AsyncMock(return_value={
                'status':'ok','results':[{'title':'原来源','url':'https://example.org/source','snippet':'仅摘要'}]})):
            result=await self.tools.execute('web_search',{'query':'一种说法'},self.work,self.state)
        return result['results'][0]['source_id']

    async def test_source_read_hidden_and_rejected_without_explicit_setting(self):
        self.cfg['public_source_read_enabled']=False
        self.assertNotIn('read_public_source',[d['function']['name'] for d in self.tools.definitions()])
        result=await self.tools.execute('read_public_source',{'source_id':'invented'},self.work,self.state)
        self.assertEqual(result.get('reason'),'public_source_read_disabled')

    async def test_search_reference_executes_existing_reader_and_records_actual_receipt(self):
        self.cfg['public_source_resolver']='cloudflare'
        ref=await self.search()
        page={'status':'ok','url':'https://example.org/source','text':'真实原文'*1500,
              'response_sha256':'actual-response-hash','fetched_at':'2026-11-09T00:00:00Z','text_truncated':False}
        with patch('agent.public_page_reader.read_public_page',AsyncMock(return_value=page)) as reader:
            result=await self.tools.execute('read_public_source',{'source_id':ref},self.work,self.state)
        reader.assert_awaited_once()
        self.assertEqual(reader.await_args.args[0],'https://example.org/source')
        self.assertEqual(reader.await_args.kwargs['resolver'],'cloudflare')
        self.assertEqual(result['response_sha256'],'actual-response-hash')
        self.assertTrue(result['text_truncated'])
        self.assertLess(len(json.dumps(result,ensure_ascii=False)),5000)
        self.assertTrue(result['text'].startswith('真实原文'))
        saved=json.loads(self.store.db.execute("select payload from metrics where kind='qq_public_source_receipt'").fetchone()[0])
        self.assertEqual(saved['source_message_id'],'original')
        self.assertEqual(saved['result'],result)
        self.api.assert_not_awaited()

    async def test_invented_and_cross_turn_source_ids_cannot_read(self):
        ref=await self.search()
        for work,source in [(self.work,'invented'),({**self.work,'key':'trial:2'},ref),
                            ({**self.work,'actor':'other'},ref),({**self.work,'group':'other'},ref)]:
            with patch('agent.public_page_reader.read_public_page',AsyncMock()) as reader:
                result=await self.tools.execute('read_public_source',{'source_id':source},work,self.state)
            self.assertEqual(result['status'],'error');reader.assert_not_awaited()

    async def test_nonpublic_catalog_entries_never_receive_a_source_id(self):
        with patch('agent.web_search_tool.WebSearchTool.search',AsyncMock(return_value={
                'status':'ok','results':[{'url':'http://127.0.0.1/x'},{'url':'https://localhost/x'}]})):
            result=await self.tools.execute('web_search',{'query':'一种说法'},self.work,self.state)
        self.assertFalse(any(r.get('source_id') for r in result['results']))

    async def test_permission_or_feature_revoked_during_read_drops_body(self):
        for revoke in ('permission','feature'):
            self.allowed=True;self.cfg['public_source_read_enabled']=True
            ref=await self.search()
            async def read(*a,**k):
                if revoke=='permission':self.allowed=False
                else:self.cfg['public_source_read_enabled']=False
                return {'status':'ok','text':'不应发布的正文'}
            with patch('agent.public_page_reader.read_public_page',side_effect=read):
                result=await self.tools.execute('read_public_source',{'source_id':ref},self.work,self.state)
            self.assertEqual(result['status'],'error');self.assertNotIn('text',result)

    async def test_read_failure_is_not_a_successful_body_or_retry(self):
        ref=await self.search()
        with patch('agent.public_page_reader.read_public_page',AsyncMock(return_value={
                'status':'error','error_code':'reader_timeout'})) as reader:
            result=await self.tools.execute('read_public_source',{'source_id':ref},self.work,self.state)
        self.assertEqual(result['status'],'error');self.assertEqual(result['error_code'],'reader_timeout')
        self.assertNotIn('text',result);self.assertEqual(reader.await_count,1)

    async def test_real_tool_loop_can_search_choose_read_then_answer(self):
        brain=Alice.__new__(Alice);brain.tools=self.tools;brain.store=self.store
        def call(name,args):return {'id':name,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}
        async def model(messages,**kwargs):
            results=[json.loads(m['content']) for m in messages if m.get('role')=='tool']
            if not results:return {'tool_calls':[call('web_search',{'query':'一种说法'})]}
            if len(results)==1:
                return {'tool_calls':[call('read_public_source',{'source_id':results[0]['results'][0]['source_id']})]}
            self.assertEqual(results[1]['text'],'这是原文，不是搜索摘要。')
            return {'content':'原文表达的是一个有条件的判断。'}
        brain.model_message=AsyncMock(side_effect=model)
        with patch('agent.web_search_tool.WebSearchTool.search',AsyncMock(return_value={
                'status':'ok','results':[{'url':'https://example.org/source','snippet':'摘要'}]})), \
             patch('agent.public_page_reader.read_public_page',AsyncMock(return_value={
                'status':'ok','text':'这是原文，不是搜索摘要。','url':'https://example.org/source'})):
            result=await brain.run_tools([{'role':'user','content':'查查这个说法'}],self.work,self.state)
        self.assertIn('有条件',result);self.assertEqual(brain.model_message.await_count,3)

    async def test_explicit_provider_uses_independent_search_key(self):
        self.cfg['web_search_provider']='deepseek_search'
        with patch.dict('os.environ',{'MODELINK_SEARCH_API_KEY':'isolated-search-key'}), \
             patch('agent.web_search_tool.WebSearchTool') as factory:
            factory.return_value.search=AsyncMock(return_value={'status':'ok','results':[]})
            await self.tools.execute('web_search',{'query':'一种说法'},self.work,self.state)
        self.assertEqual(factory.call_args.kwargs['provider'],'deepseek_search')
        self.assertEqual(factory.call_args.kwargs['api_key'],'isolated-search-key')

    async def test_unsupported_provider_does_not_create_network_tool(self):
        self.cfg['web_search_provider']='arbitrary-endpoint'
        with patch('agent.web_search_tool.WebSearchTool') as factory:
            result=await self.tools.execute('web_search',{'query':'一种说法'},self.work,self.state)
        self.assertEqual(result['status'],'error');factory.assert_not_called()

    async def test_config_rejects_nonboolean_read_setting(self):
        from run import config_from
        path=Path(self.tmp.name)/'config.json'
        path.write_text(json.dumps({'onebot_url':'ws://127.0.0.1:13001','onebot_token':'fixture',
            'expected_qq':'123','public_source_read_enabled':'false'}),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'public_source_read_enabled must be boolean'):config_from(path)

    async def test_live_reload_revokes_reader_before_next_action(self):
        import asyncio
        from run import reload_groups
        self.cfg.update(enabled=True,onebot_url='ws://127.0.0.1:13001',onebot_token='fixture',
            expected_qq='123',persona_user_id='11111111-1111-4111-8111-111111111111')
        path=Path(self.tmp.name)/'config.json'
        path.write_text(json.dumps({**self.cfg,'public_source_read_enabled':False}),encoding='utf-8')
        class Gate:
            def __init__(self):self.active={}
        bot=type('Bot',(),{'gate':Gate()})()
        sleeps=0
        async def one_cycle(*a):
            nonlocal sleeps
            sleeps+=1
            if sleeps>1:raise asyncio.CancelledError()
        with patch('run.asyncio.sleep',side_effect=one_cycle):
            with self.assertRaises(asyncio.CancelledError):await reload_groups(path,self.cfg,bot,self.store)
        self.assertFalse(self.cfg['public_source_read_enabled'])
        self.assertNotIn('read_public_source',[t['function']['name'] for t in self.tools.definitions()])

    async def test_large_search_catalog_stays_valid_inside_real_tool_loop(self):
        brain=Alice.__new__(Alice);brain.tools=self.tools;brain.store=self.store
        rows=[{'url':f'https://example.org/{i}/'+('a'*900),'title':'标题'*90,'snippet':'摘要'*250} for i in range(3)]
        async def model(messages,**kwargs):
            results=[m for m in messages if m.get('role')=='tool']
            if not results:
                return {'tool_calls':[{'id':'search','type':'function','function':{
                    'name':'web_search','arguments':'{"query":"一种说法"}'}}]}
            result=json.loads(results[-1]['content'])
            self.assertTrue(result['results'])
            self.assertTrue(all(r['source_id'] in self.state['public_sources'] for r in result['results']))
            self.assertLessEqual(len(results[-1]['content']),4500)
            return {'content':'来源目录完整，可以继续核查。'}
        brain.model_message=AsyncMock(side_effect=model)
        with patch('agent.web_search_tool.WebSearchTool.search',AsyncMock(return_value={'status':'ok','results':rows})):
            await brain.run_tools([{'role':'user','content':'查查这个说法'}],self.work,self.state)


if __name__=='__main__':unittest.main()
