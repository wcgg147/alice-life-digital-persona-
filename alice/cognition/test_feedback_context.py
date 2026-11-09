import json
import tempfile
import time
import unittest
from pathlib import Path

from cognition.state import Ledger
from cognition.social import SocialState


class FeedbackContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.ledger=Ledger(Path(self.tmp.name)/'ledger.sqlite3')
        self.social=SocialState(self.ledger);self.now=time.time()-100
        self.config={'enabled':True,'allowed_private_users':['11','22'],'account_owner_qq':'11'}

    def tearDown(self):
        self.ledger.close();self.tmp.cleanup()

    def put(self,mid,text,offset,role='person',actor='11'):
        event={'message_id':mid,'scope':'private:'+actor,'actor':actor,'role':role,'text':text,'at':self.now+offset}
        if role=='alice':event['receipts']=[{'kind':'fixture_delivery_receipt','message_id':mid}]
        return self.social.ingest(event,self.config)[0]

    def test_delayed_feedback_has_its_own_time_window_and_latest_history(self):
        reply=self.put('reply','实际被评价的原回复',1,'alice')
        feedback=self.put('feedback','你话题跳跃太大了吧',2)
        for n in range(8):self.put('later'+str(n),'后续话题'+str(n),n+3)
        context=self.social.context('private:11','11',feedback_event_id=feedback)
        window=context['feedback_context']
        self.assertEqual(window['event_id'],feedback)
        self.assertIn(reply,[r['id'] for r in window['social_events']])
        self.assertNotIn(reply,[r['id'] for r in context['social_events']])
        self.assertTrue(all(r['at']<=self.now+2 for r in window['social_events']))
        self.assertTrue(all(r['at']>self.now+2 for r in context['social_events']))
        self.assertTrue(next(r for r in window['social_events'] if r['id']==reply)['delivery']['receipts'])

    def test_foreign_scope_and_revoked_feedback_cannot_be_used(self):
        key=self.put('feedback','你话题跳跃太大了吧',2)
        with self.assertRaises(ValueError):self.social.context('private:22','22',feedback_event_id=key)
        self.social.revoke(key)
        with self.assertRaises(ValueError):self.social.context('private:11','11',feedback_event_id=key)

    def test_revoked_reply_is_not_reintroduced(self):
        reply=self.put('reply','被撤回的回复',1,'alice')
        key=self.put('feedback','你话题跳跃太大了吧',2)
        self.social.revoke(reply)
        context=self.social.context('private:11','11',feedback_event_id=key)
        self.assertNotIn(reply,json.dumps(context))

    def test_person_event_required_and_default_shape_preserved(self):
        reply=self.put('reply','普通回复',1,'alice')
        with self.assertRaises(ValueError):self.social.context('private:11','11',feedback_event_id=reply)
        self.assertNotIn('feedback_context',self.social.context('private:11','11'))

    def test_later_insert_with_same_timestamp_cannot_enter_feedback_window(self):
        reply=self.put('reply','当时的回复',1,'alice')
        key=self.put('feedback','你话题跳跃太大了吧',2)
        later=self.put('later','时间戳相同但后来入账的回复',2,'alice')
        context=self.social.context('private:11','11',feedback_event_id=key)
        ids=[r['id'] for r in context['feedback_context']['social_events']]
        self.assertIn(reply,ids);self.assertNotIn(later,ids)


if __name__=='__main__':unittest.main()
