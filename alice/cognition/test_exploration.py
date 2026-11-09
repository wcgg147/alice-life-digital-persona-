import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cognition.decision import validate
from cognition.exploration import Explorer, catalog, normalize_result, public_url
from cognition.loop import CognitiveLoop
from cognition.state import Ledger

TEXT = '观测记录解释了变化原因：当温度降低时，金属片收缩并推动开关，使窗户关闭。这是材料给出的部分因果链。' * 3


class Sequence:
    requires_inquiry = True
    def __init__(self, rows):
        self.rows = iter(rows)
    async def complete(self, messages):
        return next(self.rows)


class Search:
    def __init__(self, provider='bing_rss'):
        self.calls = 0; self.provider = provider
    async def search(self, query):
        self.calls += 1
        item = {'url':'https://example.org/study','title':'温度与关窗机制','snippet':'这里只是一个搜索片段'}
        if self.provider == 'crossref':
            item.update(url='https://doi.org/10.1234/example',doi='10.1234/example',body_text=TEXT,
                        evidence_kind='research_abstract',content_status='fetched')
        return {'status':'ok','results':[item]}


class Env:
    def __init__(self, explorer):
        self.explorer = explorer
    async def observe(self):
        return [{k:v for k,v in r.items() if k!='_entry'} for r in self.explorer.resources()], []
    def exploration_capabilities(self):
        return self.explorer.capabilities()
    async def search(self, query, goal, *, provider=None):
        return await self.explorer.search(query, goal, provider=provider)
    async def read(self, resource):
        return await self.explorer.read(resource)


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path)
        with self.ledger.db:
            self.ledger.db.execute('INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?)',
                                  ('alice','g','温度变化为什么会让窗户关闭？','text','fixture:question','active',1,'public',''))
        self.cfg={};self.tool=Search();self.page_calls=0
        async def reader(url):
            self.page_calls+=1
            return {'status':'ok','url':url,'text':TEXT,'evidence_kind':'public_html_text'}
        self.reader=reader
        self.explorer=Explorer(self.ledger,lambda:self.cfg,search_tool=self.tool,page_reader=reader)

    def tearDown(self):
        self.ledger.close();self.tmp.cleanup()

    def search(self, provider='bing_rss'):
        d={'action':'search','search_query':'temperature metal strip window mechanism','search_provider':provider,
           'resource_id':'','reason':'已有材料没有解释这个问题，寻找来源','goal':{'id':'g','status':'active'},
           'predict_success':True,'next_check_seconds':60}
        return asyncio.run(CognitiveLoop(self.ledger,Sequence([d]),Env(self.explorer)).tick())

    def read(self):
        resource=self.explorer.resources()[0]
        d={'action':'read','resource_id':resource['id'],'reason':'读取刚发现的实际材料',
           'goal':{'id':'g','status':'active'},'predict_success':True,'next_check_seconds':60,
           'inquiry':{'kind':'mechanism','criterion':'材料有温度触发关窗的因果解释','expected_progress':.6}}
        a={'verdict':'useful','evidence_id':'s0000','answer':'金属片收缩推动开关','reason':'取得部分因果链',
           'schema_version':2,'progress_type':'partial_answer','criterion_status':'partial','novelty':'new',
           'missing':'尚缺后续执行过程','prior_episode_ids':[]}
        return asyncio.run(CognitiveLoop(self.ledger,Sequence([d,a]),Env(self.explorer)).tick())

    def test_search_persists_candidates_then_read_learns_after_reopen(self):
        first=self.search()
        self.assertTrue(first['catalog_found']);self.assertFalse(first['met_need']);self.assertEqual(self.page_calls,0)
        self.assertFalse(self.ledger.view()['inquiry_model']);self.assertFalse(self.ledger.view()['strategies'])
        self.ledger.close();self.ledger=Ledger(self.path)
        self.explorer=Explorer(self.ledger,lambda:self.cfg,search_tool=self.tool,page_reader=self.reader)
        second=self.read()
        self.assertEqual(second['status'],'observed');self.assertEqual(self.page_calls,1)
        self.assertEqual(second['inquiry_assessment']['status'],'assessed')
        self.assertEqual(self.ledger.view()['inquiry_model'][0]['support_count'],1)

    def test_revoking_discovery_also_removes_derived_read_learning(self):
        first=self.search();second=self.read()
        self.ledger.revoke(first['episode_id'])
        self.assertFalse(catalog(self.ledger));self.assertFalse(self.ledger.view()['inquiry_model'])
        row=self.ledger.db.execute('SELECT revoked FROM episodes WHERE id=?',(second['episode_id'],)).fetchone()
        self.assertEqual(row[0],1)

    def test_source_revoked_during_fetch_is_discarded(self):
        first=self.search()
        async def revoked_reader(url):
            self.ledger.revoke(first['episode_id'])
            return {'status':'ok','text':TEXT}
        self.explorer.page_reader=revoked_reader
        out=self.read()
        self.assertEqual(out['status'],'tool_error')
        self.assertFalse(self.ledger.view()['inquiry_model'])

    def test_private_scope_search_rejected_before_network(self):
        context={'resources':[],'goals':self.ledger.view()['goals'],'episodes':[],
                 'scope':'private:11','exploration':self.explorer.capabilities()}
        raw={'action':'search','search_query':'test','search_provider':'bing_rss','resource_id':'','reason':'探索',
             'goal':{'id':'g','status':'active'},'predict_success':True,'next_check_seconds':60}
        with self.assertRaisesRegex(ValueError,'unauthorized'):
            validate(raw,context)
        self.assertEqual(self.tool.calls,0)

    def test_cached_author_abstract_is_marked_as_abstract_not_fulltext(self):
        self.tool=Search('crossref');self.explorer.search_tool=self.tool
        self.search('crossref');self.assertEqual(self.page_calls,0)
        self.assertEqual(self.explorer.resources()[0]['family'],'crossref_abstract')
        out=self.read();self.assertEqual(self.page_calls,0)
        result=self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?',(out['episode_id'],)).fetchone()[0]
        r=json.loads(result);self.assertEqual(r['evidence_kind'],'research_abstract');self.assertFalse(r['primary_observed'])

    def test_disabled_search_and_paused_goals_hide_candidates(self):
        self.search();self.cfg['cognition_search_enabled']=False
        self.assertFalse(self.explorer.resources());self.assertFalse(self.explorer.capabilities()['can_search'])
        self.cfg.clear()
        with self.ledger.db:self.ledger.db.execute("UPDATE goals SET status='paused'")
        self.assertFalse(self.explorer.resources())

    def test_provider_contract_does_not_promote_an_ordinary_search_snippet(self):
        item={'url':'https://example.org/study','body_text':TEXT,'content_status':'fetched','evidence_kind':'research_abstract','doi':'10.1/no'}
        result=normalize_result({'status':'ok','results':[item]},'query','bing_rss')
        self.assertNotIn('retrieved_text',result['discoveries'][0])
        self.assertEqual(result['discoveries'][0]['content_kind'],'search_snippet')

    def test_bilibili_discovery_survives_reopen_and_reads_metadata_not_video(self):
        url='https://www.bilibili.com/video/BV1g9dRYDEse'
        self.explorer.search_tool=None
        with patch('alice_reading.fetch_search',return_value=[{'url':url,'title':'选手介绍'}]) as search:
            out=self.search('bilibili')
        search.assert_called_once()
        self.assertTrue(out['catalog_found']);self.assertFalse(out['met_need'])
        self.assertFalse(self.ledger.view()['inquiry_model'])
        self.ledger.close();self.ledger=Ledger(self.path)
        self.explorer=Explorer(self.ledger,lambda:self.cfg,page_reader=self.reader)
        resource=self.explorer.resources()[0]
        self.assertEqual(resource['family'],'bilibili_metadata_comments')
        with patch('alice_reading.fetch_article',new_callable=AsyncMock,return_value=TEXT) as article:
            result=asyncio.run(self.explorer.read(resource))
        article.assert_awaited_once_with(url,comment_pages=1,comment_limit=12)
        self.assertEqual(self.page_calls,0)
        self.assertEqual(result['text'],TEXT)
        self.assertEqual(result['body_origin'],'bilibili_view_reply_api')
        self.assertFalse(result['primary_observed']);self.assertFalse(result['audio_observed'])
        self.assertFalse(self.ledger.view()['inquiry_model'])  # Fetching alone is not learning.

    def test_bilibili_search_permission_withdrawn_during_fetch_discards_results(self):
        self.explorer.search_tool=None
        def fetch(*args):
            self.cfg['cognition_search_enabled']=False
            return [{'url':'https://www.bilibili.com/video/BV1g9dRYDEse','title':'介绍'}]
        with patch('alice_reading.fetch_search',side_effect=fetch):
            out=asyncio.run(self.explorer.search('选手介绍',{'id':'g'},provider='bilibili'))
        self.assertEqual(out['error_code'],'search_not_authorized');self.assertFalse(out['discoveries'])

    def test_bilibili_read_revoked_during_fetch_does_not_become_evidence(self):
        self.tool.search=AsyncMock(return_value={'status':'ok','results':[
            {'url':'https://www.bilibili.com/video/BV1g9dRYDEse','title':'介绍'}]})
        first=self.search('bilibili');resource=self.explorer.resources()[0]
        async def fetch(*args,**kwargs):
            other=Ledger(self.path)
            try:other.revoke(first['episode_id'])
            finally:other.close()
            return TEXT
        with patch('alice_reading.fetch_article',side_effect=fetch):
            out=asyncio.run(self.explorer.read(resource))
        self.assertEqual(out['error_code'],'discovery_revoked');self.assertEqual(out['text'],'')

    def test_bilibili_query_is_not_silently_truncated(self):
        self.explorer.search_tool=None
        with patch('alice_reading.fetch_search') as fetch:
            out=asyncio.run(self.explorer.search('很长的关键词'*10,{'id':'g'},provider='bilibili'))
        fetch.assert_not_called()
        self.assertEqual(out['error_code'],'bilibili_query_too_long')

    def test_public_url_rejects_local_credentials_and_non_https(self):
        for u in ['http://example.org','https://127.0.0.1/a','https://user:pass@127.0.0.1/a',
                  'file:///tmp/data','https://host.local/a','https://example.org:8080/a']:
            self.assertFalse(public_url(u))
        self.assertEqual(public_url('https://example.org/a#frag'),'https://example.org/a')

    def test_permission_withdrawn_while_searching_discards_catalog(self):
        class Tool:
            async def search(inner,q):
                self.cfg['cognition_search_enabled']=False
                return {'status':'ok','results':[{'url':'https://example.org/x','title':'材料'}]}
        self.explorer.search_tool=Tool()
        out=self.search()
        self.assertEqual(out['status'],'tool_error');self.assertFalse(catalog(self.ledger))

    def test_invalid_search_prediction_is_logged_without_network_or_learning(self):
        d={'action':'search','search_query':'temperature','search_provider':'bing_rss','resource_id':'','reason':'探索原因',
           'goal':{'id':'g','status':'active'},'predict_success':.45,'next_check_seconds':60}
        out=asyncio.run(CognitiveLoop(self.ledger,Sequence([d]),Env(self.explorer)).tick())
        self.assertEqual(out['status'],'decision_error');self.assertEqual(self.tool.calls,0)
        self.assertEqual(self.ledger.view()['episodes'][0]['result']['validation_code'],'prediction_required')
        self.assertFalse(catalog(self.ledger))

    def test_tampering_with_a_saved_catalog_invalidates_the_receipt(self):
        first=self.search()
        with self.ledger.db:
            row=self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?',(first['episode_id'],)).fetchone()
            r=json.loads(row[0]);r['discoveries'][0]['url']='https://other.example.org/changed'
            self.ledger.db.execute('UPDATE episodes SET result_json=? WHERE id=?',(json.dumps(r),first['episode_id']))
        self.assertFalse(catalog(self.ledger))


if __name__=='__main__':unittest.main()
