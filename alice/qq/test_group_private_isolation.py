# -*- coding: utf-8 -*-
import json
import tempfile
import time
import unittest
from pathlib import Path
import sys

API_PATH = Path(__file__).resolve().parents[2] / 'alice'
if str(API_PATH) not in sys.path:
    sys.path.insert(0, str(API_PATH))

from channel import Store, Gate
from social_memory import SocialMemory
from agent.companion_contract import render_companion_limits, render_companion_social_contract


class GroupPrivateIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.db'
        self.store = Store(self.path)
        self.config = {
            'enabled': True,
            'expected_qq': '1',
            'allowed_groups': ['20001'],
            'allowed_private_users': ['10001'],
            'account_owner_qq': '10001',
        }
        self.memory = SocialMemory(self.config, self.store)
        self.gate = Gate(self.config, self.store)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def make_event(self, mid, actor='10001', group='20001', text='你好', at=True):
        return {
            'post_type': 'message',
            'message_type': 'private' if group.startswith('private:') else 'group',
            'self_id': 1,
            'group_id': group,
            'user_id': actor,
            'message_id': mid,
            'time': time.time(),
            'message': ([{'type': 'at', 'data': {'qq': '1'}}] if at else []) + [{'type': 'text', 'data': {'text': text}}],
        }

    def test_private_memories_do_not_leak_into_group_recall(self):
        # 1. Simulate private dialogue in private:10001
        priv_event = self.make_event(101, actor='10001', group='private:10001', text='我最喜欢黑白企鹅淋雨表情包', at=False)
        self.memory.observe(priv_event, scope='private:10001')
        self.store.message('conv-priv', priv_event, 'user', '我最喜欢黑白企鹅淋雨表情包', 101, True)
        self.store.message('conv-priv', priv_event, 'assistant', '收下了，惨得好笑的那种', 102, True)
        self.store.db.execute(
            "INSERT INTO qq_social_facts(fact_key, scope, subject_qq, category, quotation, source_message_id, source_row, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ('fact1', 'private:10001', '10001', 'preference', '我最喜欢黑白企鹅淋雨表情包', '101', 1, time.time(), 'ok')
        )
        self.store.db.commit()

        # 2. User 10001 speaks in group 20001: "会不会发表情包"
        group_work = {
            'key': 'gkey',
            'group': '20001',
            'actor': '10001',
            'received_at': time.time(),
            'text': '会不会发表情包',
            'event': self.make_event(201, actor='10001', group='20001', text='会不会发表情包', at=True),
        }
        packet = self.memory.recall(group_work)

        # 3. Verify private memories NEVER leak into group recall
        packet_json = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn('黑白企鹅', packet_json)
        self.assertNotIn('收下了，惨得好笑', packet_json)
        for report in packet.get('person', {}).get('quoted_self_reports', []):
            self.assertFalse(str(report.get('scope', '')).startswith('private:'))

    def test_group_continuation_without_repeat_at(self):
        t0 = 1000.0
        # Turn 1: User mentions Alice in group
        e1 = self.make_event(1, actor='10001', group='20001', text='在吗', at=True)
        e1['time'] = t0
        w1 = self.gate.admit(e1, now=t0)
        self.assertIsNotNone(w1)
        self.assertEqual(w1['trigger'], 'mention')

        # Turn 2: User responds within 30s WITHOUT mentioning Alice -> accepted as continuation
        t1 = t0 + 25.0
        e2 = self.make_event(2, actor='10001', group='20001', text='最近忙啥呢', at=False)
        e2['time'] = t1
        w2 = self.gate.admit(e2, now=t1)
        self.assertIsNotNone(w2)
        self.assertEqual(w2['trigger'], 'conversation_continuation')

        # Interruption: Another group member sends a message in the group
        t2 = t1 + 10.0
        e_other = self.make_event(3, actor='10002', group='20001', text='大家下午好', at=False)
        e_other['time'] = t2
        w_other = self.gate.admit(e_other, now=t2)
        self.assertIsNone(w_other)  # other member without @ is ignored

        # Turn 3: Original user speaks again without @ -> ignored because interruption broke 1-on-1 focus
        t3 = t2 + 10.0
        e3 = self.make_event(4, actor='10001', group='20001', text='怎么不理我了', at=False)
        e3['time'] = t3
        w3 = self.gate.admit(e3, now=t3)
        self.assertIsNotNone(w3)
        self.assertEqual(w3['trigger'], 'conversation_continuation')

    def test_witty_limits_and_companion_contract(self):
        limits = render_companion_limits()
        self.assertIn('搜不了，别考我了直接报答案吧', limits)
        self.assertIn('绝不搬出「我没有搜索工具/模型没有该功能」等客服说教', limits)
        self.assertIn('保持真实风趣的人格与微吐槽', limits)

        contract = render_companion_social_contract()
        self.assertIn('不为了讨好而认同', contract)


if __name__ == '__main__':
    unittest.main()
