import asyncio,json,unittest
from unittest.mock import MagicMock,patch
from agent.web_search_tool import WebSearchTool
from cognition.exploration import normalize_result
from agent.web_search_tool import _NoSearchAuthRedirect

class DeepSeekSearchTests(unittest.TestCase):
    def setUp(self):WebSearchTool._cache.clear()

    def test_fixed_modelink_request_returns_candidates_not_fetched_evidence(self):
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps({'results':[{'url':'https://example.org/page','title':'原始标题',
            'snippet':'短摘要','summary':'服务生成的长摘要','content':'即使返回正文也不能在搜索阶段当作已读'}]}).encode()
        tool=WebSearchTool(provider='deepseek_search',api_key='fixture-secret')
        with patch('urllib.request.OpenerDirector.open',return_value=response) as request:
            result=asyncio.run(tool.search('公开问题'))
        wire=request.call_args.args[0]
        self.assertEqual(wire.full_url,'https://api.qnaigc.com/v1/tools/search')
        self.assertEqual(wire.get_header('Authorization'),'Bearer fixture-secret')
        self.assertEqual(json.loads(wire.data),{'query':'公开问题','count':5,'need_content':False})
        normalized=normalize_result(result,'公开问题','deepseek_search')
        self.assertEqual(normalized['discoveries'][0]['content_kind'],'search_snippet')
        self.assertNotIn('retrieved_text',normalized['discoveries'][0])
        self.assertEqual(normalized['discoveries'][0]['snippet'],'短摘要')
        self.assertTrue(result['api_response_sha256'])

    def test_missing_key_and_oversized_query_do_not_send_requests(self):
        with patch('urllib.request.OpenerDirector.open') as request:
            result=asyncio.run(WebSearchTool(provider='deepseek_search').search('公开问题'))
            self.assertEqual(result['error_code'],'missing_api_key')
            result=asyncio.run(WebSearchTool(provider='deepseek_search',api_key='fixture').search('字'*129))
            self.assertEqual(result['error_code'],'invalid_query_length')
            request.assert_not_called()

    def test_cache_isolated_by_search_key_and_unsafe_candidates_are_rejected(self):
        tool=WebSearchTool(provider='deepseek_search',api_key='fixture-a')
        with patch.object(tool,'_search_deepseek',return_value={'status':'ok','results':[]}) as call:
            asyncio.run(tool.search('公开问题'));asyncio.run(tool.search('公开问题'));self.assertEqual(call.call_count,1)
        other=WebSearchTool(provider='deepseek_search',api_key='fixture-b')
        with patch.object(other,'_search_deepseek',return_value={'status':'ok','results':[]}) as call:
            asyncio.run(other.search('公开问题'));self.assertEqual(call.call_count,1)
        value=normalize_result({'status':'ok','results':[{'url':'https://127.0.0.1/private'}]},'公开问题','deepseek_search')
        self.assertFalse(value['discoveries'])

    def test_authenticated_provider_redirect_is_not_followed(self):
        with self.assertRaisesRegex(ValueError,'redirect_rejected'):
            _NoSearchAuthRedirect().redirect_request(None,None,302,'redirect',{},'https://other.example/')

if __name__=='__main__':unittest.main()
