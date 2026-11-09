"""Source-bound provisional questions. Observation is not learning or persona."""
from .state import digest
from .inquiry import receipt_matches, evidence_spans


def source(ep):
    r=ep.get('result') or {}
    if (ep.get('revoked') or ep.get('status')!='observed'
            or ep.get('decision',{}).get('action')!='inspect' or r.get('status')!='ok'
            or not r.get('source_ref') or not r.get('text') or not receipt_matches(r)):return None
    item={'id':ep['id'],'receipt':r['receipt'],'source_ref':r['source_ref'],
            'family':r['family'],'text':r['text'][:1200],
            'evidence_spans':evidence_spans(r['text'][:1200]),
            'basis':'actual_limited_observation_not_learning_or_interest'}
    intent=r.get('attention_intent')
    if (isinstance(intent,dict) and intent.get('basis')=='fallible_pre_observation_choice_not_rule_or_knowledge'
            and isinstance(intent.get('reason'),str) and 1<=len(intent['reason'])<=240):
        item['attention_intent']=dict(intent)
    return item


def checked_question(raw,s):
    if not isinstance(raw,dict):raise ValueError('invalid_wonder')
    question,reason=raw.get('question'),raw.get('reason')
    span=next((x for x in s['evidence_spans'] if x['id']==raw.get('evidence_id')),None)
    if (not isinstance(question,str) or not 5<=len(question.strip())<=240
            or not isinstance(reason,str) or not 1<=len(reason.strip())<=240
            or raw.get('status') not in {'open','deferred','dismissed'} or not span):
        raise ValueError('invalid_or_ungrounded_wonder')
    result={'source_episode_id':s['id'],'source_receipt':s['receipt'],'evidence_id':span['id'],
            'quote':span['text'],'question':question.strip(),'status':raw['status'],'reason':reason.strip()}
    result['id']=digest([s['source_ref'],digest(s['text']),result['question']])[:24]
    return result


def validate_wonder(raw,context):
    if not isinstance(raw,dict):raise ValueError('invalid_wonder')
    existing=next((q for q in context.get('open_questions',[]) if q['id']==raw.get('id')),None)
    if raw.get('id') and not existing:raise ValueError('unknown_provisional_question')
    data={**(existing or {}),**raw}
    s=next((s for s in context.get('inspection_sources',[]) if s['id']==data.get('source_episode_id')),None)
    if not s:raise ValueError('wonder_requires_visible_inspection')
    result=checked_question(data,s)
    if existing and result['id']!=existing['id']:raise ValueError('provisional_question_identity_changed')
    return result


def derive(history,goals):
    sources={ep['id']:s for ep in history if (s:=source(ep))}
    questions={}
    for ep in history:  # latest surviving disposition wins
        if ep.get('revoked'):continue
        r=ep.get('result') or {}
        d=ep.get('decision') or {}
        if (d.get('selection_process')=='idle_goal_selection'
                and ep.get('status') in {'observed','rest','tool_error','unknown'}):
            proposals=d.get('question_appraisal') or []
        elif ep.get('status')=='wondered':proposals=[r.get('wonder') or {}]
        elif (ep.get('status')=='noticed' and receipt_matches(r)
                and ep.get('decision',{}).get('action')=='notice'
                and ep['decision'].get('source_episode_id')==r.get('source_episode_id')
                and ep['decision'].get('source_receipt')==r.get('source_receipt')):
            proposals=r.get('questions') or []
        else:continue
        if not isinstance(proposals,list):continue
        for raw in proposals:
            if not isinstance(raw,dict):continue
            if ep.get('status')=='noticed' and (raw.get('source_episode_id')!=r.get('source_episode_id')
                    or raw.get('source_receipt')!=r.get('source_receipt')):continue
            s=sources.get(raw.get('source_episode_id'))
            if not s or raw.get('source_receipt')!=s['receipt']:continue
            try:q=checked_question(raw,s)
            except (ValueError,TypeError,KeyError):continue
            if q!=raw:continue
            questions.setdefault(q['id'],{**q,'disposition_episode_id':ep['id'],
                                         'basis':'fallible_question_proposal_not_a_goal_or_persona'})
    selected={digest([sources[g['origin']]['source_ref'],digest(sources[g['origin']]['text']),g['question']])[:24]
              for g in goals if g['origin'] in sources}
    pool=[q for q in questions.values() if q['status']!='dismissed'
          and q['id'] not in selected][:6]
    # Keep sources of offered questions available even after the working window.
    offered=[];seen=set()
    for sid in [q['source_episode_id'] for q in pool]+list(sources):
        s=sources[sid];key=(s['source_ref'],digest(s['text']))
        if key in seen:continue
        seen.add(key);offered.append(s)
        if len(offered)>=6:break
    return offered,pool
