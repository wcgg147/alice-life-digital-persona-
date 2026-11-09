"""Engineering candidate verification, A/B comparison, and reversible lifecycle.

An engineering candidate proposed during reflection is a hypothesis, not a fix.
It advances only through executable evidence:
  proposed -> reproduced -> verified -> applied -> rolled_back / withdrawn
"""
from __future__ import annotations

import json
import time

from .state import digest, encoded

ENGINEERING_STATUSES = {
    "proposed",
    "unreproduced",
    "reproduced",
    "verified",
    "rejected",
    "applied",
    "rolled_back",
    "withdrawn",
}


class EngineeringLoop:
    def __init__(self, ledger):
        self.ledger, self.db, self.owner = ledger, ledger.db, ledger.owner

    def get(self, candidate_id):
        row = self.db.execute(
            "SELECT c.*, e.revoked AS source_revoked, e.text AS source_text "
            "FROM engineering_candidates c "
            "JOIN social_events e ON e.owner=c.owner AND e.id=c.source_event "
            "WHERE c.owner=? AND c.id=?",
            (self.owner, candidate_id),
        ).fetchone()
        if not row:
            raise KeyError("unknown_engineering_candidate")
        item = dict(row)
        item["proposal"] = json.loads(item.pop("proposal_json"))
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        return item

    def list_candidates(self, scope=None, actor=None, statuses=None):
        sql = (
            "SELECT c.id FROM engineering_candidates c "
            "JOIN social_events e ON e.owner=c.owner AND e.id=c.source_event "
            "WHERE c.owner=? AND e.revoked=0"
        )
        args = [self.owner]
        if scope is not None:
            sql += " AND c.scope=? AND c.actor=?"
            args.extend([scope, str(actor or "")])
        if statuses:
            marks = ",".join("?" for _ in statuses)
            sql += f" AND c.status IN ({marks})"
            args.extend(statuses)
        sql += " ORDER BY e.received DESC"
        return [self.get(r[0]) for r in self.db.execute(sql, args).fetchall()]

    def evaluate(self, candidate_id, probe_fn):
        """Run an isolated before/after probe and update candidate status from evidence.

        probe_fn(candidate) must return a dict with:
          - reproduced: bool (whether the baseline defect was reproduced on the trigger case)
          - baseline_cases: list[dict] (each with 'id' and 'passed': bool)
          - candidate_cases: list[dict] (each with 'id' and 'passed': bool, including holdouts)
          - holdout_cases: list[dict] (each with 'id' and 'passed': bool on unseen inputs)
          - suite_passed: bool (whether regression unit tests passed)
          - artifacts: dict (optional file hashes, diffs, or metric summaries)
        """
        cand = self.get(candidate_id)
        if cand["source_revoked"] or cand["status"] == "withdrawn":
            raise PermissionError("candidate_source_revoked")
        report = probe_fn(cand)
        if not isinstance(report, dict) or type(report.get("reproduced")) is not bool:
            raise ValueError("invalid_probe_report")
        for key in ("baseline_cases", "candidate_cases", "holdout_cases"):
            cases = report.get(key)
            if not isinstance(cases, list) or not cases or any(
                not isinstance(c, dict) or not c.get("id") or type(c.get("passed")) is not bool
                for c in cases
            ):
                raise ValueError(f"invalid_probe_cases:{key}")
        if type(report.get("suite_passed")) is not bool:
            raise ValueError("suite_status_required")

        reproduced = report["reproduced"]
        base_pass = sum(1 for c in report["baseline_cases"] if c["passed"])
        cand_pass = sum(1 for c in report["candidate_cases"] if c["passed"])
        hold_pass = sum(1 for c in report["holdout_cases"] if c["passed"])
        all_cand_ok = cand_pass == len(report["candidate_cases"])
        all_hold_ok = hold_pass == len(report["holdout_cases"])

        if not reproduced:
            status = "unreproduced"
        elif all_cand_ok and all_hold_ok and report["suite_passed"] and cand_pass > base_pass:
            status = "verified"
        else:
            status = "reproduced" if not (all_cand_ok and all_hold_ok and report["suite_passed"]) else "rejected"

        evidence = {
            **cand["evidence"],
            "evaluated_at": time.time(),
            "reproduced": reproduced,
            "baseline_passed": f"{base_pass}/{len(report['baseline_cases'])}",
            "candidate_passed": f"{cand_pass}/{len(report['candidate_cases'])}",
            "holdout_passed": f"{hold_pass}/{len(report['holdout_cases'])}",
            "suite_passed": report["suite_passed"],
            "baseline_cases": report["baseline_cases"],
            "candidate_cases": report["candidate_cases"],
            "holdout_cases": report["holdout_cases"],
            "artifacts": report.get("artifacts", {}),
            "receipt": digest(report),
        }
        with self.db:
            self.db.execute(
                "UPDATE engineering_candidates SET status=?, evidence_json=?, updated=? WHERE owner=? AND id=?",
                (status, encoded(evidence), time.time(), self.owner, candidate_id),
            )
            self.db.execute("UPDATE cognitive_state SET revision=revision+1 WHERE owner=?", (self.owner,))
        return self.get(candidate_id)

    def mark_applied(self, candidate_id, *, target_files, rollback_token, test_receipt):
        cand = self.get(candidate_id)
        if cand["source_revoked"] or cand["status"] != "verified":
            raise ValueError("only_verified_candidate_can_be_applied")
        if not isinstance(target_files, list) or not target_files or not rollback_token or not test_receipt:
            raise ValueError("application_metadata_required")
        evidence = {
            **cand["evidence"],
            "applied_at": time.time(),
            "target_files": target_files,
            "rollback_token": str(rollback_token),
            "test_receipt": str(test_receipt),
        }
        with self.db:
            self.db.execute(
                "UPDATE engineering_candidates SET status='applied', evidence_json=?, updated=? WHERE owner=? AND id=?",
                (encoded(evidence), time.time(), self.owner, candidate_id),
            )
            self.db.execute("UPDATE cognitive_state SET revision=revision+1 WHERE owner=?", (self.owner,))
        return self.get(candidate_id)

    def rollback(self, candidate_id, reason):
        cand = self.get(candidate_id)
        if cand["status"] not in {"applied", "verified", "reproduced"}:
            raise ValueError("candidate_not_rollbackable")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300:
            raise ValueError("rollback_reason_required")
        evidence = {
            **cand["evidence"],
            "rolled_back_at": time.time(),
            "rollback_reason": reason.strip(),
        }
        with self.db:
            self.db.execute(
                "UPDATE engineering_candidates SET status='rolled_back', evidence_json=?, updated=? WHERE owner=? AND id=?",
                (encoded(evidence), time.time(), self.owner, candidate_id),
            )
            self.db.execute("UPDATE cognitive_state SET revision=revision+1 WHERE owner=?", (self.owner,))
        return self.get(candidate_id)
