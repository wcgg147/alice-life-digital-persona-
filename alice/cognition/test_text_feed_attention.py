import asyncio
import unittest
from unittest.mock import patch

from cognition.environment import ReadingEnvironment
from cognition import idle_attention


FEED='https://www.qbitai.com/feed'
CARD={'title':'一篇候选文章','url':'https://www.qbitai.com/2026/11/123.html','source':FEED}


class TextFeedTests(unittest.IsolatedAsyncioTestCase):
    def environment(self,feeds=None):
        env=ReadingEnvironment.__new__(ReadingEnvironment)
        cfg={'cognition_text_feed_urls':feeds if feeds is not None else [FEED]}
        env.current_config=lambda:cfg;env.catalog_errors=[];env.payloads={}
        return env,cfg

    async def test_only_configured_catalog_and_cached_titles_not_body(self):
        env,_=self.environment()
        with patch('alice_reading._fetch_rss',return_value=[CARD]) as fetch:
            first=await env.text_feed_cards();second=await env.text_feed_cards()
        self.assertEqual(first,second);self.assertEqual(fetch.call_count,1)
        self.assertEqual(first[0]['family'],'public_article_text')
        self.assertNotIn('text',first[0]);self.assertEqual(env.payloads[first[0]['id']][0],'text_feed')
        env.payloads={}
        await env.text_feed_cards()
        self.assertIn(first[0]['id'],env.payloads)

    async def test_unconfigured_and_foreign_feed_links_never_enter_pool(self):
        env,_=self.environment([])
        with patch('alice_reading._fetch_rss') as fetch:
            self.assertEqual(await env.text_feed_cards(),[]);fetch.assert_not_called()
        env,_=self.environment()
        with patch('alice_reading._fetch_rss',return_value=[{**CARD,'url':'http://127.0.0.1/internal'}]):
            self.assertEqual(await env.text_feed_cards(),[])
        env,_=self.environment(['http://127.0.0.1/feed'])
        with patch('alice_reading._fetch_rss') as fetch:
            self.assertEqual(await env.text_feed_cards(),[]);fetch.assert_not_called()

    async def test_actual_reader_preview_is_bounded_and_disable_is_rechecked(self):
        env,cfg=self.environment()
        with patch('alice_reading._fetch_rss',return_value=[CARD]):card=(await env.text_feed_cards())[0]
        async def reader(url,**kwargs):return {'status':'ok','text':'原文'*2000,'response_sha256':'body-hash'}
        with patch('agent.public_page_reader.read_public_page',side_effect=reader) as request:
            preview=await env.inspect(card)
            self.assertEqual(len(preview['text']),1200);self.assertFalse(preview['primary_observed'])
            self.assertTrue(preview['text_truncated'])
            self.assertEqual(request.call_args.kwargs['resolver'],'system')
            cfg['cognition_text_feed_urls']=[]
            refused=await env.read(card);self.assertEqual(refused['status'],'error');self.assertEqual(request.call_count,1)

    async def test_disable_during_read_does_not_publish_body(self):
        env,cfg=self.environment()
        with patch('alice_reading._fetch_rss',return_value=[CARD]):card=(await env.text_feed_cards())[0]
        async def reader(url,**kwargs):
            cfg['cognition_text_feed_urls']=[]
            return {'status':'ok','text':'正文'}
        with patch('agent.public_page_reader.read_public_page',side_effect=reader):
            result=await env.read(card)
        self.assertEqual(result['status'],'error');self.assertNotIn('text',result)

    async def test_consumed_feed_head_does_not_hide_remaining_articles(self):
        env,_=self.environment()
        cards=[{**CARD,'url':f'https://www.qbitai.com/{i}'} for i in range(20)]
        # Exercise the fetch limit as well as downstream slicing. A mock that
        # always returns all items would miss truncation at the adapter boundary.
        with patch('alice_reading._fetch_rss',side_effect=lambda url,limit:cards[:limit]) as fetch:
            resources=await env.text_feed_cards()
            seen=[{'resource_id':r['id'],'source_identity_seen':True} for r in resources[:2]]
            context={'scope':'public','goals':[],'episodes':[],'resources':resources,
                     'motivation':{'candidate_opportunities':seen}}
            selected=idle_attention.candidates(context,{'cognition_idle_attention_enabled':True})
            self.assertTrue(selected,'unread articles behind the consumed feed head must remain available')
            self.assertTrue(all(r['source_ref'] not in {c['url'] for c in cards[:2]} for r in selected))
            self.assertLessEqual(len(selected),6)
            self.assertLessEqual(len(resources),12)
            # The cached reservoir must also serve later choices without a fetch.
            context['motivation']['candidate_opportunities'] += [
                {'resource_id':r['id'],'source_identity_seen':True} for r in selected]
            context['resources']=await env.text_feed_cards()
            later=idle_attention.candidates(context,{'cognition_idle_attention_enabled':True})
            self.assertTrue(later)
            self.assertFalse({r['id'] for r in selected}&{r['id'] for r in later})
            self.assertEqual(fetch.call_count,1)

    async def test_feed_reservoirs_are_interleaved_and_remain_titles_only(self):
        second='https://sspai.com/feed'
        env,_=self.environment([FEED,second])
        def catalog(url,limit):
            publisher='www.qbitai.com' if url==FEED else 'sspai.com'
            return [{**CARD,'url':f'https://{publisher}/{i}','source':url} for i in range(min(limit,20))]
        with patch('alice_reading._fetch_rss',side_effect=catalog):
            resources=await env.text_feed_cards()
        selected=idle_attention.candidates({'scope':'public','goals':[],'episodes':[],
            'resources':resources},{'cognition_idle_attention_enabled':True})
        self.assertEqual([r['feed_source'] for r in selected],[FEED,second]*3)
        self.assertLessEqual(len(resources),24)
        self.assertTrue(all('text' not in r for r in resources))
        self.assertTrue(all(r['id'] in env.payloads for r in selected))


class MixedAttentionTests(unittest.TestCase):
    def test_articles_are_visible_without_displacing_all_video_choices(self):
        videos=[{'id':f'bili:{i}','source_ref':f'https://www.bilibili.com/video/{i}','family':'bilibili_metadata_comments','title':str(i)} for i in range(6)]
        articles=[{'id':f'article:{i}','source_ref':f'https://www.qbitai.com/{i}','family':'public_article_text','title':str(i)} for i in range(4)]
        context={'scope':'public','goals':[],'episodes':[],'resources':videos+articles}
        selected=idle_attention.candidates(context,{'cognition_idle_attention_enabled':True})
        self.assertEqual(len(selected),6)
        self.assertEqual([r['family'] for r in selected].count('public_article_text'),3)
        choice=idle_attention.decision({'judgments':[{'i':i+1,'open':False,'why':'暂时不想看'} for i in range(6)],'pick':None,'why_pick':'休息'},selected)
        self.assertEqual(choice['action'],'rest')
        context['inspection_sources']=[{'source_ref':r['source_ref']} for r in articles]
        self.assertTrue(all(r['family']=='bilibili_metadata_comments' for r in idle_attention.candidates(context,{'cognition_idle_attention_enabled':True})))


if __name__=='__main__':unittest.main()
