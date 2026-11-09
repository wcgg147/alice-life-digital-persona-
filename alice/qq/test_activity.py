import tempfile,unittest
from pathlib import Path
from channel import Store
from activity import claim,release

class AttentionTests(unittest.TestCase):
    def test_todays_exception_preserves_usage_and_expires_tomorrow(self):
        from datetime import datetime,timezone,timedelta
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'shared.db');zone=timezone(timedelta(hours=8))
            today=datetime(2026,10,29,12,tzinfo=zone);tomorrow=today+timedelta(days=1)
            try:
                store.db.execute('INSERT INTO budget VALUES (?,?)',('2026-10-29',300))
                store.db.execute('INSERT INTO budget VALUES (?,?)',('2026-10-30',300))
                store.db.execute('INSERT INTO budget_day_overrides VALUES (?,?,?)',('2026-10-29',None,'owner authorized today only'))
                store.db.commit();store.reserve(300,now=today.timestamp())
                self.assertEqual(store.db.execute('SELECT calls FROM budget WHERE day=?',('2026-10-29',)).fetchone()[0],301)
                with self.assertRaises(RuntimeError):store.reserve(300,now=tomorrow.timestamp())
            finally:store.db.close()

    def test_text_and_call_connections_share_one_attention_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            text=Store(Path(tmp)/'shared.db');call=Store(Path(tmp)/'shared.db')
            try:
                self.assertTrue(claim(text,'text',100))
                self.assertFalse(claim(call,'call',101))
                release(call,'wrong-owner')
                self.assertFalse(claim(call,'call',102))
                release(text,'text')
                self.assertTrue(claim(call,'call',103))
            finally:text.db.close();call.db.close()

    def test_dead_process_lease_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'shared.db')
            try:
                self.assertTrue(claim(store,'dead',100))
                self.assertTrue(claim(store,'replacement',161))
            finally:store.db.close()
