"""Link chosen approaches to executed actions and surviving question feedback.

This is scoped experience, not causal superiority or a global tool preference.
It has no new reward score and never chooses an external action by itself.
"""
import re

from .inquiry import grounded_assessments,receipt_matches
from .state import digest


def _signature(value):
    return digest(re.sub(r'[\W_]+','',str(value).casefold()))


def derive(plans,history):
    ordered=list(reversed(history));positions={e['id']:i for i,e in enumerate(ordered)}
    admitted=grounded_assessments(history)
    considered_searches={}
    for ep in ordered:
        if ep.get('revoked'):continue
        d=ep.get('decision') or {}
        if d.get('selection_process') in {'search_source_selection','retained_source_selection'} and ep.get('status')=='rest':
            if d.get('parent_search_id'):considered_searches[d['parent_search_id']]=ep['id']
            for sid in d.get('considered_search_ids') or []:considered_searches[sid]=ep['id']
    progress={};seen_answers={};seen_reads={}
    for ep in ordered:
        gid=(ep['decision'].get('goal') or {}).get('id');r=ep['result']
        if ep['id'] not in admitted:continue
        a=admitted[ep['id']][1]
        if a['verdict']=='uncertain':continue
        source=(r['source_ref'],digest(r.get('text','')));answer=_signature(a['answer'])
        seen_reads.setdefault(gid,set())
        novel=(a['verdict']=='useful' and source not in seen_reads[gid]
               and answer not in seen_answers.setdefault(gid,set()))
        progress[ep['id']]={'novel':novel,'type':a.get('progress_type','legacy_unspecified'),
                             'judgment_version':digest(r['inquiry_assessment'])}
        seen_reads[gid].add(source)
        if a['verdict']=='useful':seen_answers.setdefault(gid,set()).add(answer)
    trials=[]
    for n,plan in enumerate(plans):
        gid=plan['goal_id'];start=positions[plan['anchor_episode']]
        end=next((positions[p['anchor_episode']] for p in plans[n+1:] if p['goal_id']==gid),len(ordered))
        begin=start+1 if plan['plan_kind']=='declared_intention' else start
        actions=[ep for ep in ordered[begin:end] if not ep.get('revoked')
                 and (ep['decision'].get('goal') or {}).get('id')==gid
                 and ep['decision'].get('action') in {'read','search'}]
        searches=[ep for ep in actions if ep['decision']['action']=='search' and ep['status']=='observed'
                  and receipt_matches(ep['result']) and ep['result'].get('status')=='ok']
        catalogs={ep['id']:{x['url'] for x in ep['result'].get('discoveries',[]) if x.get('url')} for ep in searches}
        reads=[]
        for ep in actions:
            if ep['decision']['action']!='read':continue
            if plan['method']=='read':reads.append(ep)
            elif plan['method']=='search' and any(positions[eid]<positions[ep['id']] and ep['result'].get('source_ref') in urls
                                                for eid,urls in catalogs.items()):reads.append(ep)
        checked=[ep for ep in reads if ep['id'] in progress]
        gains=[ep for ep in checked if progress[ep['id']]['novel']]
        failed=[ep for ep in actions if ep['status'] in {'tool_error','unknown','cancelled'}]
        events={ep['id']:ep for ep in [*searches,*reads,*failed]}
        last=max(events.values(),key=lambda ep:positions[ep['id']]) if events else None
        if last and last['status'] in {'unknown','cancelled'}:status='execution_interrupted'
        elif last and last['status']=='tool_error':status='execution_failed'
        elif last and last['decision']['action']=='read':
            status=('provisional_progress' if progress[last['id']]['novel'] else 'no_new_progress') if last['id'] in progress else 'feedback_missing'
        elif last and last['decision']['action']=='search':
            if not catalogs.get(last['id']):status='no_candidates'
            elif last['id'] in considered_searches:status='no_relevant_candidates'
            else:status='awaiting_read'
        elif plan['method']=='search':status='awaiting_search'
        elif plan['method']=='read':status='awaiting_read'
        else:status='outside_measured_methods'
        # Preserve exact search choices as scoped experience; titles alone
        # never count as question progress, even when retrieval succeeded.
        outcomes=[]
        for ep in reads:
            known=ep['id'] in progress;a=admitted[ep['id']][1] if known else {}
            outcomes.append({'episode_id':ep['id'],'source_ref':ep['result'].get('source_ref'),
                'resource_id':ep['decision'].get('resource_id'),'feedback_status':'known' if known else 'unknown',
                'new_progress':progress[ep['id']]['novel'] if known else None,
                'progress_type':a.get('progress_type','legacy_unspecified') if known else None,
                'answer':a.get('answer') if known else None,'missing':a.get('missing') if known else None,
                'receipt':ep['result'].get('receipt'),'judgment_version':digest(ep['result'].get('inquiry_assessment')),
                'limitation':'past observation; current source version has not been refetched'})
        gap=None
        for result in outcomes:
            if result['feedback_status']=='known' and (result['missing'] or result['progress_type']=='new_answer'):
                gap={k:result[k] for k in ['episode_id','missing','progress_type','receipt','judgment_version']}
        current={'episode_id':last['id'] if last else None,'action':last['decision']['action'] if last else None,
                 'state':status,'known_question_feedback':bool(last and last['id'] in progress),
                 'new_progress':progress[last['id']]['novel'] if last and last['id'] in progress else None,
                 'basis':'latest_surviving_execution_and_its_feedback; historical gains are separate'}
        trials.append({'orientation_episode':plan['orientation_episode'],'anchor_episode':plan['anchor_episode'],
            'plan_kind':plan['plan_kind'],'goal_id':gid,'method':plan['method'],
            'stance':plan['stance'],'status':status,'attempt_episode_ids':[ep['id'] for ep in actions],
            'catalog_episode_ids':list(catalogs),'candidate_sources':sorted(set().union(*catalogs.values())) if catalogs else [],
            'known_feedback':len(checked),'new_progress':len(gains),
            'counterexamples':[ep['id'] for ep in checked if not progress[ep['id']]['novel']],
            'supporting_episodes':[ep['id'] for ep in gains],
            'judgment_versions':{ep['id']:progress[ep['id']]['judgment_version'] for ep in checked},
            'read_outcomes':outcomes[-6:],
            'current_feedback':current,'last_known_gap':gap,
            'search_choices':[{'provider':ep['result'].get('provider'),'query':ep['result'].get('query')} for ep in searches],
            'basis':'chosen_approach_then_observed_actions_and_fallible_question_feedback',
            'scope':'this_goal_and_observed_source_chain; not randomized causal comparison'})
    return trials[-24:]


def next_steps(workspace):
    """Offer an executable next step or reconsideration, preserving choice."""
    active={i['goal_id']:i for i in workspace.get('intentions',[]) if i['stance']=='pursue'}
    held={i['goal_id'] for i in workspace.get('intentions',[]) if i['stance']=='hold'}
    latest={t['goal_id']:t for t in workspace.get('method_trials',[])}
    last_gaps={}
    for t in workspace.get('method_trials',[]):
        if t.get('last_known_gap'):last_gaps[t['goal_id']]=t['last_known_gap']
    for gid,t in latest.items():
        if gid not in held and gid not in active and t['plan_kind']=='executed_approach':
            active[gid]={'orientation_episode':None,'anchor_episode':t['anchor_episode']}
    steps=[]
    for gid,item in active.items():
        t=latest.get(gid)
        if not t or (t['orientation_episode']!=item['orientation_episode'] and not
                     (item.get('actual_override') and t['plan_kind']=='executed_approach')):continue
        status=t['status']
        if item.get('actual_override') and t['plan_kind']!='executed_approach':status='approach_overridden'
        action='search' if status=='awaiting_search' else 'read' if status=='awaiting_read' else None
        gap_observation=t.get('last_known_gap') or last_gaps.get(gid) or {}
        gap=gap_observation.get('missing')
        if status=='provisional_progress' and t['method']=='search' and gap:action='search'
        steps.append({'goal_id':gid,'orientation_episode':t['orientation_episode'],'anchor_episode':t['anchor_episode'],'status':status,
            'suggested_action':action,'candidate_sources':t['candidate_sources'] if action=='read' and t['method']=='search' else [],
            'remaining_gap':gap,
            'chosen_intention_overridden':bool(item.get('actual_override')),
            'remaining_gap_evidence':gap_observation.get('episode_id'),
            'remaining_gap_is_latest_observation':bool(gap_observation and gap_observation['episode_id']==t['current_feedback']['episode_id']),
            'reconsider_supported':status in {'no_new_progress','no_candidates','no_relevant_candidates','feedback_missing','execution_failed','execution_interrupted'},
            'provisional_reuse_supported':status=='provisional_progress','supporting_episodes':t['supporting_episodes'],
            'counterexamples':t['counterexamples'],'not_mandatory':True})
    return steps


def attach_options(context):
    """Ground reconsideration in the currently enabled action interfaces."""
    workspace=dict(context.get('workspace') or {})
    observations={}
    for trial in workspace.get('method_trials',[]):
        for result in trial.get('read_outcomes',[]):observations[(trial['goal_id'],result['source_ref'])]=result
    for resource in context.get('resources',[]):
        resource.pop('prior_method_observation',None)
        item=observations.get((resource.get('goal_id',context.get('focus_goal_id')),resource.get('source_ref')))
        if item:resource['prior_method_observation']=item
    if context.get('resources'):
        context['resources'].sort(key=lambda r:bool((r.get('prior_method_observation') or {}).get('feedback_status')=='known'))
    options=[];goals={g['id']:g for g in context.get('goals',[])}
    integrations={o['goal_id']:o for o in context.get('question_integrations',[])}
    dev_by_goal={o['goal']['id']:o for o in context.get('development_opportunities',[]) if o.get('goal')}
    untested_by_goal={};hypothesized_anchors=set()
    for b in context.get('experimental_world_model',[]):
        plan=b.get('plan') or {};gid=plan.get('goal_id')
        if plan.get('source_episode_id'):hypothesized_anchors.add(plan['source_episode_id'])
        if b.get('status')=='untested' and gid:untested_by_goal.setdefault(gid,[]).append(b)
    unhypothesized_by_goal={}
    for hs in context.get('hypothesis_sources',[]):
        if hs.get('goal_id') and hs.get('id') not in hypothesized_anchors:
            unhypothesized_by_goal.setdefault(hs['goal_id'],[]).append(hs)
    pending_by_goal={}
    for item in context.get('pending_learning',[]):
        if (item.get('scope','public')=='public'
                or (item.get('scope')==context.get('scope') and item.get('actor')==context.get('actor'))):
            pending_by_goal.setdefault(item.get('goal_id'),[]).append(item['id'])
    for step in workspace.get('next_steps',[]):
        gid=step['goal_id'];goal=goals.get(gid)
        if goal is None:continue
        opt=integrations.get(gid)
        meaningful_opt=bool(opt and opt.get('assessed_useful_count',0)>0)
        overflow=bool(meaningful_opt and opt.get('outside_recent_episodes_count',0)>0)
        integration_ready=bool(meaningful_opt and step.get('suggested_action')=='integrate')
        continue_search=bool((step.get('suggested_action')=='search' or step.get('status')=='provisional_progress') and step.get('remaining_gap'))
        if (not step['reconsider_supported'] and not overflow and not integration_ready
                and not continue_search and not step.get('optional_method_trials')):continue
        choices=[]
        if gid in dev_by_goal:
            choices.append({'action':'rest','consolidation_id':dev_by_goal[gid]['id'],
                            'purpose':('review milestone and recent evidence to complete the answered goal or evolve the next question' if step.get('all_integrated_parts_answered') else 'review accumulated topic sources to update the consolidation summary or reconsider goal status')})
        if step['status']=='feedback_missing' and pending_by_goal.get(gid):
            choices.append({'action':'evaluate','target_episode_ids':pending_by_goal[gid][:3],
                            'purpose':'evaluate a saved read that is still missing question feedback without re-reading externally'})
        if overflow or integration_ready:
            choices.append({'action':'integrate','integration_goal_id':gid,
                            'unintegrated_sources':opt.get('unintegrated_source_count',len(opt.get('source_ids',[]))),
                            'outside_recent_episodes_count':opt.get('outside_recent_episodes_count',0),
                            'purpose':'integrate accumulated source-bound progress to refresh subquestion answers; no external reread required'})
        if step['status']!='feedback_missing' and pending_by_goal.get(gid):
            choices.append({'action':'evaluate','target_episode_ids':pending_by_goal[gid][:3],
                            'purpose':'evaluate a saved read that is still missing question feedback without re-reading externally'})
        exploration=context.get('exploration',{})
        if context.get('scope','public')=='public' and goal.get('scope','public')=='public' and exploration.get('can_search'):
            search_choice={'action':'search','providers':exploration.get('providers',[]),
                           'requires_existing_candidate':False,
                           'purpose':'search for a new source targeting remaining_gap' if continue_search else 'discover a different source for this same question'}
            if step.get('preferred_search_belief'):
                search_choice['preferred_belief']=step['preferred_search_belief']
            choices.append(search_choice)
        if context.get('resources'):choices.append({'action':'read','purpose':'choose an available source; a repeated source need not add knowledge'})
        if context.get('scope','public')=='public' and goal.get('scope','public')=='public' and exploration.get('can_search'):
            if untested_by_goal.get(gid):
                choices.append({'action':'experiment','hypothesis_id':untested_by_goal[gid][0]['id'],
                                'purpose':'run a blind paired search trial for the untested search hypothesis'})
            if unhypothesized_by_goal.get(gid) and step['status'] in {'no_candidates','no_relevant_candidates','no_new_progress'}:
                choices.append({'action':'hypothesize','source_episode_id':unhypothesized_by_goal[gid][0]['id'],
                                'factors':['query_variant','provider'],
                                'purpose':'propose a single-factor falsifiable search hypothesis on the observed search episode'})
        if workspace.get('can_orient'):choices.append({'action':'orient','purpose':'reconsider concern, approach, or waiting condition'})
        choices.append({'action':'rest','purpose':'choose not to act'})
        problem=('all_subquestions_answered_ready_to_consolidate' if step.get('all_integrated_parts_answered')
                 else step['status'] if step['reconsider_supported']
                 else 'candidate_already_read_gap_remains' if continue_search
                 else 'unintegrated_sources_outside_window' if overflow
                 else 'optional_method_comparison' if step.get('optional_method_trials')
                 else 'unintegrated_progress_ready')
        options.append({'goal_id':gid,'observed_problem':problem,'choices':choices,'not_mandatory':True})
    workspace['available_alternatives']=options
    context['workspace']=workspace
