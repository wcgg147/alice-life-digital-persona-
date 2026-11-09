"""Persistent, source-bound intentions and executive attention, without new scores.

An orientation is a fallible choice, not knowledge or a personality fact. Replay
surviving episodes to retain, override, or withdraw it across process restarts.
"""
from .inquiry import receipt_matches
from .state import digest

METHODS={'read','search','experiment','consolidate','open'}
WAITS={'new_evidence','new_source','after_rest','on_choice'}
CONTRACT='''
workspace 保存你之前选择的关注理由、探索方法与暂缓条件；不是固定人格或强制任务。
workspace.tensions 提示进展停滞、反馈缺失、尚缺答案、等待条件变化，或已有阶段性阅读证据滑出最近episodes窗口尚未整合（unintegrated_progress_outside_window / unintegrated_progress_ready）、已保存阅读尚缺评估（unassessed_saved_reads）。可继续关心难题，换方法、先做内部整合（integrate）或补评（evaluate）、转向其他问题、暂缓，或休息。机会估计不等于问题的意义。
可选择 action="orient" 进行内部目标调节，不读取或发送外部内容。此时 goal=null、resource_id为空、inquiry=null，predict_success=false，orientation结构为：
{"focus_goal_id":"本次优先考虑的已有问题id，或null", "intentions":[{"goal_id":"已有问题id","stance":"pursue或hold","why":"根据这些经历，为什么还想追问或先放下，1~240字","method":"read/search/experiment/consolidate/open","evidence_ids":["workspace.sources中属于这个问题的经历id"],"revisit_when":"new_evidence/new_source/after_rest/on_choice"}]}。
每次最多调整3个问题。focus_goal_id必须是本次pursue的问题；都先放下时可以null。至少引用一条同问题真实来源；不能凭空植入兴趣、改写问题或宣告已经回答。search/experiment方法须有当前权限；内部调节不会扩大工具权限。
method=search时，可在该intention中附search_plan={"provider":"当前exploration.providers中的入口","query":"自行选择的查询"}，保存具体打算；可省略，查询不超过120字，bilibili不超过40字。它是可改变的安排，不是已执行行动或新权限。后续可执行它、说明改变方法，或休息；不能把未执行的打算说成已经实践。
进行 orient 时，应结合 progress_memory、question_states 与 workspace.tensions 判断：若 motivation 分数较高的问题在 progress_memory 中已有多次有效回答且 missing=null（核心问题已由既有证据回答），而另一问题在当前阅读来源上连续无进展（change_method_or_hold，total_new_progress=0）但 exploration.can_search=true 允许换用 search 寻找新来源，可将已基本回答的问题设为 stance="hold"（method="consolidate" 或 "open"，revisit_when="on_choice"），将 focus_goal_id 设为仍未解决的问题（stance="pursue"，method="search"，revisit_when="new_source"），避免只顺着高机会分数重复已解答问题而搁置未决难题。
当 workspace.intentions 已保存有效意图（如将未决问题设为 focus_goal_id 且 stance="pursue"、method="search"，另一问题设为 stance="hold"），且 workspace.next_steps 显示该焦点问题 status="awaiting_search"（suggested_action="search"）时，后续决策可按已保存意图对该 focus_goal_id 执行 search，而不必退回已被 hold 的问题。
new_evidence表示出现新的实际阅读来源，new_source表示发现新的候选来源，after_rest表示实际选择过休息，on_choice表示自己以后再考虑。条件出现只提示重新考虑，不强制恢复。普通read/search仍可覆盖原关注；原来关心的问题和理由会保留。若当前活跃问题已在当前来源读过且缺口无法在同来源解答、且暂不打算用其他入口搜索，若想放下它以便空闲观察新内容，应选择 action="orient" 将其设为 stance="hold"（revisit_when="new_evidence"，focus_goal_id=null）；仅选择 action="rest" 会让该目标保持活跃并继续占据注意力，阻止进入空闲观察。当某个问题在 workspace.intentions 中已设为 stance="hold" 且 condition_observed=false（所等待的 new_evidence/new_source 等条件尚未出现）时，若无新的外部线索，应维持暂放（选择 rest 或 inspect 观察新候选，或转向其他未暂放问题），不要在等待条件未满足时又立即对同一被 hold 的问题重复盲搜；若此前覆盖暂放（chosen_intention_overridden=true）的搜索仍为 no_relevant_candidates/no_candidates，可再次执行 orient 将其恢复为 stance="hold"（focus_goal_id=null）或直接 rest。
workspace.method_trials 把选择的方法与真实后续结果关联起来。search方法包含先搜索再读取同问题的新候选；只有正文带来的有来源问题进展才支持暂定沿用，搜到标题不算回答。workspace.next_steps提示计划还缺哪一步，或无进展时可换方法、暂缓、休息；都是建议，不强制执行。已知反例、缺反馈和错误不能说成方法有效。任何沿用只限当前问题与实际观察的来源链，不泛化成某种工具永远更好。
workspace.available_alternatives 根据当前权限列出可执行的替代动作。search正是用来寻找尚不知道URL的来源，不需要先有候选；可用入口以exploration.providers为准，包括环境明确提供的本地资料库。依然可选择不搜索、继续关心、暂缓或休息。
候选 resource 若带有 prior_method_observation（尤其 feedback_status="known"），说明该来源正文在当前问题下已经读取并评估过，其中 answer 是已从该来源取得的认识，missing 是该来源未能解答的缺口；不要对同一已读且已知缺口的来源重复执行 read，若 workspace.next_steps.suggested_action="search"（且 provisional_reuse_supported=true），可针对 remaining_gap 换关键词继续 search 寻找新来源，或读取尚未读过的其他候选。若 prior_method_observation.feedback_status="unknown"（如此前评估未完成或被撤回），则该来源的结论尚未确立。
method_trials.status/current_feedback 表示最近一次执行及其反馈；new_progress/supporting_episodes 是累计历史，不能用旧成功盖过新反例、失败或尚未评估的阅读。remaining_gap 保留最近有来源的已知缺口；若 remaining_gap_is_latest_observation=false，较新的材料还没有确认这个旧缺口是否已变化，需要保留不确定性。
'''


def goal_version(goal):
    return digest({k:goal.get(k) for k in ('id','question','need','origin')})


def sources_for(goals,history):
    goals={g['id']:g for g in goals};sources={}
    for ep in reversed(history):
        d,r=ep.get('decision') or {},ep.get('result') or {}
        gid=(d.get('goal') or {}).get('id')
        if (gid not in goals or ep.get('revoked') or ep.get('status')!='observed'
                or d.get('action') not in {'read','search','experiment'} or not receipt_matches(r)):
            continue
        sources[ep['id']]={'id':ep['id'],'goal_id':gid,'action':d['action'],
            'source_ref':r.get('source_ref'),'receipt':r['receipt'],
            'judgment_version':digest(r.get('inquiry_assessment')),
            'goal_version':goal_version(goals[gid]),
            'observed_text':bool(r.get('met_need')) and bool(r.get('text')),
            'found_sources':bool(r.get('catalog_found'))}
    return sources


def validate_orientation(raw,context):
    if not isinstance(raw,dict) or not isinstance(raw.get('intentions'),list) or not 1<=len(raw['intentions'])<=3:
        raise ValueError('invalid_orientation')
    goals={g['id']:g for g in context['goals'] if g['status'] in {'active','paused'}}
    sources={s['id']:s for s in context.get('workspace',{}).get('sources',[])}
    entries=[];seen=set()
    for item in raw['intentions']:
        if not isinstance(item,dict):raise ValueError('invalid_intention')
        gid=item.get('goal_id');ids=item.get('evidence_ids');why=item.get('why');method=item.get('method')
        if gid not in goals or gid in seen:raise ValueError('unknown_or_duplicate_intention_goal')
        if (item.get('stance') not in {'pursue','hold'} or method not in METHODS or item.get('revisit_when') not in WAITS
                or not isinstance(why,str) or not 1<=len(why.strip())<=240 or not isinstance(ids,list) or not 1<=len(ids)<=6
                or any(not isinstance(x,str) or x not in sources or sources[x]['goal_id']!=gid for x in ids)):
            raise ValueError('unsourced_or_invalid_intention')
        if method in {'search','experiment'} and (context.get('scope','public')!='public'
                or goals[gid].get('scope','public')!='public' or not context.get('exploration',{}).get('can_search')):
            raise ValueError('intention_method_not_available')
        seen.add(gid)
        search_plan=item.get('search_plan')
        if search_plan is not None:
            if (method!='search' or not isinstance(search_plan,dict)
                    or set(search_plan)!={'provider','query'}
                    or search_plan['provider'] not in context.get('exploration',{}).get('providers',[])
                    or not isinstance(search_plan['query'],str) or not search_plan['query'].strip()
                    or len(search_plan['query'])>(40 if search_plan['provider']=='bilibili' else 120)):
                raise ValueError('invalid_intention_search_plan')
            search_plan={**search_plan,'query':search_plan['query'].strip()}
        entries.append({'goal_id':gid,'stance':item['stance'],'why':why.strip(),'method':method,
                        'revisit_when':item['revisit_when'],'evidence_ids':list(dict.fromkeys(ids)),
                        **({'search_plan':search_plan} if search_plan is not None else {}),
                        'source_bindings':[sources[x] for x in dict.fromkeys(ids)],'goal_version':goal_version(goals[gid])})
    focus=raw.get('focus_goal_id')
    if focus is not None and not any(e['goal_id']==focus and e['stance']=='pursue' for e in entries):
        raise ValueError('orientation_focus_must_be_pursued')
    return {'focus_goal_id':focus,'intentions':entries}


def derive(goals,history,motivation,progress_memory,*,question_states=None,question_integrations=None,pending_learning=None,experimental_world_model=None,hypothesis_sources=None):
    goals={g['id']:g for g in goals if g['status'] in {'active','paused'}}
    sources=sources_for(list(goals.values()),history)
    intentions={};focus=None;focus_episode=None;rest_ids=[];method_plans=[]
    ordered=list(reversed(history))
    for index,ep in enumerate(ordered):
        if ep.get('revoked'):continue
        d,r=ep['decision'],ep['result']
        if d.get('action')=='orient' and ep['status']=='oriented' and receipt_matches(r):
            plan=r.get('orientation') or {};valid=[]
            for item in plan.get('intentions',[]):
                gid=item.get('goal_id')
                if gid not in goals or item.get('goal_version')!=goal_version(goals[gid]):continue
                bindings=item.get('source_bindings') or []
                if not bindings or any(sources.get(s['id'])!=s for s in bindings):continue
                entry={k:item[k] for k in ['goal_id','stance','why','method','revisit_when','evidence_ids']}
                if item.get('search_plan') is not None:entry['search_plan']=item['search_plan']
                entry.update(orientation_episode=ep['id'],at_index=index,actual_override=False)
                intentions[gid]=entry;valid.append(gid)
                method_plans.append({**entry,'anchor_episode':ep['id'],'plan_kind':'declared_intention'})
            if valid and (plan.get('focus_goal_id') is None or plan['focus_goal_id'] in valid):
                focus=plan.get('focus_goal_id');focus_episode=ep['id']
        elif ep['status']=='rest':rest_ids.append((index,ep['id']))
        elif ep['status']=='tool_error' and d.get('action') in {'read','search'}:
            # A recorded failed invocation is evidence of execution trouble,
            # never evidence about the unread outside world.
            gid=(d.get('goal') or {}).get('id')
            if gid in goals:
                method_plans.append({'goal_id':gid,'method':d['action'],'stance':'pursue','orientation_episode':None,
                    'anchor_episode':ep['id'],'plan_kind':'executed_approach','at_index':index,'source_family':None})
                if gid in intentions:
                    intentions[gid]['actual_override']=True;intentions[gid]['stance']='pursue'
                    focus=gid;focus_episode=ep['id']
        elif ep['id'] in sources:
            gid=sources[ep['id']]['goal_id']
            if intentions:focus=gid;focus_episode=ep['id']
            previous=next((p for p in reversed(method_plans) if p['goal_id']==gid),None)
            source_chain=(previous and previous['method']=='search' and d['action']=='read' and any(
                other['decision'].get('action')=='search' and (other['decision'].get('goal') or {}).get('id')==gid
                and other['status']=='observed' and not other.get('revoked') and receipt_matches(other['result'])
                and r.get('source_ref') in {x.get('url') for x in other['result'].get('discoveries',[])}
                for other in ordered[previous['at_index']:index]))
            if d['action'] in {'read','search'} and not source_chain and (not previous or previous['method']!=d['action']
                    or (previous['plan_kind']=='executed_approach' and (d['action']=='search' or previous.get('source_family')!=r.get('family')))):
                method_plans.append({'goal_id':gid,'method':d['action'],'stance':'pursue','orientation_episode':None,
                    'anchor_episode':ep['id'],'plan_kind':'executed_approach','at_index':index,'source_family':r.get('family')})
            if gid in intentions:
                search_read=(intentions[gid]['method']=='search' and d['action']=='read' and any(
                    other['decision'].get('action')=='search' and (other['decision'].get('goal') or {}).get('id')==gid
                    and other['status']=='observed' and not other.get('revoked') and receipt_matches(other['result'])
                    and r.get('source_ref') in {x.get('url') for x in other['result'].get('discoveries',[])}
                    for other in ordered[intentions[gid]['at_index']+1:index]))
                plan=intentions[gid].get('search_plan')
                changed_search=(d['action']=='search' and plan is not None
                    and (d.get('search_provider')!=plan['provider'] or d.get('search_query')!=plan['query']))
                if search_read and plan is not None and intentions[gid]['actual_override']:
                    changed_search=True
                intentions[gid]['actual_override']=intentions[gid]['stance']=='hold' or changed_search or (intentions[gid]['method'] not in {d['action'],'open'} and not search_read)
                intentions[gid]['stance']='pursue'
    positions={ep['id']:i for i,ep in enumerate(ordered)}
    results={ep['id']:ep['result'] for ep in ordered}
    tensions=[]
    for gid,item in intentions.items():
        cutoff=item.pop('at_index');later=[s for eid,s in sources.items() if s['goal_id']==gid and positions[eid]>cutoff]
        earlier=[s for eid,s in sources.items() if s['goal_id']==gid and positions[eid]<cutoff]
        earlier_reads={(s['source_ref'],digest(results[s['id']].get('text',''))) for s in earlier if s['action']=='read'}
        earlier_urls={x.get('url') for s in earlier if s['action']=='search' for x in results[s['id']].get('discoveries',[])}
        trigger=item['revisit_when'];seen_ids=[]
        if trigger=='after_rest':seen_ids=[eid for i,eid in rest_ids if i>cutoff]
        elif trigger=='new_evidence':seen_ids=[s['id'] for s in later if s['action']=='read' and s['observed_text']
            and (s['source_ref'],digest(results[s['id']].get('text',''))) not in earlier_reads]
        elif trigger=='new_source':seen_ids=[s['id'] for s in later if s['action']=='search' and s['found_sources']
            and any(x.get('url') and x['url'] not in earlier_urls for x in results[s['id']].get('discoveries',[]))]
        item['condition_observed']=bool(seen_ids)
        item['condition_evidence']=seen_ids[-3:]
        if item['stance']=='hold' and seen_ids:tensions.append({'goal_id':gid,'kind':'reconsider_waiting_goal','evidence_ids':seen_ids[-3:]})
    for p in motivation.get('goals',[]):
        if p['goal_id'] not in goals:continue
        if p['stalled'] or p['feedback_missing']:
            tensions.append({'goal_id':p['goal_id'],'kind':'change_method_or_hold' if p['stalled'] else 'feedback_unavailable',
                             'evidence_ids':p['supporting_episodes'][-3:]})
    for gid in goals:
        qstate=(question_states or {}).get(gid)
        if qstate:
            unresolved=[p['question'] for p in qstate.get('parts',[]) if p.get('status')!='tentatively_answered']
            if unresolved:
                tensions.append({'goal_id':gid,'kind':'unresolved_gap','missing':'；'.join(unresolved),
                                 'evidence_ids':[qstate['last_integration']]})
            else:
                tensions.append({'goal_id':gid,'kind':'question_answered_ready_to_hold_or_consolidate',
                                 'supporting_count':len(qstate.get('parts',[])),
                                 'evidence_ids':[qstate['last_integration']]})
            continue
        answers=progress_memory.get(gid) or []
        if answers and answers[0].get('missing'):
            tensions.append({'goal_id':gid,'kind':'unresolved_gap','missing':answers[0]['missing'],
                             'evidence_ids':[answers[0]['episode_id']]})
    for opt in question_integrations or []:
        gid=opt.get('goal_id')
        if gid not in goals:continue
        outside=opt.get('outside_recent_episodes_count',0);useful=opt.get('assessed_useful_count',0)
        unint=opt.get('unintegrated_source_count',len(opt.get('source_ids',[])))
        answers=progress_memory.get(gid) or []
        has_gap=bool((question_states or {}).get(gid) or (answers and answers[0].get('missing')))
        if outside>0 or (useful>=2 and has_gap):
            tensions.append({'goal_id':gid,
                'kind':'unintegrated_progress_outside_window' if outside>0 else 'unintegrated_progress_ready',
                'source_count':unint,'assessed_useful_count':useful,
                'outside_recent_episodes_count':outside,'existing_parts':opt.get('existing_parts',0),
                'evidence_ids':opt.get('source_ids',[])[-3:]})
    pending_by_goal={}
    for item in pending_learning or []:
        if item.get('goal_id') in goals and item.get('previous_attempts',0)==0:
            pending_by_goal.setdefault(item['goal_id'],[]).append(item['id'])
    for gid,eids in pending_by_goal.items():
        tensions.append({'goal_id':gid,'kind':'unassessed_saved_reads','episode_ids':eids[:3]})
    untested_by_goal={};preferred_by_goal={};hypothesized_anchors=set()
    for b in experimental_world_model or []:
        plan=b.get('plan') or {};gid=plan.get('goal_id')
        if plan.get('source_episode_id'):hypothesized_anchors.add(plan['source_episode_id'])
        if gid not in goals:continue
        if b.get('status')=='untested':untested_by_goal.setdefault(gid,[]).append(b)
        elif b.get('preferred'):preferred_by_goal.setdefault(gid,[]).append(b)
    unhypothesized_by_goal={}
    for hs in hypothesis_sources or []:
        gid=hs.get('goal_id')
        if gid in goals and hs.get('id') not in hypothesized_anchors:
            unhypothesized_by_goal.setdefault(gid,[]).append(hs)
    for gid,items in untested_by_goal.items():
        tensions.append({'goal_id':gid,'kind':'untested_search_hypothesis','hypothesis_ids':[b['id'] for b in items[:3]]})
    if len(goals)>1:tensions.append({'kind':'competing_questions','goal_ids':list(goals)})
    visible=[]
    for gid in goals:
        goal_sources=[s for s in sources.values() if s['goal_id']==gid]
        read_ids={e['episode_id'] for e in (progress_memory.get(gid) or []) if e.get('episode_id')}
        read_ids.update(s['id'] for s in goal_sources if s['action']=='read' and s['observed_text'])
        kept_reads=[s for s in goal_sources if s['id'] in read_ids][-6:]
        kept_ids={s['id'] for s in kept_reads}
        recent_other=[s for s in goal_sources if s['id'] not in kept_ids][-max(0,12-len(kept_reads)):]
        visible.extend(sorted([*kept_reads,*recent_other],key=lambda s:positions[s['id']]))
    workspace={'focus_goal_id':focus,'focus_episode':focus_episode,'intentions':list(intentions.values()),
            'tensions':tensions[:24],'sources':visible,'can_orient':bool(visible),
            'basis':'source_bound_self_choices_not_personality_facts',
            'limitation':'waiting signals suggest reconsideration; no automatic goal completion or emotion claims'}
    from .method_learning import derive as methods,next_steps
    workspace['method_trials']=methods(method_plans,history)
    workspace['next_steps']=next_steps(workspace)
    integration_by_goal={o['goal_id']:o for o in (question_integrations or [])}
    integration_ready={t['goal_id'] for t in tensions
                       if t.get('kind') in {'unintegrated_progress_ready','unintegrated_progress_outside_window'}}
    for step in workspace['next_steps']:
        if step['status']=='feedback_missing' and pending_by_goal.get(step['goal_id']) and step.get('suggested_action') is None:
            step.update(suggested_action='evaluate',suggested_target_episode_id=pending_by_goal[step['goal_id']][0])
        if untested_by_goal.get(step['goal_id']):
            step['optional_method_trials']={'experiment_hypothesis_id':untested_by_goal[step['goal_id']][0]['id']}
        elif (unhypothesized_by_goal.get(step['goal_id']) and step['status'] in {'no_candidates','no_relevant_candidates','no_new_progress'}
                and step.get('suggested_action') is None):
            step['optional_method_trials']={'hypothesize_source_id':unhypothesized_by_goal[step['goal_id']][0]['id']}
        if preferred_by_goal.get(step['goal_id']):
            pref_b=preferred_by_goal[step['goal_id']][0]
            step['preferred_search_belief']={'belief_id':pref_b['id'],'provider':pref_b['preferred']['provider'],
                'query':pref_b['preferred']['query'],'observed_relevance_rates':pref_b['observed_relevance_rates']}
        opt=integration_by_goal.get(step['goal_id'])
        if opt:
            unint=opt.get('unintegrated_source_count',len(opt.get('source_ids',[])))
            meaningful=bool(unint and opt.get('assessed_useful_count',0)>0)
            step.update(has_unintegrated_sources=meaningful,
                        unintegrated_source_count=unint if meaningful else 0,
                        unintegrated_outside_window_count=opt.get('outside_recent_episodes_count',0) if meaningful else 0)
            if meaningful and step['goal_id'] in integration_ready and (step.get('suggested_action') is None or step.get('status')=='provisional_progress'):
                step.update(suggested_action='integrate',suggested_integration_goal_id=step['goal_id'])
        state=(question_states or {}).get(step['goal_id'])
        if not state:continue
        unresolved=[p for p in state['parts'] if p['status']!='tentatively_answered']
        has_unint=bool(opt and opt.get('assessed_useful_count',0)>0)
        step.update(remaining_gap='；'.join(p['question'] for p in unresolved) or None,
                    remaining_part_ids=[p['id'] for p in unresolved],remaining_gap_basis='integrated_subquestions',
                    remaining_gap_evidence=state['last_integration'],remaining_gap_is_latest_observation=False,
                    has_unintegrated_sources=has_unint,
                    all_integrated_parts_answered=bool(state.get('parts')) and not unresolved and not has_unint)
        if step['all_integrated_parts_answered']:
            step.update(provisional_reuse_supported=False,reconsider_supported=True,suggested_action='rest')
    return workspace


def focus_goals(goals,workspace):
    """Respect a surviving intention while keeping alternatives available."""
    if not workspace.get('intentions'):return goals,None
    held={i['goal_id'] for i in workspace['intentions'] if i['stance']=='hold'}
    focus=workspace.get('focus_goal_id')
    selected=next((g for g in goals if g['id']==focus and g['status'] in {'active','paused'} and g['id'] not in held),None)
    if selected is None:selected=next((g for g in goals if g['status']=='active' and g['id'] not in held),None)
    return sorted(goals,key=lambda g:(g['id']!=(selected or {}).get('id'),g['id'] in held)),selected
