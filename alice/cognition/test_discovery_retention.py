"""Historical leads remain actionable without inventing or promoting evidence."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cognition.exploration import Explorer, catalog, normalize_result, source_followup
from cognition.state import Ledger, digest
from unittest.mock import patch


class DiscoveryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'ledger.sqlite3'
        self.ledger = Ledger(self.path)
        with self.ledger.db:
            self.ledger.db.execute('INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?)',
                ('alice', 'g', 'Which event and price does this comment refer to?',
                 'text', 'fixture:question', 'active', 1, 'public', ''))

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def discover(self, provider, prefix):
        goal = dict(self.ledger.db.execute('SELECT * FROM goals WHERE id=?', ('g',)).fetchone())
        decision = {'action': 'search', 'goal': goal, 'search_provider': provider,
                    'search_query': prefix, 'next_check_seconds': 60}
        ep = self.ledger.begin({}, decision)
        result = normalize_result({'status': 'ok', 'results': [
            {'url': f'https://example.org/{prefix}/{i}', 'title': f'{prefix} {i}',
             'snippet': 'An unverified search lead'} for i in range(3)]}, prefix, provider)
        result.update(catalog_found=True, source_ref=f'search:{ep}', text='')
        result['receipt'] = digest(result)
        self.ledger.finish(ep, 'observed', result, 60)
        return ep

    def fill_window(self):
        old = self.discover('wikipedia', 'older-lead')
        self.discover('bing_rss', 'newer-web')
        self.discover('bilibili', 'newer-video')
        self.discover('bing_rss', 'more-web')
        latest = self.discover('bing_rss', 'latest-web')
        return old, latest

    def test_older_provider_survives_newer_searches_and_reopen(self):
        old, latest = self.fill_window()
        self.ledger.close()
        self.ledger = Ledger(self.path)
        resources = catalog(self.ledger)
        self.assertEqual(len(resources), 6)
        self.assertIn(old, {r['discovery_episode_id'] for r in resources})
        self.assertEqual(sum(r['discovery_episode_id'] == latest for r in resources), 3)
        self.assertNotIn('retrieved_text', resources[-1])
        self.assertFalse(self.ledger.view()['inquiry_model'])

    def test_latest_search_keeps_all_candidates_for_source_followup(self):
        _, latest = self.fill_window()
        context = {'resources': catalog(self.ledger), 'goals': self.ledger.view()['goals']}
        followup = source_followup(self.ledger, context, latest)
        self.assertEqual(len(followup['candidates']), 3)
        self.assertTrue(all(r['discovery_episode_id'] == latest for r in followup['candidates']))

    def test_equal_started_times_keep_the_last_search_complete(self):
        with patch('cognition.state.time.time', return_value=1000):
            _, latest = self.fill_window()
        self.assertEqual(sum(r['discovery_episode_id'] == latest for r in catalog(self.ledger)), 3)

    def test_retained_lead_can_be_read_and_withdrawal_still_blocks_it(self):
        old, _ = self.fill_window()
        explorer = Explorer(self.ledger, lambda: {}, page_reader=self.read_page)
        resource = next(r for r in explorer.resources() if r['discovery_episode_id'] == old)
        result = asyncio.run(explorer.read(resource))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['discovery_episode_id'], old)
        self.ledger.revoke(old)
        result = asyncio.run(explorer.read(resource))
        self.assertEqual(result['error_code'], 'discovery_revoked')
        self.assertEqual(result['text'], '')
        self.assertFalse(self.ledger.view()['inquiry_model'])

    async def read_page(self, url):
        return {'status': 'ok', 'url': url, 'text': 'Actual adapter output for this test.'}

    def test_wikipedia_retained_query_drives_excerpt_and_preserves_provenance(self):
        ep = self.discover('wikipedia', 'self-selected-query')
        row = self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?', (ep,)).fetchone()
        result = json.loads(row[0])
        result['discoveries'][0].update(content_kind='encyclopedia_intro_extract', language='en', page_id=123)
        result.pop('receipt')
        result['receipt'] = digest(result)
        with self.ledger.db:
            self.ledger.db.execute('UPDATE episodes SET result_json=? WHERE id=?', (json.dumps(result), ep))
        explorer = Explorer(self.ledger, lambda: {})
        resource = explorer.resources()[0]
        page = {'status': 'ok', 'text': 'Actual body excerpt', 'source_text_start': 7000,
                'source_text_end': 7019, 'source_text_sha256': 'body-hash', 'response_sha256': 'response-hash'}
        with patch('agent.web_search_tool.WebSearchTool.read_wikipedia_article', return_value=page) as read:
            observed = asyncio.run(explorer.read(resource))
        read.assert_awaited_once_with('en', 123, query='self-selected-query')
        for key in ('source_text_start', 'source_text_end', 'source_text_sha256', 'response_sha256'):
            self.assertEqual(observed[key], page[key])

    def test_older_tampered_private_foreign_or_inactive_sources_stay_hidden(self):
        old, _ = self.fill_window()
        row = self.ledger.db.execute('SELECT result_json FROM episodes WHERE id=?', (old,)).fetchone()
        original = row[0]
        result = json.loads(original)
        result['discoveries'][0]['title'] = 'Tampered title'
        with self.ledger.db:
            self.ledger.db.execute('UPDATE episodes SET result_json=? WHERE id=?', (json.dumps(result), old))
        self.assertNotIn(old, {r['discovery_episode_id'] for r in catalog(self.ledger)})
        with self.ledger.db:
            self.ledger.db.execute('UPDATE episodes SET result_json=?,scope=?,actor=? WHERE id=?',
                                   (original, 'private:11', '11', old))
        self.assertNotIn(old, {r['discovery_episode_id'] for r in catalog(self.ledger)})
        with self.ledger.db:
            self.ledger.db.execute('UPDATE episodes SET scope=?,actor=?,owner=? WHERE id=?',
                                   ('public', '', 'other', old))
        self.assertNotIn(old, {r['discovery_episode_id'] for r in catalog(self.ledger)})
        with self.ledger.db:
            self.ledger.db.execute('UPDATE goals SET status=?', ('paused',))
        self.assertFalse(catalog(self.ledger))


if __name__ == '__main__':
    unittest.main()
