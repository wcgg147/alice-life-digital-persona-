import json,tempfile,unittest
from pathlib import Path
from channel import Store
from call_health import call_capability


class CallHealthTests(unittest.TestCase):
    def test_only_fresh_matching_owner_service_is_advertised(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'channel.sqlite3')
            cfg={'account_owner_qq':'owner','expected_qq':'self'};work={'group':'private:owner'}
            path=Path(tmp)/'native-call-health.json'
            state={'at':100,'account':'self','owner':'owner','available':True}
            try:
                self.assertEqual(call_capability(cfg,store,work,now=101),'')
                path.write_text(json.dumps(state),encoding='utf-8')
                self.assertIn('可以接听',call_capability(cfg,store,work,now=101))
                self.assertEqual(call_capability(cfg,store,{'group':'group:123'},now=101),'')
                self.assertEqual(call_capability(cfg,store,work,now=109),'')
                for changes in ({'available':False},{'account':'another-account'},{'owner':'different-owner'}):
                    path.write_text(json.dumps({**state,**changes}),encoding='utf-8')
                    self.assertEqual(call_capability(cfg,store,work,now=101),'')
                path.write_text(json.dumps(state),encoding='utf-8')
                (Path(tmp)/'voice-provider-unavailable.json').write_text(json.dumps({'account':'self','error':'voice_account_unavailable'}),encoding='utf-8')
                self.assertEqual(call_capability(cfg,store,work,now=101),'')
            finally:store.db.close()
