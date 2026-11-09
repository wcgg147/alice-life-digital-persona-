"""An unread source can be appraised at a due opportunity without forcing a read."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from cognition.exploration import normalize_result, source_followup, retained_source_messages
from cognition.loop import CognitiveLoop
from cognition.state import Ledger, digest
from cognition.test_source_followup import Model, Env, SEARCH, READ, REST, ASSESS, URL


class RetainedSourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'ledger.sqlite3'
        self.l = Ledger(self.path)
        self.env = Env(self.l)
        self.env.cfg['cognition_retained_source_selection_enabled'] = True
        ep = self.l.begin({}, {'action': 'rest', 'goal': {'id': 'g', 'question': '这个温控装置的触发阈值是多少？',
            'need': 'text', 'origin': 'fixture:observation', 'status': 'active'}})
        self.l.finish(ep, 'rest', {}, 0)

    def tearDown(self):
        self.l.close()
        self.tmp.cleanup()

    def parent(self, url=URL):
        goal = dict(self.l.db.execute('SELECT * FROM goals WHERE id=?', ('g',)).fetchone())
        ep = self.l.begin({}, {**SEARCH, 'goal': goal})
        result = normalize_result({'status': 'ok', 'results': [
            {'url': url, 'title': '实际来源候选', 'snippet': '可能包含阈值，也可能不相关'}]}, 'threshold', 'bing_rss')
        result.update(catalog_found=True, source_ref=f'search:{ep}', text='')
        result['receipt'] = digest(result)
        self.l.finish(ep, 'observed', result, 600)
        return ep

    def tick(self, model):
        return asyncio.run(CognitiveLoop(self.l, model, self.env, daily_cap=0).tick(opportunity_source='experiment'))

    def test_due_choice_can_read_one_of_multiple_old_sources_then_use_normal_decision(self):
        first = self.parent()
        second = self.parent('https://example.org/another')
        model = Model([READ, ASSESS])
        result = self.tick(model)
        row = self.l.db.execute('SELECT * FROM episodes WHERE id=?', (result['episode_id'],)).fetchone()
        before, decision = json.loads(row['before_json']), json.loads(row['decision_json'])
        self.assertEqual(decision['selection_process'], 'retained_source_selection')
        self.assertEqual(decision['parent_search_id'], first)
        self.assertEqual(before['opportunity']['cause'], 'retained_search_results')
        offered = json.loads(model.inputs[0][-1]['content'])['search_results']
        self.assertEqual({p['id'] for p in offered}, {first, second})
        self.assertEqual(result['inquiry_assessment']['progress_type'], 'new_answer')
        normal = Model([REST])
        self.tick(normal)
        self.assertIn('resources', json.loads(normal.inputs[0][-1]['content']))

    def test_end_consumes_offered_parents_once_without_read_or_goal_completion(self):
        first = self.parent()
        second = self.parent('https://example.org/another')
        result = self.tick(Model([REST]))
        decision = json.loads(self.l.db.execute('SELECT decision_json FROM episodes WHERE id=?', (result['episode_id'],)).fetchone()[0])
        self.assertEqual(set(decision['considered_search_ids']), {first, second})
        self.assertEqual(self.env.reads, 0)
        self.assertEqual(self.l.db.execute('SELECT status FROM goals WHERE id=?', ('g',)).fetchone()[0], 'active')
        normal = Model([REST])
        self.tick(normal)
        context = json.loads(normal.inputs[0][-1]['content'])
        self.assertIn('resources', context)
        self.assertIsNone(source_followup(self.l, context, first))
        self.assertIsNone(source_followup(self.l, context, second))

    def test_selection_cannot_search_complete_goal_or_read_an_unoffered_source(self):
        for decision in (SEARCH, {**REST, 'goal': {'id': 'g', 'status': 'completed'}},
                         {**READ, 'resource_id': 'not-offered'}):
            parent = self.parent(f'https://example.org/{len(str(decision))}')
            result = self.tick(Model([decision]))
            self.assertEqual(result['status'], 'decision_error')
            self.assertEqual((self.env.searches, self.env.reads), (0, 0))

    def test_revocation_or_disable_during_selection_blocks_read(self):
        parent = self.parent()
        result = self.tick(Model([READ], hook=lambda: self.l.revoke(parent)))
        self.assertEqual(result['status'], 'decision_error')
        self.assertEqual(self.env.reads, 0)
        self.parent()
        def disable():
            self.env.cfg['cognition_retained_source_selection_enabled'] = False
        result = self.tick(Model([READ], hook=disable))
        self.assertEqual(result['status'], 'decision_error')
        self.assertEqual(self.env.reads, 0)

    def test_private_or_tampered_parent_is_never_offered(self):
        parent = self.parent()
        with self.l.db:
            self.l.db.execute('UPDATE episodes SET scope=?,actor=? WHERE id=?', ('private:11', '11', parent))
        model = Model([REST])
        self.tick(model)
        self.assertIn('resources', json.loads(model.inputs[0][-1]['content']))
        parent = self.parent()
        row = self.l.db.execute('SELECT result_json FROM episodes WHERE id=?', (parent,)).fetchone()
        result = json.loads(row[0]); result['query'] = 'changed'
        with self.l.db:
            self.l.db.execute('UPDATE episodes SET result_json=? WHERE id=?', (json.dumps(result), parent))
        model = Model([REST])
        self.tick(model)
        self.assertIn('resources', json.loads(model.inputs[0][-1]['content']))

    def test_disabled_or_competing_goal_keeps_general_choice(self):
        self.parent()
        self.env.cfg['cognition_retained_source_selection_enabled'] = False
        model = Model([REST]); self.tick(model)
        self.assertIn('resources', json.loads(model.inputs[0][-1]['content']))
        self.env.cfg['cognition_retained_source_selection_enabled'] = True
        with self.l.db:
            self.l.db.execute('INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?)',
                ('alice', 'other', '另一个问题', 'text', 'fixture:other', 'active', 1, 'public', ''))
        model = Model([REST]); self.tick(model)
        self.assertIn('resources', json.loads(model.inputs[0][-1]['content']))

    def test_goal_changes_during_selection_cannot_install_read(self):
        self.parent()
        def change_goal():
            with self.l.db:
                self.l.db.execute('UPDATE goals SET question=? WHERE id=?', ('已经变化的问题', 'g'))
        result = self.tick(Model([READ], hook=change_goal))
        self.assertEqual(result['status'], 'decision_error')
        self.assertEqual(self.env.reads, 0)

    def test_running_loop_does_not_shorten_saved_rest_for_retained_sources(self):
        self.parent()
        next_at = time.time() + 3600
        with self.l.db:
            self.l.db.execute('UPDATE cognitive_state SET next_at=?', (next_at,))
        stop = Path(self.tmp.name) / 'STOP'
        async def during_wait(delay):
            stop.touch()
        model = Model([])
        with patch('cognition.loop.asyncio.sleep', side_effect=during_wait):
            asyncio.run(CognitiveLoop(self.l, model, self.env, daily_cap=0, stop_path=stop).run())
        self.assertFalse(model.inputs)
        self.assertEqual(self.l.view()['next_at'], next_at)

    def test_source_appraisal_receives_existing_plan_without_turning_it_into_answers(self):
        parent = self.parent()
        resources = asyncio.run(self.env.observe())[0]
        plan = {'parts': [{'question': '阈值是多少', 'evidence_needed': '真实说明的数值'}],
                'basis': 'fallible_model_draft_not_knowledge'}
        context = {**self.l.view(), 'resources': resources, 'prospective_goal_plan': plan}
        payload = json.loads(retained_source_messages(context, [source_followup(self.l, context, parent)])[1]['content'])
        self.assertEqual(payload['prospective_goal_plan'], plan)
        self.assertFalse(payload['known_answers'])


if __name__ == '__main__':
    unittest.main()
