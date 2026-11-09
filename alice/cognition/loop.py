"""Runtime opportunity != motivation. Decision and external feedback stay separate."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import time

from .decision import action_system, validate, working_context
from .state import digest, encoded


class CognitiveLoop:
    def __init__(self, ledger, model, environment, *, daily_cap=20, stop_path=None, use_strategies=True):
        self.ledger, self.model, self.environment = ledger, model, environment
        self.daily_cap, self.stop_path, self.use_strategies = daily_cap, stop_path, use_strategies

    async def tick(self, *, opportunity_source='manual', source_search_id=None):
        if opportunity_source not in {'manual','runtime','experiment'}:
            raise ValueError('invalid_opportunity_source')
        if self.stop_path and self.stop_path.exists():
            return {"status": "stopped"}
        # Avoid observation traffic after exhaustion; reserve an actual attempt
        # only once observation succeeds and immediately before the model call.
        self.ledger.check_budget(self.daily_cap)
        from .social import SocialState, permitted
        social = SocialState(self.ledger)
        social_watermark = time.time()
        config = self.environment.current_config() if hasattr(self.environment, 'current_config') else getattr(self.environment, 'cfg', {})
        pending = social.pending(config)
        scope, actor = (pending["scope"], pending["actor"]) if pending else ("public", "")
        if source_search_id and (pending or config.get('cognition_source_followup_enabled') is not True):
            return {'status':'cancelled','reason':'source_followup_disabled_or_social_pending'}
        if not source_search_id and scope=='public' and config.get('cognition_observation_assimilation_enabled') is True:
            from . import observation_assimilation
            unprocessed=observation_assimilation.pending(self.ledger)
            if unprocessed:
                def allowed():
                    if self.stop_path and self.stop_path.exists():return False
                    latest=self.environment.current_config() if hasattr(self.environment,'current_config') else config
                    return latest.get('cognition_observation_assimilation_enabled') is True
                capabilities=self.environment.exploration_capabilities() if hasattr(self.environment,'exploration_capabilities') else {}
                return await observation_assimilation.process(self.ledger,self.model,unprocessed,
                    daily_cap=self.daily_cap,allowed=allowed,opportunity_source=opportunity_source,capabilities=capabilities)
        resources, facts = await self.environment.observe()
        if self.stop_path and self.stop_path.exists():
            return {"status": "stopped"}
        context = working_context(self.ledger.view(scope, actor), resources, facts, self.use_strategies)
        context.update(scope=scope, actor=actor, social_watermark=social_watermark)
        context['opportunity']={'source':opportunity_source,'external_pending':bool(pending)}
        context['require_inquiry'] = getattr(self.model, 'requires_inquiry', False)
        if hasattr(self.environment, 'exploration_capabilities'):
            context['exploration'] = self.environment.exploration_capabilities()
            if scope != 'public':
                context['exploration']['can_search'] = False
        if (not source_search_id and scope=='public' and not pending and hasattr(self.environment,'exploration_capabilities')
                and config.get('cognition_inquiry_planning_enabled') is True):
            from . import inquiry_planning
            capabilities=self.environment.exploration_capabilities
            bundle=inquiry_planning.inputs(self.ledger,capabilities())
            if bundle:
                plan=inquiry_planning.current(self.ledger,bundle)
                if plan is not None:context['prospective_goal_plan']=plan
                elif not inquiry_planning.attempted(self.ledger,bundle):
                    def allowed():
                        if self.stop_path and self.stop_path.exists():return False
                        latest=self.environment.current_config() if hasattr(self.environment,'current_config') else config
                        return latest.get('cognition_inquiry_planning_enabled') is True
                    return await inquiry_planning.process(self.ledger,self.model,bundle,daily_cap=self.daily_cap,
                        allowed=allowed,capabilities=capabilities,opportunity_source=opportunity_source)
        if pending:
            context["pending_social_event"] = {k: pending[k] for k in ["id", "text", "at", "scope", "actor"]}
            context.update(social.context(scope, actor))
        context["observation_limits"] = getattr(self.environment, "catalog_errors", [])
        from .method_learning import attach_options
        attach_options(context)
        source_parent=None
        retained=[]
        if source_search_id:
            from .exploration import source_followup
            source_parent=source_followup(self.ledger,context,source_search_id)
            if source_parent is None:return {'status':'cancelled','reason':'source_followup_not_available'}
            context['opportunity'].update(cause='search_result',parent_search_id=source_search_id)
        else:
            from .exploration import retained_sources
            retained=retained_sources(self.ledger,context,config)
            if retained:
                context['opportunity'].update(cause='retained_search_results')
                context['retained_searches']=[{'id':p['id'],'receipt':p['receipt']} for p in retained]
        from . import idle_attention,idle_goal_choice
        idle_questions=idle_goal_choice.candidates(context,config)
        idle_resources=idle_attention.candidates(context,config) if not idle_questions else []
        messages = (idle_goal_choice.messages(context,idle_questions) if idle_questions else
                    idle_attention.messages(context,idle_resources) if idle_resources else
                    [{"role": "system", "content": action_system(context)}, {"role": "user", "content": encoded(context)}])
        if source_parent:
            from .exploration import source_messages
            messages=source_messages(context,source_parent)
        elif retained:
            from .exploration import retained_source_messages
            messages=retained_source_messages(context,retained)
        elif config.get('cognition_source_followup_enabled') is True:
            from .exploration import SOURCE_FOLLOWUP_CONTRACT
            messages[0]={**messages[0],'content':messages[0]['content']+SOURCE_FOLLOWUP_CONTRACT}
        if context.get('prospective_goal_plan') and not retained:
            from .inquiry_planning import CONTRACT
            messages[0]={**messages[0],'content':messages[0]['content']+CONTRACT}
        self.ledger.reserve_call(self.daily_cap)
        try:
            raw = await self.model.complete(messages)
            appraisal=(idle_goal_choice.decision(raw,context,idle_questions) if idle_questions else
                       idle_attention.decision(raw,idle_resources) if idle_resources else None)
            decision = validate(appraisal if appraisal is not None else raw, context)
            if retained:
                from .exploration import retained_sources
                latest_config=self.environment.current_config() if hasattr(self.environment,'current_config') else config
                fresh={**context,**self.ledger.view(scope,actor)}
                checked=retained_sources(self.ledger,fresh,latest_config)
                if checked!=retained or social.pending(latest_config):
                    raise ValueError('retained_sources_changed')
                if decision['action'] not in {'read','rest'}:
                    raise ValueError('retained_source_read_or_end_only')
                source_parent=(retained[0] if decision['action']=='rest' else
                    next((p for p in retained if any(r['id']==decision['resource_id'] for r in p['candidates'])),None))
                if source_parent is None:raise ValueError('retained_source_not_offered')
                source_search_id=source_parent['id']
            if source_parent:
                latest_config=self.environment.current_config() if hasattr(self.environment,'current_config') else config
                fresh={**context,**self.ledger.view(scope,actor)}
                from .exploration import source_followup
                checked=source_followup(self.ledger,fresh,source_search_id)
                if (latest_config.get('cognition_source_followup_enabled') is not True or checked is None
                        or checked['receipt']!=source_parent['receipt']):raise ValueError('source_followup_changed')
                if decision['action'] not in {'read','rest'}:raise ValueError('source_followup_read_or_end_only')
                if decision['action']=='rest' and (decision.get('goal') is not None or decision.get('consolidation_id') is not None):
                    raise ValueError('source_end_cannot_update_goal_or_review_memory')
                if decision['action']=='read' and ((decision.get('goal') or {}).get('id')!=source_parent['goal']['id']
                        or decision['resource_id'] not in {r['id'] for r in source_parent['candidates']}
                        or not decision.get('inquiry')):raise ValueError('source_followup_same_goal_and_candidates_only')
                decision.update(selection_process='search_source_selection',parent_search_id=source_search_id,
                                parent_search_receipt=source_parent['receipt'])
                if retained:
                    considered=retained if decision['action']=='rest' else [source_parent]
                    decision.update(selection_process='retained_source_selection',
                        considered_search_ids=[p['id'] for p in considered],
                        considered_search_receipts={p['id']:p['receipt'] for p in considered})
            if idle_questions:
                idle_goal_choice.recheck(idle_questions,self.ledger.view(scope,actor))
                decision.update(selection_process='idle_goal_selection',question_appraisal=appraisal['question_appraisal'])
            elif appraisal is not None:
                decision.update(selection_process='idle_selective_attention',attention_appraisal=appraisal['attention_appraisal'])
            origin=(decision.get('goal') or {}).get('origin')
            memory=next((s for s in context.get('memory_goal_origins',[]) if s['id']==origin),None)
            if not memory:memory=next((s for s in context.get('inspection_sources',[]) if s['id']==origin),None)
            if memory:
                from .inquiry import receipt_matches
                row=self.ledger.db.execute("SELECT result_json FROM episodes WHERE owner=? AND id=? AND revoked=0 AND status='observed' AND (scope='public' OR (scope=? AND actor=?))",
                                           (self.ledger.owner,origin,scope,actor)).fetchone()
                current=json.loads(row[0]) if row else {}
                if current.get('receipt')!=memory['receipt'] or not receipt_matches(current):
                    raise ValueError('memory_goal_origin_changed')
            if decision['action']=='wonder':
                from .noticing import validate_wonder
                current=self.ledger.view(scope,actor)
                old=next((q for q in context.get('open_questions',[]) if q['id']==decision['wonder']['id']),None)
                fresh=next((q for q in current.get('open_questions',[]) if q['id']==decision['wonder']['id']),None)
                if old and old!=fresh:raise ValueError('wonder_disposition_changed')
                recheck={k:v for k,v in decision['wonder'].items() if k!='id'}
                if validate_wonder(recheck,current)!=decision['wonder']:
                    raise ValueError('wonder_source_changed')
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            ep = self.ledger.begin(context, {"action": "invalid", "_scope": scope, "_actor": actor,
                                            **({'parent_search_id':source_search_id} if source_search_id else {}),
                                            **({'considered_search_ids':[p['id'] for p in retained],
                                                'considered_search_receipts':{p['id']:p['receipt'] for p in retained}} if retained else {}),
                                            "model_output": str(locals().get("raw", ""))[:4000]})
            self.ledger.finish(ep, "decision_error", {"error": "invalid_model_decision", 'validation_code': str(exc)[:160]},
                               source_parent['next_check_seconds'] if source_parent else 60)
            return {"status": "decision_error", "episode_id": ep}
        decision.update(_scope=scope, _actor=actor)
        ep = self.ledger.begin({**context, "prompt_sha256": digest(messages)}, decision)
        # This event precedes the actual action and the durable row is committed.
        print(encoded({"event": "decision", "episode_id": ep, "decision": decision}), flush=True)
        if pending and hasattr(self.environment, 'current_config') and not permitted(self.environment.current_config(), scope, actor):
            self.ledger.finish(ep, 'cancelled', {'reason':'social_scope_revoked'})
            return {'status':'cancelled','episode_id':ep}
        if self.stop_path and self.stop_path.exists():
            self.ledger.finish(ep, "cancelled", {"reason": "stop_before_action"})
            return {"status": "cancelled", "episode_id": ep}
        if decision['action']=='wonder':
            if pending:social.defer(pending['id'],decision['next_check_seconds'])
            self.ledger.finish(ep,'wondered',{'wonder':decision['wonder'],'basis':'provisional_question_not_learning'},decision['next_check_seconds'])
            return {'status':'wondered','episode_id':ep,'question_id':decision['wonder']['id']}
        if decision['action']=='integrate':
            from .question_state import integrate
            allow_revision=config.get('cognition_interpretation_revision_enabled') is True
            def allowed():
                if self.stop_path and self.stop_path.exists():return False
                cfg=self.environment.current_config() if hasattr(self.environment,'current_config') else config
                if allow_revision and cfg.get('cognition_interpretation_revision_enabled') is not True:return False
                return scope=='public' or permitted(cfg,scope,actor)
            if pending:social.defer(pending['id'],decision['next_check_seconds'])
            return await integrate(self.model,self.ledger,ep,decision['integration_goal_id'],decision['integration_version'],
                scope,actor,self.daily_cap,allowed,decision['next_check_seconds'],allow_withdrawal=allow_revision)
        if decision['action']=='evaluate':
            from .inquiry import assess
            from .pending_learning import candidates,eligible
            current=next((p for p in candidates(self.ledger,scope,actor) if p['id']==decision['target_episode_id']),None)
            row=self.ledger.db.execute('select * from episodes where owner=? and id=?',
                                      (self.ledger.owner,decision['target_episode_id'])).fetchone()
            if not current or current['receipt']!=decision['target_receipt'] or not row or not eligible(row):
                self.ledger.finish(ep,'cancelled',{'reason':'saved_read_changed'},decision['next_check_seconds'])
                return {'status':'cancelled','episode_id':ep}
            source_decision=json.loads(row['decision_json']);source_result=json.loads(row['result_json'])
            original_before=json.loads(row['before_json'] or '{}')
            assessment=await assess(self.model,self.ledger,source_decision,source_result,self.daily_cap,before=original_before)
            if pending and hasattr(self.environment,'current_config') and not permitted(self.environment.current_config(),scope,actor):
                self.ledger.finish(ep,'cancelled',{'reason':'social_scope_revoked'},decision['next_check_seconds'])
                return {'status':'cancelled','episode_id':ep}
            accepted=self.ledger.annotate_inquiry(row['id'],assessment,source_result['receipt'])
            result={'target_episode_id':row['id'],'assessment_status':assessment.get('status') if accepted else 'source_changed',
                    'basis':'saved_observation_and_original_pre_action_context','external_read_performed':False}
            self.ledger.finish(ep,'evaluated' if accepted else 'cancelled',result,decision['next_check_seconds'])
            if pending:social.defer(pending['id'],decision['next_check_seconds'])
            return {'status':'evaluated' if accepted else 'cancelled','episode_id':ep,**result}
        if decision['action']=='orient':
            from .workspace import validate_orientation
            current=self.ledger.view(scope,actor)
            try:
                # Recheck exact source and judgment bindings after the model
                # call, so concurrent withdrawal cannot install stale intent.
                checked=validate_orientation(decision['orientation'],{**context,**current})
                if checked!=decision['orientation']:raise ValueError('orientation_source_changed')
            except (ValueError,TypeError,KeyError):
                self.ledger.finish(ep,'cancelled',{'reason':'orientation_source_changed'},decision['next_check_seconds'])
                return {'status':'cancelled','episode_id':ep}
            if pending:social.defer(pending['id'],decision['next_check_seconds'])
            result={'status':'ok','source_ref':'orientation:'+ep,'text':'','family':'internal_attention',
                    'met_need':False,'orientation':checked,'basis':'fallible_source_bound_intention'}
            result['receipt']=digest(result)
            self.ledger.finish(ep,'oriented',result,decision['next_check_seconds'])
            return {'status':'oriented','episode_id':ep,'focus_goal_id':checked['focus_goal_id']}
        if decision["action"] == "rest":
            if pending:
                social.defer(pending['id'], decision['next_check_seconds'])
            self.ledger.finish(ep, "rest", {"status": "chosen_rest"}, decision["next_check_seconds"])
            if decision.get('consolidation_id'):
                from .development import review_topic
                opportunity = next(o for o in context['development_opportunities'] if o['id'] == decision['consolidation_id'])
                def allowed():
                    if self.stop_path and self.stop_path.exists():
                        return False
                    return (opportunity['scope'] == 'public' or
                            permitted(self.environment.current_config() if hasattr(self.environment, 'current_config') else config,
                                      opportunity['scope'], opportunity['actor']))
                try:
                    review = await review_topic(self.model, self.ledger, opportunity['topic'], scope=opportunity['scope'],
                                               actor=opportunity['actor'], daily_cap=self.daily_cap, guard=allowed,
                                               expected_version=opportunity['input_version'])
                except asyncio.CancelledError:
                    raise  # Rest remains committed, and the review records interruption.
                except Exception as exc:
                    review = {'status': 'budget_wait' if str(exc) == 'daily_model_budget_exhausted' else 'failed',
                              'error_type': type(exc).__name__}
                return {'status': 'rest', 'episode_id': ep, 'development_review': review}
            return {"status": "rest", "episode_id": ep}
        if decision["action"] == "reflect":
            from .reflection import assess
            try:
                assessment = await assess(self.model, self.ledger, social, pending, self.daily_cap)
                if hasattr(self.environment, 'current_config') and not permitted(self.environment.current_config(), scope, actor):
                    raise PermissionError('social_scope_revoked')
                result = social.apply_assessment(pending["id"], assessment, ep, decision["next_check_seconds"])
                return {"status": "reflected", "episode_id": ep, **result}
            except asyncio.CancelledError:
                self.ledger.finish(ep, "unknown", {"error": "reflection_interrupted"})
                raise
            except Exception as exc:
                self.ledger.finish(ep, "reflection_error", {"error_type": type(exc).__name__}, decision["next_check_seconds"])
                return {"status": "reflection_error", "episode_id": ep, "error_type": type(exc).__name__}
        if decision['action'] == 'hypothesize':
            plan = decision['hypothesis']
            current = next((s for s in self.ledger.view()['hypothesis_sources'] if s['id']==plan['source_episode_id']), None)
            if (not current or current['receipt'] != plan['source_receipt'] or not self.environment.exploration_capabilities().get('can_search')):
                self.ledger.finish(ep, 'cancelled', {'reason': 'hypothesis_source_changed'}, decision['next_check_seconds'])
                return {'status': 'cancelled', 'episode_id': ep}
            result = {'status': 'ok', 'text': '', 'family': 'untested_hypothesis', 'met_need': False,
                      'source_ref': 'hypothesis:' + ep, 'source_episode_id': plan['source_episode_id'], 'plan': plan}
            result['receipt'] = digest(result)
            self.ledger.finish(ep, 'observed', result, decision['next_check_seconds'])
            return {'status': 'hypothesized', 'episode_id': ep, 'hypothesis_id': ep}
        if decision['action'] == 'experiment':
            from .world_model import run_trial
            try:
                return await run_trial(self.ledger, self.model, self.environment, ep, decision['hypothesis_id'],
                                       decision['goal'], self.daily_cap, self.stop_path, decision['next_check_seconds'])
            except BaseException as exc:
                row = self.ledger.db.execute('SELECT status FROM episodes WHERE owner=? AND id=?', (self.ledger.owner, ep)).fetchone()
                if row and row['status']=='started':
                    self.ledger.finish(ep, 'unknown' if isinstance(exc, asyncio.CancelledError) else 'tool_error',
                                       {'error_type': type(exc).__name__}, decision['next_check_seconds'])
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if not isinstance(exc, Exception):
                    raise
                return {'status': 'experiment_error', 'episode_id': ep, 'error_type': type(exc).__name__}
        if decision['action'] == 'search':
            if decision.get('belief_id'):
                current = next((h for h in self.ledger.view()['experimental_world_model'] if h['id']==decision['belief_id']), None)
                if not current or current['version'] != decision['belief_version']:
                    self.ledger.finish(ep, 'cancelled', {'reason':'experimental_belief_changed'}, decision['next_check_seconds'])
                    return {'status':'cancelled','episode_id':ep}
            try:
                outcome = await asyncio.wait_for(self.environment.search(decision['search_query'], decision['goal'], provider=decision['search_provider']), timeout=30)
                if not isinstance(outcome, dict) or outcome.get('status') not in {'ok', 'error'} or not isinstance(outcome.get('discoveries'), list):
                    raise ValueError('invalid_search_receipt')
                result = {**outcome, 'source_ref': 'search:' + digest([outcome.get('provider'), decision['search_query']])[:24],
                          'family': 'web_search_catalog', 'text': '', 'met_need': False,
                          'catalog_found': outcome['status'] == 'ok' and bool(outcome['discoveries']),
                          'observed_at': datetime.now(timezone.utc).isoformat()}
                result['prediction_error'] = decision['predict_success'] != result['catalog_found']
                result['receipt'] = digest(result)
                status = 'observed' if outcome['status'] == 'ok' else 'tool_error'
                self.ledger.finish(ep, status, result, decision['next_check_seconds'])
                return {'status': status, 'episode_id': ep, 'catalog_found': result['catalog_found'],
                        'candidate_count': len(outcome['discoveries']), 'met_need': False}
            except asyncio.CancelledError:
                self.ledger.finish(ep, 'unknown', {'error': 'search_interrupted'})
                raise
            except Exception as exc:
                self.ledger.finish(ep, 'tool_error', {'error_type': type(exc).__name__}, decision['next_check_seconds'])
                return {'status': 'tool_error', 'episode_id': ep, 'error_type': type(exc).__name__}
        resource = next(r for r in resources if r["id"] == decision["resource_id"])
        if pending:
            social.defer(pending['id'], decision['next_check_seconds'])
        try:
            reader=getattr(self.environment,'inspect',self.environment.read) if decision['action']=='inspect' else self.environment.read
            outcome = await asyncio.wait_for(reader(resource), timeout=90)
            if not isinstance(outcome, dict) or outcome.get("status") not in {"ok", "empty", "error"}:
                raise ValueError("invalid_tool_receipt")
            if not isinstance(outcome.get("source_ref"), str) or not outcome["source_ref"]:
                raise ValueError("missing_source_reference")
            result = {**outcome, "family": resource["family"], "observed_at": datetime.now(timezone.utc).isoformat()}
            if decision['action']=='inspect':
                result['text']=str(result.get('text',''))[:1200]
                available=outcome['status']=='ok' and len(result['text'].strip())>=10
                result.update(met_need=False,observation_available=available,
                              prediction_error=decision['predict_success']!=available,
                              basis='limited_observation_not_question_progress')
                if decision.get('selection_process')=='idle_selective_attention':
                    result['attention_intent']={'reason':decision['reason'],
                        'basis':'fallible_pre_observation_choice_not_rule_or_knowledge'}
                result['receipt']=digest(result)
                status='observed' if result['status']=='ok' else 'tool_error'
                self.ledger.finish(ep,status,result,decision['next_check_seconds'])
                return {'status':status,'episode_id':ep,'receipt':result['receipt'],'met_need':False,
                        'observation_available':available,'prediction_error':result['prediction_error'],'learning_assessed':False}
            result["text"] = str(result.get("text", ""))[:5000]
            need = decision["goal"]["need"]
            result["met_need"] = outcome["status"] == "ok" and (
                (need == "text" and len(result["text"].strip()) >= 80)
                or (need == "audio" and outcome.get("audio_observed") is True)
                or (need == "primary" and outcome.get("primary_observed") is True and len(result["text"].strip()) >= 80))
            result["prediction_error"] = decision["predict_success"] != result["met_need"]
            result["reflection"] = {"predicted_evidence": need, "predicted_success": decision["predict_success"],
                                     "actual_evidence_met": result["met_need"], "basis": "tool_receipt"}
            result["receipt"] = digest(result)
            if decision.get('inquiry'):
                result['inquiry_assessment'] = {'status': 'unassessed', 'reason': 'assessment_pending'}
            status = "observed" if result["status"] == "ok" else "tool_error"
            self.ledger.finish(ep, status, result, decision["next_check_seconds"])
        except asyncio.CancelledError:
            self.ledger.finish(ep, "unknown", {"error": "interrupted_no_confirmed_result"})
            raise
        except Exception as exc:
            self.ledger.finish(ep, "tool_error", {"error_type": type(exc).__name__}, decision["next_check_seconds"])
            return {"status": "tool_error", "episode_id": ep, "error_type": type(exc).__name__}

        # Evaluate after committing the actual read. A model failure, budget limit
        # or interrupted assessment must never erase a confirmed external result.
        assessment = {'status': 'not_requested'}
        if status == 'observed' and decision.get('inquiry'):
            from .inquiry import assess
            if not (self.stop_path and self.stop_path.exists()):
                assessment = await assess(self.model, self.ledger, decision, result, self.daily_cap, before=context)
                if not self.ledger.annotate_inquiry(ep, assessment, result['receipt']):
                    assessment = {'status': 'unassessed', 'reason': 'source_revoked'}
        return {"status": status, "episode_id": ep, "source_ref": result["source_ref"],
                "met_need": result["met_need"], "prediction_error": result["prediction_error"],
                "receipt": result["receipt"], "inquiry_assessment": assessment}

    async def run(self, rounds=0, interval=None):
        recovered = self.ledger.recover()
        print(encoded({"event": "recovery", "interrupted_episodes": recovered}), flush=True)
        n = 0
        last_wait_code = None
        while not rounds or n < rounds:
            if self.stop_path and self.stop_path.exists():
                break
            if interval is None:
                delay = self.ledger.view()["next_at"] - time.time()
                if delay > 0:
                    await asyncio.sleep(min(delay, 5))
                    continue
            try:
                result = await self.tick(opportunity_source='runtime' if rounds==0 and interval is None else 'experiment' if interval is not None else 'manual')
                if result.get('catalog_found') and result.get('status')=='observed':
                    cfg=self.environment.current_config() if hasattr(self.environment,'current_config') else getattr(self.environment,'cfg',{})
                    if cfg.get('cognition_source_followup_enabled') is True:
                        try:
                            followup=await self.tick(opportunity_source='runtime' if rounds==0 and interval is None else 'experiment' if interval is not None else 'manual',
                                                     source_search_id=result['episode_id'])
                        except Exception as exc:
                            followup={'status':'runtime_error','error_type':type(exc).__name__}
                        print(encoded({'event':'source_result_followup','parent_search_id':result['episode_id'],**followup}),flush=True)
            except Exception as exc:
                code = str(exc) if isinstance(exc, RuntimeError) else ''
                result = {"status": "budget_wait" if code == 'daily_model_budget_exhausted' else "runtime_error",
                          "error_type": type(exc).__name__, "code": code}
            if result['status'] != 'budget_wait' or last_wait_code != result['code']:
                print(encoded({"event": "outcome", **result}), flush=True)
            last_wait_code = result.get('code') if result['status'] == 'budget_wait' else None
            n += 1
            if rounds and n >= rounds:
                break
            # No catch-up queue: a fresh decision follows downtime.
            if interval is not None:
                await asyncio.sleep(interval)
            elif result["status"] in {"runtime_error", "budget_wait"}:
                # Poll STOP during backoff; neither retry storms nor a one-minute
                # uninterruptible stop delay should follow a depleted budget.
                for _ in range(12):
                    if self.stop_path and self.stop_path.exists():
                        break
                    await asyncio.sleep(5)
