"""Experience-grounded opportunity estimates, not fixed tastes or persona facts.

Progress, feedback availability, repetition and autonomous returns are separate.
Scores use explicit initial priors; the LLM may override the suggested focus.
"""
from __future__ import annotations

import re

from .inquiry import receipt_matches, grounded_assessments
from .state import digest, encoded


def _signature(text):
    return digest(re.sub(r'[\W_]+', '', str(text).casefold()))


def derive(goals, history):
    tracks={g['id']:{'goal':g,'events':[],'seen_reads':set(),'seen_answers':set(),'seen_measurements':set(),
                     'legacy_unmeasured':0,'runtime_visits':0,'runtime_returns':0,'return_evidence':[]} for g in goals}
    # Reuse the experiment module's measurement-admission rules. A changed
    # candidate order or repeated/noisy grading cannot manufacture progress.
    from .world_model import derive as experiments
    measured={};repeated_measurements=set()
    for belief in experiments(history,set(tracks),include_measurements=True):
        for eid in belief['_measurement_trials']:
            measured[eid]=belief['plan']
        repeated_measurements.update(belief['_repeated_measurement_trials'])
    current=None;seen_runtime=set()
    admitted=grounded_assessments(history)
    for ep in reversed(history):
        if ep.get('revoked'):
            continue
        d,r=ep['decision'],ep['result'];gid=(d.get('goal') or {}).get('id');track=tracks.get(gid)
        opportunity=ep.get('opportunity') or {}
        natural=opportunity.get('source')=='runtime' and not opportunity.get('external_pending')
        if natural and ep['status']=='rest':
            current=None
        if natural and track and ep['status']=='observed' and receipt_matches(r):
            track['runtime_visits']+=1
            if current!=gid and gid in seen_runtime:
                track['runtime_returns']+=1;track['return_evidence'].append(ep['id'])
            seen_runtime.add(gid);current=gid
        if not track:
            continue
        if d.get('action')=='read':
            if not d.get('inquiry'):
                track['legacy_unmeasured']+=1
                continue
            event={'id':ep['id'],'feedback_known':False,'novel':False,'kind':'feedback_missing'}
            a=r.get('inquiry_assessment') or {}
            checked=admitted.get(ep['id'],(None,{}))[1]
            valid=bool(checked) and checked['verdict']!='uncertain'
            if valid:
                event['feedback_known']=True
                source=(r['source_ref'],digest(r['text']))
                answer=_signature(checked['answer'])
                if source in track['seen_reads']:
                    event['kind']='repeated_read'
                elif checked.get('novelty')=='known':
                    event['kind']='semantic_confirmation'
                elif checked['verdict']=='not_useful':
                    event['kind']='no_support'
                elif answer in track['seen_answers']:
                    event['kind']='confirmation'
                else:
                    event.update(kind=checked.get('progress_type','legacy_unspecified_progress'),novel=True)
                track['seen_reads'].add(source)
                if checked['verdict']=='useful':track['seen_answers'].add(answer)
            elif a.get('status')=='withdrawn':
                continue  # Withdrawn interpretations are not negative learning.
            track['events'].append(event)
        elif (d.get('action')=='search' and ep['status'] in {'observed','tool_error'}
              and r.get('status') in {'ok','error'} and receipt_matches(r)):
            # Retrieval consumes an opportunity but does not yet measure an
            # answer. Empty results and network errors are missing question
            # feedback, not proof that the question has no answer.
            track['events'].append({'id':ep['id'],'feedback_known':False,'novel':False,
                                    'kind':'search_without_question_feedback'})
        elif d.get('action')=='experiment':
            if r.get('experiment_revoked'):
                continue
            event={'id':ep['id'],'feedback_known':False,'novel':False,'kind':'unresolved_measurement'}
            if ep['id'] in measured:
                plan=measured[ep['id']];rates=r['experiment_assessment']['rates']
                outcome=sorted((encoded(plan[arm]),rates[arm]) for arm in ['control','treatment'])
                key=digest(outcome)
                event.update(feedback_known=True,kind='measurement_confirmation' if key in track['seen_measurements'] else 'new_measured_outcome',
                             novel=key not in track['seen_measurements'])
                track['seen_measurements'].add(key)
            else:
                assessment=r.get('experiment_assessment') or {}
                if ep['id'] in repeated_measurements:
                    event.update(feedback_known=True,kind='repeated_measurement')
                elif assessment.get('status')=='assessed':
                    # Duplicate snapshots and disputed measurements do not
                    # reward curiosity merely because a scorer was called again.
                    event['kind']='repeated_or_disputed_measurement'
            track['events'].append(event)
    profiles=[];cues=[]
    for gid,t in tracks.items():
        events=t['events'];window=events[-6:]
        known=sum(e['feedback_known'] for e in window);novel=sum(e['novel'] for e in window)
        gain=(novel+1)/(known+2)
        availability=(known+1)/(len(window)+2)
        recent=events[-3:]
        stalled=len(recent)>=3 and all(e['feedback_known'] and not e['novel'] for e in recent)
        missing=len(recent)>=3 and all(not e['feedback_known'] for e in recent)
        previous=events[-12:-6]
        change=None
        if len(window)>=2 and len(previous)>=2:
            change=round(novel/len(window)-sum(e['novel'] for e in previous)/len(previous),4)
        profile={'goal_id':gid,'opportunity_estimate':round(gain*availability,4),
                 'estimated_new_progress':round(gain,4),'feedback_availability':round(availability,4),
                 'window_attempts':len(window),'recent_new_progress':novel,'recent_measured_feedback':known,
                 'progress_rate_change':change,'stalled':stalled,'feedback_missing':missing,
                 'legacy_unmeasured':t['legacy_unmeasured'],'total_new_progress':sum(e['novel'] for e in events),
                 'recent_full_answers':sum(e['novel'] and e['kind']=='new_answer' for e in window),
                 'recent_partial_answers':sum(e['novel'] and e['kind']=='partial_answer' for e in window),
                 'recent_search_attempts':sum(e['kind']=='search_without_question_feedback' for e in window),
                 'recent_confirmations':sum(e['kind'] in {'semantic_confirmation','confirmation','repeated_read'} for e in window),
                 'runtime_visits':t['runtime_visits'],'runtime_returns':t['runtime_returns'],
                 'supporting_episodes':[e['id'] for e in events[-6:]],
                 'basis':'fallible_semantic_assessments_and_admitted_measurements',
                 'limitation':'exact answer deduplication is not semantic novelty verification'}
        profiles.append(profile)
        if t['runtime_returns']>=2 and profile['total_new_progress']>0:
            cues.append({'goal_id':gid,'question':t['goal']['question'],'status':'provisional_attention_cue',
                         'runtime_returns':t['runtime_returns'],'supporting_episodes':t['return_evidence'][-6:],
                         'not_equivalent_to':'established_personality_or_subjective_interest'})
    active=[p for p in profiles if tracks[p['goal_id']]['goal']['status']=='active']
    active.sort(key=lambda p:-p['opportunity_estimate'])
    return {'goals':profiles,'recommended_goal_id':active[0]['goal_id'] if active else None,
            'rest_or_change_method_supported':bool(active) and all(p['stalled'] or p['feedback_missing'] for p in active),
            'interest_cues':cues,'policy':'advisory_progress_times_feedback_estimate',
            'priors':'Beta(1,1); six-attempt recency window; thresholds are engineering choices, not learned motives'}


def order_goals(goals, motivation):
    scores={p['goal_id']:p['opportunity_estimate'] for p in motivation.get('goals',[])}
    return sorted(goals,key=lambda g:(g['status']!='active',-scores.get(g['id'],.25)))


def source_feedback(history):
    """Goal-independent, scoped source-family evidence; not a learned interest.

    Only explicit schema-2 judgments measure novelty. Exact source/content and
    answer deduplication are conservative safeguards, not semantic verification.
    Withdrawn judgments are neutral. Caller supplies visibility-filtered history.
    """
    admitted=grounded_assessments(history);families={};selected=[]
    for ep in history:  # newest surviving interpretation per exact observation
        d,r=ep.get('decision') or {},ep.get('result') or {}
        if (ep.get('revoked') or ep.get('status')!='observed' or d.get('action') not in {'read','inspect'}
                or r.get('status')!='ok' or not r.get('text') or not receipt_matches(r)):continue
        family=r.get('family')
        if not family:continue
        f=families.setdefault(family,{'seen_refs':set(),'seen_content':set(),'seen_answers':set(),
                                     'events':[],'legacy_unmeasured':0})
        f['seen_refs'].add(r['source_ref'])
        if d.get('action')=='inspect':continue  # Seen does not mean learned.
        a=r.get('inquiry_assessment') or {}
        if a.get('status')=='withdrawn':continue
        key=(r['source_ref'],digest(r['text']))
        if key in f['seen_content']:continue
        f['seen_content'].add(key)
        selected.append(ep)
    for ep in reversed(selected):
        d,r=ep['decision'],ep['result'];f=families[r['family']]
        checked=admitted.get(ep['id'],(None,{}))[1]
        if not d.get('inquiry') or (checked and checked.get('schema_version')!=2):
            f['legacy_unmeasured']+=1;continue
        known=bool(checked) and checked['verdict']!='uncertain'
        novel=False
        if known and checked['verdict']=='useful':
            answer=_signature(checked['answer'])
            novel=(checked['progress_type'] in {'new_answer','partial_answer'}
                   and checked['novelty']=='new' and answer not in f['seen_answers'])
            f['seen_answers'].add(answer)
        f['events'].append({'id':ep['id'],'known':known,'novel':novel})
    out={}
    for family,f in families.items():
        events=f['events'][-6:];known=sum(e['known'] for e in events);gains=sum(e['novel'] for e in events)
        a,b=gains+1,known-gains+1
        out[family]={'unique_attempts':len(events),'measured_feedback':known,'new_progress':gains,
            'estimated_new_progress':round(a/(a+b),4),
            'feedback_availability':round((known+1)/(len(events)+2),4),
            'opportunity_estimate':round(a/(a+b)*(known+1)/(len(events)+2),4),
            'estimate_variance':round(a*b/((a+b)**2*(a+b+1)),6),
            'legacy_unmeasured':f['legacy_unmeasured'],'seen_source_refs':sorted(f['seen_refs']),
            'supporting_episodes':[e['id'] for e in events]}
    return out


def candidate_opportunities(resources,feedback):
    """Project only visible candidates; do not leak the full historical catalog."""
    out=[]
    for resource in resources:
        f=feedback.get(resource['family'],{})
        out.append({'resource_id':resource['id'],'family':resource['family'],
            **{k:f.get(k,default) for k,default in {
                'unique_attempts':0,'measured_feedback':0,'new_progress':0,
                'estimated_new_progress':.5,'feedback_availability':.5,'opportunity_estimate':.25,
                'estimate_variance':round(1/12,6),'legacy_unmeasured':0,'supporting_episodes':[]}.items()},
            'source_identity_seen':resource.get('source_ref') in f.get('seen_source_refs',[]),
            'basis':'fallible_source_family_progress_with_Beta_1_1_engineering_priors',
            'limitation':'six_unique_attempts; family_transfer_is_unverified; uncertainty_is_not_reward; not_interest_or_skill_improvement'})
    return out


def memory_goal_origins(history,recent_ids):
    """Bounded, receipt-grounded retrieval for new questions, not assigned interests.

    Visibility is supplied by Ledger.view. Unmeasured or unhelpful observations
    may still inspire a different question; old progress is not a global taste.
    """
    from .self_model import topic_key
    out=[];topics={};seen=set()
    for ep in history:
        d,r=ep['decision'],ep['result']
        if (ep.get('revoked') or ep['id'] in recent_ids or ep['status']!='observed'
                or d.get('action')!='read' or r.get('status')!='ok' or not r.get('text') or not receipt_matches(r)):continue
        topic=topic_key(d.get('resource_id',''),r.get('family',''))
        key=(r['source_ref'],digest(r['text']))
        if key in seen or topics.get(topic,0)>=2 or (topic not in topics and len(topics)>=6):continue
        seen.add(key);topics[topic]=topics.get(topic,0)+1
        out.append({'id':ep['id'],'source_ref':r['source_ref'],'family':r.get('family'),
                    'topic':topic,'receipt':r['receipt'],'scope':ep.get('scope','public'),'actor':ep.get('actor',''),
                    'excerpt':r['text'][:240],'basis':'previously_observed_text_not_a_new_goal_or_interest'})
        if len(out)>=12:break
    return out
