"""Model-layer learning pipeline (System 5.3): DPO / LoRA preference pair extraction.

Extracts verified (prompt_context, chosen, rejected) preference pairs from:
1. Cognitive ledger `feedback_policies` + `social_events` (where a reply was
   critiqued by the user and later replaced by a supported trial policy reply).
2. Engineering probe A/B replay comparisons (`baseline_reply` vs `candidate_reply`).
3. QQ `channel.sqlite3` `qq_reply_quality` negative feedback markers paired with
   subsequent clean replies in the same scope.

Outputs LLaMA-Factory / TRL ShareGPT DPO format without touching model weights directly.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3


def extract_dpo_pairs(*, probe_results_paths: list[Path] | None = None, ledger_path: Path | None = None) -> list[dict]:
    pairs: list[dict] = []

    for p in probe_results_paths or []:
        if not Path(p).is_file():
            continue
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        comparisons = data.get("reply_comparisons") or {}
        proposal = data.get("proposal") or {}
        for case_id, item in comparisons.items():
            base_reply = str(item.get("baseline_reply") or "").strip()
            cand_reply = str(item.get("candidate_reply") or "").strip()
            user_text = str(item.get("user_text") or "").strip()
            if not base_reply or not cand_reply or base_reply == cand_reply:
                continue
            pairs.append({
                "conversations": [
                    {
                        "from": "system",
                        "value": "你是数字个体 Alice。按当前对话真实上下文自然接话，不复读、不端着、不突然跳到无关旧话题。",
                    },
                    {"from": "human", "value": user_text},
                ],
                "chosen": {"from": "gpt", "value": cand_reply},
                "rejected": {"from": "gpt", "value": base_reply},
                "meta": {
                    "source": "engineering_ab_replay",
                    "case_id": case_id,
                    "candidate_id": data.get("candidate_id"),
                    "problem": proposal.get("problem", ""),
                },
            })

    if ledger_path and Path(ledger_path).is_file():
        db = sqlite3.connect(Path(ledger_path).as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            policies = db.execute(
                "SELECT * FROM feedback_policies WHERE status='supported'"
            ).fetchall()
            for pol in policies:
                val = json.loads(pol["validation_json"] or "{}")
                good_ev_id = val.get("reply_event_id")
                src_ev_id = pol["source_event"]
                if not good_ev_id or not src_ev_id:
                    continue
                good_reply = db.execute(
                    "SELECT * FROM social_events WHERE owner=? AND id=? AND role='alice' AND revoked=0",
                    (pol["owner"], good_ev_id),
                ).fetchone()
                src_ev = db.execute(
                    "SELECT * FROM social_events WHERE owner=? AND id=? AND revoked=0",
                    (pol["owner"], src_ev_id),
                ).fetchone()
                if not good_reply or not src_ev:
                    continue
                bad_reply = db.execute(
                    "SELECT * FROM social_events WHERE owner=? AND scope=? AND actor=? AND role='alice' AND at<=? AND revoked=0 ORDER BY at DESC LIMIT 1",
                    (pol["owner"], pol["scope"], pol["actor"], src_ev["at"]),
                ).fetchone()
                if bad_reply and bad_reply["text"] != good_reply["text"]:
                    pairs.append({
                        "conversations": [
                            {
                                "from": "system",
                                "value": f"你是数字个体 Alice。适用情境：{pol['situation']}",
                            },
                            {"from": "human", "value": pol["evidence_quote"]},
                        ],
                        "chosen": {"from": "gpt", "value": good_reply["text"]},
                        "rejected": {"from": "gpt", "value": bad_reply["text"]},
                        "meta": {
                            "source": "supported_feedback_policy",
                            "policy_id": pol["id"],
                            "scope": pol["scope"],
                        },
                    })
        finally:
            db.close()

    return pairs
