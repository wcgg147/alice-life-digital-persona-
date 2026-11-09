import tempfile
import unittest
from pathlib import Path

from cognition.state import Ledger,digest


class SearchEffortTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3';self.ledger=Ledger(self.path)

    def tearDown(self):
        self.ledger.close();self.tmp.cleanup()

    def search(self,status='ok',found=False):
        goal={'id':'g','question':'实际问题','need':'text','origin':'fixture:g','status':'active'}
        ep=self.ledger.begin({}, {'action':'search','goal':goal})
        r={'status':status,'source_ref':'search:'+ep,'text':'','catalog_found':found,'discoveries':[],
           'provider':'bing_rss','query':'fixture query','family':'web_search_catalog','met_need':False}
        r['receipt']=digest(r);self.ledger.finish(ep,'observed' if status=='ok' else 'tool_error',r)
        return ep

    def profile(self):
        return self.ledger.view()['motivation']['goals'][0]

    def test_search_effort_reduces_feedback_opportunity_without_inventing_negative_knowledge(self):
        for status in ('ok','error','ok'):self.search(status)
        p=self.profile()
        self.assertEqual(p['window_attempts'],3)
        self.assertEqual(p['recent_search_attempts'],3)
        self.assertEqual(p['recent_measured_feedback'],0)
        self.assertEqual(p['total_new_progress'],0)
        self.assertTrue(p['feedback_missing'])
        self.assertFalse(p['stalled'])
        self.assertLess(p['opportunity_estimate'],.25)

    def test_candidates_alone_are_not_a_new_answer_and_revocation_removes_effort(self):
        ep=self.search(found=True)
        p=self.profile();self.assertEqual(p['recent_search_attempts'],1)
        self.assertEqual(p['recent_new_progress'],0)
        self.ledger.revoke(ep)
        self.assertEqual(self.profile()['window_attempts'],0)

    def test_tampered_result_does_not_become_search_effort(self):
        ep=self.search()
        with self.ledger.db:
            self.ledger.db.execute('UPDATE episodes SET result_json=? WHERE id=?',
                ('{"status":"error","receipt":"wrong","text":""}',ep))
        self.assertEqual(self.profile()['window_attempts'],0)
