"""Prospective subquestions after an actual unsuccessful method experiment.

Draft questions and retrieval ideas are not answers or a selected external
action. Each goal version gets at most one surviving automatic attempt.
"""
import asyncio
import json

from .inquiry import receipt_matches
from .observation_assimilation import current_source
from .question_state import goal_version
from .state import digest, encoded
from .world_model import judge

SYSTEM = '你是Alice针对自己已经采用的公开问题进行问题拆分与查证规划的过程。你不负责回答问题或选择兴趣。\n只依据真实原观察、当前问题、已有回答和实际失败，识别最多3个可独立查证且仍然未知的具体部分，并给出希望取得哪种证据。可以只保留一个整体问题，也可以不规划。评论中的说法、价格、昵称都未证实，不当作事实，不编造英文人名或指代。\n搜索没有结果不等于问题不可查；已失败的关键词组合不代表所有子问题、所有入口都无效。各入口实际用途在capabilities，按其用途自行选择查询。不要为追求成功另造与原问题无关的任务。\n不要输出答案、结论、人格或已发生的行动。每项provider/query表示可选后续查证方案，不执行它。只输出JSON {"parts":[{"question":"未决子问题","evidence_needed":"何种材料能推进它","provider":"可用入口","query":"可用查询"}],"reason":"为什么这样拆分或不规划，不超过240字"}。'
CONTRACT = ('\nprospective_goal_plan 是针对当前未决问题的暂定查证拆分；不是事实、已学知识或必须执行的任务。'
            '可选择其中一小部分查证，也可采用其他方法或休息；查清一部分不等于解答整体。')


def inputs(ledger, capabilities):
    if not capabilities.get('can_search'):return None
    state=ledger.view('public','')
    held={i['goal_id'] for i in state.get('workspace',{}).get('intentions',[]) if i.get('stance')=='hold' and not i.get('condition_observed')}
    active=[g for g in state['goals'] if g['status']=='active' and g['id'] not in held and g['scope']=='public' and not g['actor']]
    if len(active)!=1:return None  # Competing goals remain with general control.
    goal=active[0];source=current_source(ledger,goal['origin'])
    if not source:return None
    for belief in state['experimental_world_model']:
        if (belief['plan']['goal_id']!=goal['id']
                or belief.get('observed_relevance_rates')!={'control':0.0,'treatment':0.0}
                or belief.get('assessment_disagreements')):continue
        for eid in belief['supporting_trials']:
            row=ledger.db.execute("SELECT * FROM episodes WHERE owner=? AND id=? AND scope='public' AND actor='' AND revoked=0 AND status='observed'",
                                  (ledger.owner,eid)).fetchone()
            if not row:continue
            result=json.loads(row['result_json']);a=result.get('experiment_assessment') or {}
            if (not receipt_matches(result) or result.get('experiment_revoked') or a.get('status')!='assessed'
                    or a.get('rates')!={'control':0.0,'treatment':0.0}):continue
            if result.get('scoring_payload',{}).get('question')!=goal['question']:continue
            try:
                if judge({'scores':a['scores']},result['scoring_payload'],result['members'])!=a:continue
            except (ValueError,TypeError,KeyError):continue
            failed={'id':eid,'receipt':result['receipt'],'assessment':a,
                    'arms':{name:{k:arm.get(k) for k in ('query','provider','status','discoveries')}
                            for name,arm in result['arms'].items()}}
            from .self_model import retrieve_related_knowledge
            prior=retrieve_related_knowledge(state,[goal['question'],source.get('text','')[:400]],exclude_goal_id=goal['id'])
            return {'goal':goal,'actual_observation':[source],
                    'known_answers':state['progress_memory'].get(goal['id'],[]),
                    'question_state':state['question_states'].get(goal['id']),
                    **({'related_prior_knowledge':prior} if prior else {}),
                    'actual_recent_outcomes':[failed], 'capabilities':capabilities,
                    'binding':{'goal_id':goal['id'],'goal_version':goal_version(goal),
                               'origin':source['id'],'origin_receipt':source['receipt'],
                               'trial_id':eid,'trial_receipt':result['receipt'],'belief_version':belief['version'],
                               'providers':list(capabilities.get('providers',[]))}}
    return None


def attempted(ledger,bundle):
    for row in ledger.db.execute("SELECT decision_json FROM episodes WHERE owner=? AND scope='public' AND actor='' AND revoked=0",
                                 (ledger.owner,)):
        d=json.loads(row[0]);b=d.get('planning_binding') or {}
        if (d.get('action')=='plan_inquiry' and b.get('goal_id')==bundle['binding']['goal_id']
                and b.get('goal_version')==bundle['binding']['goal_version']):return True
    return False


def validate(raw,capabilities):
    data=json.loads(raw) if isinstance(raw,str) else raw
    if not isinstance(data,dict) or not isinstance(data.get('parts'),list) or len(data['parts'])>3:
        raise ValueError('invalid_inquiry_plan')
    # This explanation is optional metadata, never the evidence or a question.
    # Keep an empty explanation empty; do not fabricate one or discard valid
    # subquestions because the model's final duplicate reason field was blank.
    reason=data.get('reason','')
    if not isinstance(reason,str) or len(reason.strip())>240:
        raise ValueError('invalid_planning_reason')
    parts=[]
    for item in data['parts']:
        if not isinstance(item,dict):raise ValueError('invalid_planning_part')
        for key,minimum in [('question',5),('evidence_needed',1)]:
            if not isinstance(item.get(key),str) or not minimum<=len(item[key].strip())<=240:
                raise ValueError('invalid_planning_part')
        provider=item.get('provider');query=item.get('query')
        if (provider not in capabilities.get('providers',[]) or not isinstance(query,str)
                or not 2<=len(query.strip())<=120 or any(ord(c)<32 for c in query)
                or (provider=='bilibili' and len(query.strip())>40)):
            raise ValueError('invalid_planning_query')
        parts.append({k:item[k].strip() for k in ('question','evidence_needed','provider','query')})
    if len({p['question'] for p in parts})!=len(parts):raise ValueError('duplicate_planning_part')
    return {'parts':parts,'reason':reason.strip()}


def current(ledger,bundle):
    if bundle is None:return None
    for row in ledger.db.execute("SELECT * FROM episodes WHERE owner=? AND scope='public' AND actor='' AND revoked=0 AND status='planned' ORDER BY started DESC,rowid DESC",
                                 (ledger.owner,)):
        d=json.loads(row['decision_json']);r=json.loads(row['result_json'] or '{}')
        if (d.get('action')!='plan_inquiry' or d.get('planning_binding')!=bundle['binding']
                or r.get('planning_binding')!=bundle['binding'] or not receipt_matches(r)):continue
        try:plan=validate(r,bundle['capabilities'])
        except (ValueError,TypeError,KeyError):continue
        return {**plan,'goal_id':bundle['goal']['id'],'episode_id':row['id'],
                'basis':'fallible_prospective_questions_not_answers_or_selected_actions'}
    return None


async def process(ledger,model,bundle,*,daily_cap,allowed,capabilities,opportunity_source):
    latest=inputs(ledger,capabilities())
    if not allowed() or latest is None or latest['binding']!=bundle['binding'] or attempted(ledger,bundle):
        return {'status':'cancelled','reason':'planning_inputs_changed'}
    messages=[{'role':'system','content':SYSTEM},{'role':'user','content':encoded(bundle)}]
    ledger.reserve_call(daily_cap)
    eid=ledger.begin({'planning_inputs':bundle,'prompt_sha256':digest(messages),
                      'opportunity':{'source':opportunity_source,'external_pending':False}},
                     {'action':'plan_inquiry','planning_binding':bundle['binding'],'goal':None,'_scope':'public','_actor':''})
    try:
        raw=await model.complete(messages);plan=validate(raw,bundle['capabilities'])
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            fresh=inputs(ledger,capabilities())
            if not allowed() or fresh is None or fresh['binding']!=bundle['binding']:
                ledger.finish(eid,'cancelled',{'reason':'planning_inputs_changed'},60)
                return {'status':'cancelled','episode_id':eid}
            result={**plan,'planning_binding':bundle['binding'],'external_read_performed':False,
                    'basis':'fallible_prospective_questions_not_answers_or_selected_actions'}
            result['receipt']=digest(result);ledger.finish(eid,'planned',result,60)
        return {'status':'planned','episode_id':eid,'part_count':len(plan['parts']),'learning_assessed':False}
    except asyncio.CancelledError:
        ledger.finish(eid,'unknown',{'reason':'planning_interrupted'},60);raise
    except Exception as exc:
        ledger.finish(eid,'planning_error',{'error_type':type(exc).__name__,
                      'validation_code':str(exc)[:160] if isinstance(exc,(ValueError,TypeError,KeyError)) else '',
                      'raw_output':str(locals().get('raw',''))[:4000],'learning_assessed':False},60)
        return {'status':'planning_error','episode_id':eid,'error_type':type(exc).__name__}
