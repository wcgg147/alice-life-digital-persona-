"""Exact locations for selected text, without model-supplied citation IDs.

Candidate utility, not wired into runtime admission. Callers must supply only
currently visible, surviving, receipt-verified sources. A resolved location
proves text occurrence, never entailment, permission, or learning progress.
"""
from hashlib import sha256
from copy import deepcopy


def locate_quote(quote, sources):
    """Resolve one exact quote in the supplied source window, without a cache.

    Multiple occurrences (even in one span) are ambiguous. Cross-span and fuzzy
    matches are not repaired. Positions refer to the original persisted text.
    """
    if not isinstance(quote, str) or not quote.strip():
        return {'status': 'invalid_quote', 'location': None, 'candidate_count': 0}
    locations = []
    identities = set()
    for source in sources:
        if source.get('revoked') or source.get('inquiry_revoked'):
            continue
        episode_id, source_ref = source.get('episode_id'), source.get('source_ref')
        if not isinstance(episode_id, str) or not episode_id or not isinstance(source_ref, str) or not source_ref:
            raise ValueError('invalid_quote_source')
        for span in source['spans']:
            span_id, text = span.get('id'), span.get('text')
            start, end = span.get('start'), span.get('end')
            if (not isinstance(span_id, str) or not span_id or not isinstance(text, str)
                    or type(start) is not int or type(end) is not int
                    or start < 0 or end != start + len(text)):
                raise ValueError('invalid_quote_span')
            identity = (episode_id, span_id)
            if identity in identities:
                raise ValueError('duplicate_quote_span')
            identities.add(identity)
            offset = text.find(quote)
            while offset >= 0:
                locations.append({'episode_id': episode_id, 'source_ref': source_ref,
                                  'span_id': span_id, 'start': start + offset,
                                  'end': start + offset + len(quote), 'quote': quote,
                                  'span_sha256': sha256(text.encode('utf-8')).hexdigest()})
                offset = text.find(quote, offset + 1)
    status = 'resolved' if len(locations) == 1 else 'ambiguous' if locations else 'unmatched'
    return {'status': status, 'location': locations[0] if status == 'resolved' else None,
            'candidate_count': len(locations)}


def bind_quote_selection(selection, claim, sources):
    """Quote-only candidate interface; invalid fields never become admitted refs.

    Per-quote locations remain inspectable even when the whole selection fails.
    This function does not decide which assertions the evidence supports.
    """
    errors, matches, refs = [], [], []
    if not isinstance(selection, dict) or set(selection) != {'matches'} or not isinstance(selection['matches'], list):
        return {'status': 'invalid_fields', 'errors': ['selection_shape'], 'matches': [], 'refs': []}
    for index, match in enumerate(selection['matches']):
        if (not isinstance(match, dict) or set(match) != {'fragment', 'quotes'}
                or not isinstance(match.get('quotes'), list)):
            errors.append({'index': index, 'reason': 'match_shape'})
            continue
        fragment = match['fragment']
        if not isinstance(fragment, str) or not fragment or fragment not in claim:
            errors.append({'index': index, 'reason': 'fragment_not_in_claim'})
        located = [locate_quote(quote, sources) for quote in match['quotes']]
        matches.append({'fragment': fragment, 'quotes': deepcopy(match['quotes']), 'locations': located})
        for result in located:
            if result['status'] == 'resolved':
                location = result['location']
                ref = {'episode_id': location['episode_id'], 'span_id': location['span_id']}
                if ref not in refs:
                    refs.append(ref)
    unresolved = any(r['status'] != 'resolved' for m in matches for r in m['locations'])
    # An empty evidence selection is not an accepted positive citation result.
    has_quote = any(m['quotes'] for m in matches)
    status = 'invalid_fields' if errors else 'unresolved' if unresolved or not has_quote else 'resolved'
    return {'status': status, 'errors': errors, 'matches': matches,
            'refs': refs if status == 'resolved' else []}


def locate_quote_range(quote, sources):
    """Exact unique occurrence across contiguous persisted span positions.

    Unlike locate_quote, a range may yield several original spans. Gaps are not
    joined, text is never normalized, and the caller still enforces ref limits.
    """
    sources = list(sources)
    checked = locate_quote(quote, sources)  # Validate source identities/offsets.
    if checked['status'] == 'invalid_quote':
        return {'status': 'invalid_quote', 'locations': [], 'candidate_count': 0}
    occurrences = []
    for source in sources:
        if source.get('revoked') or source.get('inquiry_revoked'):
            continue
        blocks = []
        for span in sorted(source['spans'], key=lambda s: s['start']):
            if blocks and blocks[-1][-1]['end'] == span['start']:
                blocks[-1].append(span)
            else:
                blocks.append([span])
        for block in blocks:
            text = ''.join(s['text'] for s in block)
            offset = text.find(quote)
            while offset >= 0:
                start = block[0]['start'] + offset
                end = start + len(quote)
                locations = []
                for span in block:
                    left, right = max(start, span['start']), min(end, span['end'])
                    if left < right:
                        locations.append({'episode_id': source['episode_id'], 'source_ref': source['source_ref'],
                                          'span_id': span['id'], 'start': left, 'end': right,
                                          'quote': span['text'][left-span['start']:right-span['start']],
                                          'span_sha256': sha256(span['text'].encode('utf-8')).hexdigest()})
                occurrences.append(locations)
                offset = text.find(quote, offset + 1)
    status = 'resolved' if len(occurrences) == 1 else 'ambiguous' if occurrences else 'unmatched'
    return {'status': status, 'locations': occurrences[0] if status == 'resolved' else [],
            'candidate_count': len(occurrences)}
