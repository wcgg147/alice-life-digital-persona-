import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from urllib.parse import urlsplit,parse_qs

from agent.web_search_tool import WebSearchTool
from cognition.exploration import Explorer,normalize_result
from cognition.loop import CognitiveLoop
from cognition.state import Ledger

TEXT='这是实际取回的词条导言样例，介绍一个概念的背景，不代表读过整篇文章，也不是对原评论含义的独立核实。'*3
SEARCH={'query':{'search':[{'pageid':123,'title':'示例概念','snippet':'这里只是检索片段','timestamp':'2026-11-01T00:00:00Z'}]}}
EXTRACT={'query':{'pages':[{'pageid':123,'title':'示例概念','extract':TEXT}]}}

class WikiSearchTests(unittest.TestCase):
    def test_article_query_selects_verbatim_body_beyond_prefix_with_offsets(self):
        prefix = 'Historical reinforcement learning project. ' * 180
        passage = '\n== Reasoning model pricing ==\nThe model API costs 600 dollars per million output tokens. '
        body = prefix + passage + ('Other unrelated projects. ' * 250)
        raw = json.dumps({'query': {'pages': [{'pageid': 123, 'title': 'Products', 'extract': body}]}})
        tool = WebSearchTool(provider='wikipedia')
        with patch.object(tool, '_request', return_value=raw):
            result = asyncio.run(tool.read_wikipedia_article('en', 123, query='reasoning model API pricing 600 dollars output tokens'))
        self.assertIn('600 dollars per million output tokens', result['text'])
        self.assertLessEqual(len(result['text']), 5000)
        self.assertGreater(result['source_text_start'], 0)
        self.assertEqual(result['text'], body[result['source_text_start']:result['source_text_end']])
        self.assertTrue(result['text_truncated'])
        self.assertEqual(result['evidence_kind'], 'encyclopedia_text_excerpt')
        import hashlib
        self.assertEqual(result['response_sha256'], hashlib.sha256(raw.encode()).hexdigest())

    def test_unmatched_article_query_keeps_real_prefix_without_snippet_substitution(self):
        body = TEXT * 50
        raw = json.dumps({'query': {'pages': [{'pageid': 123, 'title': 'Actual page', 'extract': body}]}})
        with patch.object(WebSearchTool, '_request', return_value=raw):
            result = asyncio.run(WebSearchTool(provider='wikipedia').read_wikipedia_article('en', 123, query='nonexistentkeyword'))
        self.assertEqual(result['text'], body[:5000])
        self.assertEqual(result['source_text_start'], 0)

    def test_sparse_plain_long_query_expands_once_keeps_originals_and_fetches_real_intros(self):
        query='OpenAI o1 API pricing per million tokens 600 dollars'
        extra={'query':{'search':[{'pageid':123,'title':'original'},{'pageid':456,'title':'expanded'}]}}
        pages={'query':{'pages':[{'pageid':123,'extract':TEXT},{'pageid':456,'extract':'Actual expanded introductory text.'}]}}
        tool=WebSearchTool(provider='wikipedia',max_results=2)
        with patch.object(tool,'_request',side_effect=[json.dumps(SEARCH),json.dumps(extra),json.dumps(pages)]) as request:
            result=asyncio.run(tool.search(query))
        self.assertEqual(request.call_count,3)
        self.assertEqual(parse_qs(urlsplit(request.call_args_list[1].args[0]).query)['srsearch'],[' OR '.join(query.split())])
        self.assertEqual([r['page_id'] for r in result['results']],[123,456])
        self.assertEqual(result['search_queries'][0]['query'],query)
        self.assertEqual(result['search_queries'][1]['mode'],'or_expansion')
        normalized=normalize_result(result,query,'wikipedia')
        self.assertEqual(normalized['search_queries'],result['search_queries'])
        self.assertEqual(len(normalized['discoveries']),2)

    def test_failed_optional_expansion_preserves_original_retrieved_hits(self):
        tool=WebSearchTool(provider='wikipedia',max_results=2)
        with patch.object(tool,'_request',side_effect=[json.dumps(SEARCH),json.dumps({'error':{'code':'busy'}}),json.dumps(EXTRACT)]):
            result=asyncio.run(tool.search('original concept long plain query for sparse search'))
        self.assertEqual(result['status'],'ok');self.assertEqual(len(result['results']),1)
        self.assertEqual(result['search_queries'][1]['status'],'error')

    def test_explicit_query_constraints_and_sufficient_hits_are_not_relaxed(self):
        queries=['"OpenAI o1" API pricing per million tokens',
                 'OpenAI o1 API pricing per million tokens -600',
                 'OpenAI o1 API pricing intitle:tokens per million',
                 'OpenAI AND o1 API pricing per million tokens']
        for query in queries:
            tool=WebSearchTool(provider='wikipedia',max_results=2);tool._cache={}
            with patch.object(tool,'_request',side_effect=[json.dumps(SEARCH),json.dumps(EXTRACT)]) as request:
                result=asyncio.run(tool.search(query))
            self.assertEqual(request.call_count,2);self.assertEqual(len(result['search_queries']),1)
        tool=WebSearchTool(provider='wikipedia',max_results=1)
        with patch.object(tool,'_request',side_effect=[json.dumps(SEARCH),json.dumps(EXTRACT)]) as request:
            asyncio.run(tool.search('sufficient hits long query does not need expanding'))
        self.assertEqual(request.call_count,2)

    def setUp(self):WebSearchTool._cache={}

    def test_fixed_api_query_and_actual_matching_extract_are_cached(self):
        tool=WebSearchTool(provider='wikipedia',max_results=3)
        with patch.object(tool,'_request',side_effect=[json.dumps(SEARCH),json.dumps(EXTRACT)]) as request:
            result=asyncio.run(tool.search('OpenAI o1 简介'))
            cached=asyncio.run(tool.search('OpenAI o1 简介'))
        self.assertEqual(request.call_count,2);self.assertFalse(result['cached']);self.assertTrue(cached['cached'])
        urls=[c.args[0] for c in request.call_args_list]
        self.assertTrue(all(urlsplit(u).hostname=='zh.wikipedia.org' for u in urls))
        self.assertEqual(parse_qs(urlsplit(urls[0]).query)['srsearch'],['OpenAI o1 简介'])
        self.assertEqual(parse_qs(urlsplit(urls[1]).query)['pageids'],['123'])
        self.assertEqual(result['results'][0]['body_text'],TEXT)
        self.assertEqual(result['results'][0]['evidence_kind'],'encyclopedia_intro_extract')

    def test_missing_extract_is_not_replaced_by_a_search_snippet(self):
        tool=WebSearchTool(provider='wikipedia')
        with patch.object(tool,'_request',side_effect=[json.dumps(SEARCH),json.dumps({'query':{'pages':[{'pageid':456,'extract':TEXT}]}})]):
            result=asyncio.run(tool.search('test concept'))
        self.assertFalse(result['results'])

    def test_no_hits_and_api_errors_are_not_fabricated_successful_documents(self):
        tool=WebSearchTool(provider='wikipedia')
        with patch.object(tool,'_request',return_value=json.dumps({'query':{'search':[]}})) as request:
            result=asyncio.run(tool.search('no such entity'))
        self.assertEqual(request.call_count,1);self.assertFalse(result['results'])
        with patch.object(tool,'_request',return_value=json.dumps({'error':{'code':'badvalue'}})):
            result=asyncio.run(tool.search('different query'))
        self.assertEqual(result['status'],'error');self.assertEqual(result['error_code'],'wikipedia_api_error')

    def test_provenance_must_match_provider_host_language_and_page_id(self):
        item={'url':'https://zh.wikipedia.org/?curid=123','title':'概念','snippet':'片段',
              'page_id':123,'language':'zh','body_text':TEXT,'content_status':'fetched','evidence_kind':'encyclopedia_intro_extract'}
        for change,provider in [({},'bing_rss'),({'url':'https://example.org/?curid=123'},'wikipedia'),
                                ({'page_id':456},'wikipedia'),({'language':'en'},'wikipedia')]:
            r=normalize_result({'status':'ok','results':[{**item,**change}]},'query',provider)
            self.assertNotIn('retrieved_text',r['discoveries'][0])

    def test_real_search_directory_survives_restart_and_reads_article_without_html_fetch(self):
        class Model:
            async def complete(self,messages):return {'action':'search','resource_id':'','reason':'查询一个概念背景',
                'goal':{'id':'g','status':'active'},'search_query':'示例概念','search_provider':'wikipedia',
                'predict_success':True,'next_check_seconds':60}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.sqlite3';ledger=Ledger(path)
            with ledger.db:ledger.db.execute('INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?)',
                ('alice','g','这个概念的背景是什么？','text','fixture:question','active',1,'public',''))
            cfg={'cognition_search_providers':['wikipedia']};explorer=Explorer(ledger,lambda:cfg)
            class Env:
                async def observe(self):return [],[]
                def exploration_capabilities(self):return explorer.capabilities()
                async def search(self,*a,**k):return await explorer.search(*a,**k)
            with patch.object(WebSearchTool,'_request',side_effect=[json.dumps(SEARCH),json.dumps(EXTRACT)]):
                out=asyncio.run(CognitiveLoop(ledger,Model(),Env(),daily_cap=0).tick(opportunity_source='experiment'))
            self.assertTrue(out['catalog_found']);self.assertFalse(out['met_need']);self.assertFalse(ledger.view()['inquiry_model'])
            ledger.close();ledger=Ledger(path);explorer=Explorer(ledger,lambda:cfg)
            try:
                resource=explorer.resources()[0]
                self.assertEqual(resource['family'],'encyclopedia_article')
                full=TEXT+'这是导言之外的正文内容。'
                with patch('agent.public_page_reader.read_public_page',new_callable=AsyncMock) as generic, \
                     patch.object(WebSearchTool,'_request',return_value=json.dumps({'query':{'pages':[{'pageid':123,'title':'示例概念','extract':full}]}})) as api:
                    text=asyncio.run(explorer.read(resource))
                generic.assert_not_awaited();self.assertEqual(text['text'],full)
                self.assertNotIn('exintro',parse_qs(urlsplit(api.call_args.args[0]).query))
                self.assertEqual(text['evidence_kind'],'encyclopedia_text_excerpt')
                self.assertFalse(text['primary_observed']);self.assertFalse(text['audio_observed'])
                self.assertEqual(text['body_origin'],'mediawiki_api_response')
                cfg['cognition_search_enabled']=False
                denied=asyncio.run(explorer.read(resource));self.assertEqual(denied['status'],'error')
                self.assertEqual(denied['text'],'')
            finally:ledger.close()

    def test_article_failure_cannot_be_reported_as_a_successful_intro_read(self):
        tool=WebSearchTool(provider='wikipedia')
        with patch.object(tool,'_request',return_value=json.dumps({'error':{'code':'badvalue'}})):
            out=asyncio.run(tool.read_wikipedia_article('en',123))
        self.assertEqual(out['status'],'error');self.assertEqual(out['text'],'')
        with patch.object(tool,'_request') as request:
            out=asyncio.run(tool.read_wikipedia_article('localhost',123))
        request.assert_not_called();self.assertEqual(out['status'],'error')

    def test_five_word_sparse_query_expands_and_transient_urlerror_retries_once(self):
        import urllib.error
        query='Anthropic distillation model extraction policy'
        tool=WebSearchTool(provider='wikipedia',max_results=2)
        with patch.object(tool,'_request',side_effect=[json.dumps({'query':{'search':[]}}),json.dumps(SEARCH),json.dumps(EXTRACT)]) as request:
            result=asyncio.run(tool.search(query))
        self.assertEqual(request.call_count,3)
        self.assertEqual(result['search_queries'][1]['mode'],'or_expansion')
        self.assertEqual(len(result['results']),1)
        class FakeResp:
            headers=type('H',(),{'get_content_charset':lambda self:'utf-8'})()
            def __enter__(self):return self
            def __exit__(self,*a):return False
            def read(self,n):return b'{"ok":true}'
        with patch('urllib.request.urlopen',side_effect=[urllib.error.URLError('timed out'),FakeResp()]) as uopen:
            self.assertEqual(tool._request('https://en.wikipedia.org/w/api.php'), '{"ok":true}')
            self.assertEqual(uopen.call_count,2)
