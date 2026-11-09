"""Evidence qualifications must survive the recent-episode window."""
import json
import unittest

from cognition import test_inquiry as fixtures
from cognition.decision import working_context
from cognition.state import Ledger


class AssessmentBoundaryTests(unittest.TestCase):
    setUp = fixtures.InquiryTests.setUp
    tearDown = fixtures.InquiryTests.tearDown
    read = fixtures.InquiryTests.read

    def test_original_assessment_reason_survives_window_reopen_and_withdrawal(self):
        caveat = '材料只给出气压与风速关系；没有说明这条评论是否指该机制。'
        result, _ = self.read(1, verdict='useful', assessment={'reason': caveat})
        eid = result['episode_id']
        original = self.ledger.db.execute('select result_json from episodes where id=?', (eid,)).fetchone()[0]
        gid = json.loads(self.ledger.db.execute('select decision_json from episodes where id=?', (eid,)).fetchone()[0])['goal']['id']
        for _ in range(9):
            ep = self.ledger.begin({}, {'action': 'rest', 'resource_id': '', 'predict_success': False})
            self.ledger.finish(ep, 'rest', {'status': 'chosen_rest'}, 600)
        path = self.ledger.path
        self.ledger.close()
        self.ledger = Ledger(path)
        state = self.ledger.view()
        self.assertNotIn(eid, [ep['id'] for ep in state['episodes']])
        context = working_context(state, [], [])
        entry = context['progress_memory'][gid][0]
        self.assertEqual(entry.get('assessment_reason'), caveat)
        self.assertEqual(entry['episode_id'], eid)
        self.assertEqual(entry['receipt'], json.loads(original)['receipt'])
        self.assertEqual(self.ledger.db.execute('select result_json from episodes where id=?', (eid,)).fetchone()[0], original)
        self.ledger.revoke_inquiry(eid, '该解释待重新核验')
        self.assertNotIn(gid, working_context(self.ledger.view(), [], [])['progress_memory'])


if __name__ == '__main__':
    unittest.main()
