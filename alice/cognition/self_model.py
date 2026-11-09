"""Functional Self-Model, Selective Attention signals, and Intrinsic Motivation.

Implements Systems 1 (Selective Attention / Novelty / Habituation),
3 (Intrinsic Motivation / Learning Progress / Satiation), and
6 (Functional Self-Model: Capabilities, Limitations, Prediction Discrepancies,
Emerged Interests, Behavioral Tendencies, Unresolved Questions) from the
brain-inspired digital life architecture without preset persona scripts.
"""
from __future__ import annotations

import re


def _bigrams(text: str) -> set[str]:
    out = set(re.findall(r"[a-zA-Z0-9]{3,}", (text or "").casefold()))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text or ""):
        out.update(run[i : i + 2] for i in range(min(len(run) - 1, 60)))
    return out


_GENERIC_CUE_TERMS = {
    "问题", "什么", "为什么", "如何", "怎么", "怎样", "是否", "哪些", "哪个",
    "具体", "关于", "提到", "讨论", "说明", "解释", "原因", "结果", "部分",
    "原文", "文本", "材料", "内容", "视频", "标题", "评论", "论区", "观众", "作者",
    "可能", "应该", "需要", "已经", "目前", "现在", "当时", "后续", "之后",
    "之前", "继续", "开始", "进行", "通过", "使用", "提供", "存在", "出现",
}
_FUNCTION_CHAR_RE = re.compile(
    r"[的了着过是在有和与或把被给让对向从到为以及而但却就都也还又再才只不没未很太更最"
    r"这那哪谁我你他她它们个位些次种样里上下中外前后去来出进要想能会可看说听走跑坐站"
    r"吃喝穿住叫喊问回答知觉得以为因所如果虽然么吗呢吧啊呀哦哈呃嗯]+"
)


def _cue_terms(text: str) -> set[str]:
    out = set(re.findall(r"[a-zA-Z0-9]{2,}", (text or "").casefold()))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text or ""):
        for seg in _FUNCTION_CHAR_RE.split(run):
            if len(seg) >= 2:
                out.update(seg[i : i + 2] for i in range(min(len(seg) - 1, 40)))
    return out - _GENERIC_CUE_TERMS


def retrieve_related_knowledge(state: dict, query_texts: list[str] | tuple[str, ...],
                               *, exclude_goal_id: str | None = None, limit: int = 4) -> list[dict]:
    """Cue-driven associative recall of receipt-bound findings across goals.

    Reads only surviving, non-revoked `question_states` and `progress_memory`
    already scoped to the caller's `(scope, actor)` visibility. Revoking an
    episode or inquiry judgment immediately removes it from recall.
    """
    cues: set[str] = set()
    for text in query_texts or ():
        if text:
            cues.update(_cue_terms(str(text)))
    if not cues:
        return []
    goals = {g["id"]: g for g in (state.get("goals") or []) if isinstance(g, dict) and g.get("id")}
    qstates = state.get("question_states") or {}
    pmem = state.get("progress_memory") or {}
    candidates: list[dict] = []
    for gid, goal in goals.items():
        if exclude_goal_id and gid == exclude_goal_id:
            continue
        covered_eps: set[str] = set()
        qs = qstates.get(gid)
        if isinstance(qs, dict) and qs.get("parts"):
            for part in qs["parts"]:
                if not isinstance(part, dict):
                    continue
                claims = part.get("claims") or ([part["claim"]] if isinstance(part.get("claim"), dict) else [])
                for claim in reversed(claims):
                    if not isinstance(claim, dict) or not claim.get("answer") or not claim.get("evidence"):
                        continue
                    ev = [
                        {
                            "episode_id": e["episode_id"],
                            "span_id": e.get("span_id"),
                            "quote": e.get("quote", ""),
                            "receipt": e["receipt"],
                        }
                        for e in claim["evidence"]
                        if isinstance(e, dict) and e.get("episode_id") and e.get("receipt")
                    ]
                    if not ev:
                        continue
                    covered_eps.update(e["episode_id"] for e in ev)
                    candidates.append({
                        "goal_id": gid,
                        "goal_status": goal.get("status"),
                        "question": part.get("question") or goal.get("question", ""),
                        "answer": claim["answer"],
                        "coverage": claim.get("coverage", "partial"),
                        "status": part.get("status"),
                        "missing": None,
                        "source_episode_id": qs.get("last_integration"),
                        "evidence": ev[:2],
                        "basis": "fallible_source_bound_prior_knowledge_within_original_evidence_scope",
                    })
        for item in pmem.get(gid) or []:
            if (not isinstance(item, dict) or not item.get("episode_id")
                    or not item.get("receipt") or not item.get("answer")):
                continue
            if item["episode_id"] in covered_eps:
                continue
            candidates.append({
                "goal_id": gid,
                "goal_status": goal.get("status"),
                "question": goal.get("question", ""),
                "answer": item["answer"],
                "coverage": (
                    "answer"
                    if item.get("progress_type") == "new_answer" and not item.get("missing")
                    else "partial"
                ),
                "missing": item.get("missing"),
                "source_episode_id": item["episode_id"],
                "evidence": [{
                    "episode_id": item["episode_id"],
                    "source_ref": item.get("source_ref"),
                    "span_id": item.get("evidence_id"),
                    "quote": item.get("quote", ""),
                    "receipt": item["receipt"],
                }],
                "basis": "fallible_source_bound_prior_knowledge_within_original_evidence_scope",
            })
    scored = []
    for idx, entry in enumerate(candidates):
        entry_text = f"{entry['question']} {entry['answer']}"
        matched = cues & _cue_terms(entry_text)
        if not matched:
            continue
        scored.append((len(matched), -idx, {**entry, "matched_cues": sorted(matched)[:6]}))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    per_goal: dict[str, int] = {}
    for _, _, item in scored:
        gid = item["goal_id"]
        if per_goal.get(gid, 0) >= 2:
            continue
        per_goal[gid] = per_goal.get(gid, 0) + 1
        out.append(item)
        if len(out) >= limit:
            break
    return out


def topic_key(resource_id: str, family: str = "") -> str:
    rid = str(resource_id or "")
    parts = rid.split(":")
    if len(parts) >= 2 and parts[0] in {"book", "novel", "web"}:
        return f"{parts[0]}:{parts[1]}"
    if parts and parts[0]:
        return parts[0]
    return family or "unknown"


def compute_attention_signals(resource: dict, strategies: list[dict], need: str | None = None,
                              episodes: list[dict] | None = None, active_goal: dict | None = None) -> dict:
    matches = [s for s in strategies if s["family"] == resource.get("family") and (not need or s["need"] == need)]
    relevant = next((s for s in matches if s["need"] == need), None)
    affordance = round((relevant["successes"] + 1) / (relevant["successes"] + relevant["failures"] + 2), 3) if relevant else 0.5

    valid_eps = [e for e in (episodes or []) if not e.get("revoked") and e.get("status") == "observed"]
    seen_ids = {
        (e.get("decision") or {}).get("resource_id")
        for e in valid_eps
        if (e.get("decision") or {}).get("action") == "read"
    }
    novelty = 0.0 if resource.get("id") in seen_ids else 1.0

    # Consecutive streak on the same family (habituation decay)
    streak = 0
    for e in valid_eps:
        dec = e.get("decision") or {}
        res = e.get("result") or {}
        if dec.get("action") == "read" and res.get("family") == resource.get("family"):
            streak += 1
        else:
            break
    habituation = round(min(0.24, max(0, streak - 2) * 0.06), 3)

    # Cognitive relevance to active goal question
    relevance = 0.0
    if active_goal and active_goal.get("question"):
        q_terms = _bigrams(active_goal["question"])
        r_terms = _bigrams(f"{resource.get('title', '')} {resource.get('summary', '')} {resource.get('id', '')}")
        if q_terms and r_terms:
            overlap = len(q_terms & r_terms) / max(1, min(len(q_terms), 8))
            relevance = round(min(0.15, overlap * 0.15), 3)

    # Combine with affordance as primary anchor when a specific need is active
    if not episodes and not active_goal:
        score = affordance
    else:
        novelty_adj = 0.06 if novelty > 0 else -0.08
        score = round(max(0.05, min(0.99, affordance + novelty_adj + relevance - habituation)), 3)

    return {
        "affordance": affordance,
        "novelty": novelty,
        "relevance": relevance,
        "habituation": habituation,
        "family_streak": streak,
        "score": score,
    }


def synthesize_self_model(state: dict) -> dict:
    """Derive functional self-model and intrinsic motivation from actual ledger history."""
    strategies = state.get("strategies") or []
    episodes = [e for e in (state.get("episodes") or []) if not e.get("revoked")]
    goals = state.get("goals") or []

    verified_capabilities = []
    known_limitations = []
    for s in strategies:
        total = s["successes"] + s["failures"]
        est = round((s["successes"] + 1) / (total + 2), 3)
        entry = {
            "family": s["family"],
            "need": s["need"],
            "estimate": est,
            "successes": s["successes"],
            "failures": s["failures"],
        }
        if s["successes"] > s["failures"]:
            verified_capabilities.append(entry)
        if s["failures"] > 0:
            known_limitations.append(entry)

    # Prediction vs actual outcome calibration
    evaluated = []
    surprises = []
    topic_counts: dict[str, dict] = {}
    action_counts = {
        "read": 0,
        "reflect": 0,
        "rest": 0,
        "search": 0,
        "hypothesize": 0,
        "experiment": 0,
        "orient": 0,
        "integrate": 0,
        "evaluate": 0,
    }
    read_streak = 0
    streak_open = True

    for e in episodes:
        dec = e.get("decision") or {}
        res = e.get("result") or {}
        act = dec.get("action")
        if act in action_counts:
            action_counts[act] += 1
        if streak_open:
            if act == "read":
                read_streak += 1
            else:
                streak_open = False

        if e.get("status") == "observed" and act == "read":
            rid = dec.get("resource_id") or ""
            fam = res.get("family") or ""
            tkey = topic_key(rid, fam)
            slot = topic_counts.setdefault(tkey, {"topic": tkey, "family": fam, "reads": 0, "met_need": 0})
            slot["reads"] += 1
            if res.get("met_need"):
                slot["met_need"] += 1

            pred = dec.get("predict_success")
            actual = res.get("met_need")
            if isinstance(pred, bool) and isinstance(actual, bool):
                matched = pred == actual
                evaluated.append(matched)
                if not matched and len(surprises) < 3:
                    surprises.append({
                        "episode_id": e["id"],
                        "resource_id": rid,
                        "predicted": pred,
                        "actual_met_need": actual,
                    })

    accuracy = round(sum(evaluated) / len(evaluated), 3) if evaluated else None
    if len(evaluated) >= 2:
        mid = len(evaluated) // 2
        # episodes are ordered newest-first
        recent_acc = sum(evaluated[:mid]) / max(1, mid)
        older_acc = sum(evaluated[mid:]) / max(1, len(evaluated) - mid)
        transport_accuracy_change = round(recent_acc - older_acc, 3)
    else:
        transport_accuracy_change = 0.0

    activity_topics = sorted(
        (
            {
                "topic": v["topic"],
                "family": v["family"],
                "episodes": v["reads"],
                "satisfaction_rate": round(v["met_need"] / max(1, v["reads"]), 3),
                'basis':'activity_frequency_and_transport_receipts_not_personality',
            }
            for v in topic_counts.values()
        ),
        key=lambda x: (-x["episodes"], x["topic"]),
    )[:5]

    qstates = state.get("question_states") or {}
    unresolved = []
    for g in goals:
        if g.get("status") not in {"active", "paused"}:
            continue
        item = {"id": g["id"], "question": g["question"], "need": g["need"], "status": g["status"]}
        qs = qstates.get(g["id"])
        if qs and qs.get("parts"):
            item["open_subquestions"] = [
                p["question"] for p in qs["parts"] if p.get("status") != "tentatively_answered"
            ]
            item["answered_subquestions"] = [
                p["question"] for p in qs["parts"] if p.get("status") == "tentatively_answered"
            ]
        unresolved.append(item)

    satiation = round(min(1.0, read_streak / 6.0), 2)
    motivation=state.get('motivation') or {}
    focused=next((p for p in motivation.get('goals',[]) if p['goal_id']==motivation.get('recommended_goal_id')), {})
    return {
        "capabilities": verified_capabilities,
        "limitations": known_limitations,
        "prediction_calibration": {
            "evaluated_count": len(evaluated),
            "accuracy": accuracy,
            "error_count": len(evaluated) - sum(evaluated),
            "recent_surprises": surprises,
            'recent_accuracy_change':transport_accuracy_change,
        },
        "emerged_interests": [],
        'current_concerns':[{k:i[k] for k in ['goal_id','stance','why','method','revisit_when','condition_observed']}
                            for i in state.get('workspace',{}).get('intentions',[])][:6],
        'activity_topics':activity_topics,
        'interest_cues':motivation.get('interest_cues',[]),
        "behavioral_tendencies": {
            "recent_actions": action_counts,
            "consecutive_read_streak": read_streak,
        },
        "intrinsic_motivation": {
            "learning_progress": focused.get('progress_rate_change'),
            'learning_progress_basis':'same_goal_informative_outcome_rate_change_not_model_capability_proof',
            "satiation_level": None,
            'activity_streak_level':satiation,
            'activity_streak_basis':'fixed_count_heuristic_not_physiological_fatigue',
            "rest_or_shift_suggested": motivation.get('rest_or_change_method_supported',False),
        },
        "unresolved_questions": unresolved,
    }
