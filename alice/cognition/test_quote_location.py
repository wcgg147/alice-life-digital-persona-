"""Location guarantees, not semantic correctness or receipt admission."""
import unittest

from cognition.quote_location import bind_quote_selection, locate_quote, locate_quote_range


class QuoteLocationTests(unittest.TestCase):
    def source(self, text='date evidence', **extra):
        return {'episode_id': 'e1', 'source_ref': 'source-one',
                'spans': [{'id': 's2160', 'start': 2160, 'end': 2160+len(text), 'text': text}], **extra}

    def test_derive_id_and_offsets_from_exact_content(self):
        result = locate_quote('evidence', [self.source()])
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['location']['span_id'], 's2160')
        self.assertEqual((result['location']['start'], result['location']['end']), (2165, 2173))

    def test_repeated_occurrences_do_not_pick_first(self):
        result = locate_quote('same', [self.source('same same')])
        self.assertEqual(result['status'], 'ambiguous')
        self.assertIsNone(result['location'])

    def test_duplicate_across_sources_does_not_pick_owner(self):
        other = {**self.source(), 'episode_id': 'e2', 'source_ref': 'source-two'}
        self.assertEqual(locate_quote('evidence', [self.source(), other])['status'], 'ambiguous')

    def test_scope_removal_cannot_reuse_cached_location(self):
        self.assertEqual(locate_quote('evidence', [self.source()])['status'], 'resolved')
        self.assertEqual(locate_quote('evidence', [])['status'], 'unmatched')
        self.assertEqual(locate_quote('evidence', [self.source(revoked=True)])['status'], 'unmatched')

    def test_no_fuzzy_or_cross_span_repair(self):
        source = self.source('right text')
        source['spans'].append({'id':'s2400','start':2400,'end':2410,'text':'continues.'})
        for quote in ('Right text', 'right textcontinues.', '', ' '):
            self.assertNotEqual(locate_quote(quote, [source])['status'], 'resolved')

    def test_bad_offset_or_duplicate_identity_fails_closed(self):
        with self.assertRaises(ValueError):
            locate_quote('evidence', [self.source(), self.source()])
        source = self.source(); source['spans'][0]['end'] += 1
        with self.assertRaises(ValueError):
            locate_quote('evidence', [source])

    def test_field_failure_not_silently_repaired(self):
        selection = {'matches': [{'fragment': 'English fragment', 'quotes': ['evidence']}]}
        result = bind_quote_selection(selection, '中文命题', [self.source()])
        self.assertEqual(result['status'], 'invalid_fields')
        self.assertEqual(result['refs'], [])
        self.assertEqual(result['matches'][0]['locations'][0]['status'], 'resolved')
        self.assertEqual(selection['matches'][0]['fragment'], 'English fragment')

    def test_quote_only_contract_rejects_model_id_and_empty_selection(self):
        result = bind_quote_selection({'matches': [{'fragment': 'claim', 'quotes': [
            {'text': 'evidence', 'span_id': 'wrong'}]}]}, 'claim', [self.source()])
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['refs'], [])
        self.assertEqual(bind_quote_selection({'matches': []}, 'claim', [self.source()])['status'], 'unresolved')

    def test_valid_interface_uses_location_id(self):
        result = bind_quote_selection({'matches': [{'fragment': 'claim', 'quotes': ['evidence']}]},
                                      'claim', [self.source()])
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['refs'], [{'episode_id': 'e1', 'span_id': 's2160'}])

    def test_range_never_joins_a_gap_or_changes_punctuation(self):
        source=self.source('left')
        source['spans'].append({'id':'next','start':2200,'end':2205,'text':'right'})
        self.assertEqual(locate_quote_range('leftright',[source])['status'],'unmatched')
        self.assertEqual(locate_quote_range('Left',[source])['status'],'unmatched')

    def test_range_unique_across_all_sources_not_just_single_spans(self):
        source=self.source('left')
        source['spans'].append({'id':'next','start':2164,'end':2169,'text':'right'})
        self.assertEqual(locate_quote_range('leftright',[source])['status'],'resolved')
        other=self.source('leftright');other['episode_id']='other'
        self.assertEqual(locate_quote_range('leftright',[source,other])['status'],'ambiguous')


if __name__ == '__main__':
    unittest.main()
