"""Source-scoped social experience and reversible local behavior hypotheses."""
import json
import re
import time
from datetime import datetime,timezone,timedelta

from .state import digest, encoded


def migrate(db):
    for table in ("goals", "episodes"):
        columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for column, default in [("scope", "public"), ("actor", "")]:
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS social_events(
      owner TEXT, id TEXT, scope TEXT, actor TEXT, role TEXT, text TEXT,
      at REAL, received REAL, status TEXT, payload_json TEXT, revoked INTEGER DEFAULT 0,
      PRIMARY KEY(owner,id));
    CREATE INDEX IF NOT EXISTS social_pending ON social_events(owner,status,received);
    CREATE TABLE IF NOT EXISTS feedback_policies(
      owner TEXT, id TEXT, scope TEXT, actor TEXT, guidance TEXT, situation TEXT,
      source_event TEXT, quote TEXT, status TEXT, validation_json TEXT,
      PRIMARY KEY(owner,id));
    CREATE TABLE IF NOT EXISTS engineering_candidates(
      owner TEXT, id TEXT, scope TEXT, actor TEXT, source_event TEXT,
      proposal_json TEXT, status TEXT, PRIMARY KEY(owner,id));
    PRAGMA user_version=2;
    """)
    eng_cols = {r[1] for r in db.execute("PRAGMA table_info(engineering_candidates)")}
    if "evidence_json" not in eng_cols:
        db.execute("ALTER TABLE engineering_candidates ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'")
    if "updated" not in eng_cols:
        db.execute("ALTER TABLE engineering_candidates ADD COLUMN updated REAL NOT NULL DEFAULT 0")


def permitted(config, scope, actor):
    if not config.get("enabled", False) or not str(actor).isdigit():
        return False
    if scope.startswith("private:"):
        return scope == "private:" + actor and actor in {str(x) for x in config.get("allowed_private_users", [])}
    return scope in {str(x) for x in config.get("allowed_groups", [])}


def feedback_candidate(text):
    return bool(re.search(r"你.{0,12}(?:重复|反问|道歉|认错|啰嗦|回复|记错|串|表情|语音|模板|听错|话题|跳跃|跳太)|别再|不要每|why do you|you (?:keep|always|already)|stop (?:asking|repeating)|too (?:long|many)", text, re.I))


class SocialState:
    def __init__(self, ledger):
        self.ledger, self.db, self.owner = ledger, ledger.db, ledger.owner

    def ingest(self, event, config):
        scope, actor = str(event.get("scope", "")), str(event.get("actor", ""))
        if not permitted(config, scope, actor):
            raise PermissionError("scope_not_authorized")
        if event.get("role") not in {"person", "alice"}:
            raise ValueError("invalid_event_role")
        text, native_id = event.get("text"), str(event.get("message_id", ""))
        if not isinstance(text, str) or not 1 <= len(text) <= 4000 or not 1 <= len(native_id) <= 160:
            raise ValueError("invalid_event_material")
        if type(event.get("at")) not in {int, float} or not 0 < event["at"] <= time.time() + 300:
            raise ValueError("invalid_event_time")
        if event["role"] == "alice" and not event.get("receipts"):
            raise ValueError("delivery_receipt_required")
        key = digest(["qq", scope, event["role"], native_id])[:28]
        # Model fields never control source type, event identity, or permissions.
        payload = {k: event.get(k) for k in ["message_id", "receipts", "reply_to", "basis", "voice_source", "source_record"]}
        saved = (event.get('source_record') or {}).get('kind')=='saved_qq_message'
        pending = event["role"] == "person" and (
            feedback_candidate(text) or (not saved and scope == "private:" + str(config.get("account_owner_qq", ""))))
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO social_events VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (self.owner, key, scope, actor, event["role"], text, event["at"], time.time(),
                 "pending" if pending else "recorded", encoded(payload))).rowcount
            if inserted and pending:
                self.db.execute("UPDATE cognitive_state SET next_at=MIN(next_at,?),revision=revision+1 WHERE owner=?", (time.time(), self.owner))
        return key, bool(inserted)

    def pending(self, config):
        scopes = [str(x) for x in config.get('allowed_groups', [])] + ['private:'+str(x) for x in config.get('allowed_private_users', [])]
        if not scopes or not config.get('enabled'):
            return None
        marks = ','.join('?' for _ in scopes)
        rows = self.db.execute(f"SELECT * FROM social_events WHERE owner=? AND scope IN ({marks}) AND role='person' AND status IN ('pending','deferred') AND revoked=0 ORDER BY received LIMIT 40", (self.owner, *scopes))
        candidates = [dict(r) for r in rows if permitted(config, r["scope"], r["actor"]) and json.loads(r['payload_json']).get('review_after',0)<=time.time()]
        return next((r for r in candidates if feedback_candidate(r["text"])), candidates[0] if candidates else None)

    def defer(self, event_id, seconds):
        row = self.db.execute("SELECT payload_json FROM social_events WHERE owner=? AND id=? AND revoked=0",(self.owner,event_id)).fetchone()
        if row:
            payload = json.loads(row[0]);payload['review_after']=time.time()+seconds
            with self.db:self.db.execute("UPDATE social_events SET status='deferred',payload_json=? WHERE owner=? AND id=?",(encoded(payload),self.owner,event_id))

    def context(self, scope, actor, *, feedback_event_id=None):
        events = [dict(r) for r in self.db.execute("SELECT id,role,text,at,payload_json FROM social_events WHERE owner=? AND scope=? AND actor=? AND revoked=0 ORDER BY at DESC, received DESC LIMIT 6", (self.owner, scope, actor))]
        for e in events:
            e["text"] = e["text"][:700]
            e['at_iso']=datetime.fromtimestamp(e['at'],timezone(timedelta(hours=8))).isoformat()
            e["delivery"] = json.loads(e.pop("payload_json"))
        policies = [dict(r) for r in self.db.execute("SELECT p.* FROM feedback_policies p JOIN social_events e ON e.owner=p.owner AND e.id=p.source_event WHERE p.owner=? AND p.scope=? AND p.actor=? AND p.status IN ('trial','supported') AND e.revoked=0 ORDER BY e.received DESC LIMIT 4", (self.owner, scope, actor))]
        proposals = [dict(id=r['id'],status=r['status'],proposal=json.loads(r['proposal_json']),
                          evidence=json.loads(r['evidence_json'] or '{}')) for r in self.db.execute(
            "SELECT c.* FROM engineering_candidates c JOIN social_events e ON e.owner=c.owner AND e.id=c.source_event WHERE c.owner=? AND c.scope=? AND c.actor=? AND c.status IN ('proposed','reproduced','verified','applied') AND e.revoked=0 ORDER BY e.received DESC LIMIT 3",(self.owner,scope,actor))]
        context={"scope": scope, "actor": actor, "social_events": list(reversed(events)), "policy_trials": policies, "engineering_candidates": proposals}
        if feedback_event_id is not None:
            # Resolve time from the scoped, surviving record, never model input.
            anchor=self.db.execute("SELECT rowid,at FROM social_events WHERE owner=? AND id=? AND scope=? AND actor=? AND role='person' AND revoked=0",
                                   (self.owner,feedback_event_id,scope,actor)).fetchone()
            if not anchor:raise ValueError('unknown_feedback_context_event')
            rows=[dict(r) for r in self.db.execute(
                "SELECT id,role,text,at,payload_json FROM social_events WHERE owner=? AND scope=? AND actor=? AND revoked=0 AND (at<? OR (at=? AND rowid<=?)) ORDER BY at DESC,rowid DESC LIMIT 6",
                (self.owner,scope,actor,anchor['at'],anchor['at'],anchor['rowid']))]
            for row in rows:
                row['text']=row['text'][:700]
                row['at_iso']=datetime.fromtimestamp(row['at'],timezone(timedelta(hours=8))).isoformat()
                row['delivery']=json.loads(row.pop('payload_json'))
            context['feedback_context']={'event_id':feedback_event_id,'at':anchor['at'],
                'social_events':list(reversed(rows)),
                'basis':'scoped_events_at_or_before_feedback_not_latest_conversation'}
        return context

    def apply_assessment(self, event_id, assessment, episode=None, next_seconds=600):
        row = self.db.execute("SELECT * FROM social_events WHERE owner=? AND id=? AND role='person' AND revoked=0 AND status IN ('pending','deferred')", (self.owner, event_id)).fetchone()
        if not row or not isinstance(assessment, dict) or type(assessment.get("is_self_feedback")) is not bool:
            raise ValueError("invalid_feedback_assessment")
        quote = assessment.get("evidence_quote", "")
        if not isinstance(quote, str) or not (4 if assessment["is_self_feedback"] else 1) <= len(quote) <= 400 or quote not in row["text"]:
            raise ValueError("unsupported_feedback_quote")
        accepted = assessment["is_self_feedback"]
        policy, engineering, validation = assessment.get("policy"), assessment.get("engineering"), assessment.get("validation")
        if not accepted and any([policy, engineering, validation]):
            raise ValueError("nonfeedback_cannot_update")
        # Validate ALL proposed changes before opening the transaction.
        if policy and (not isinstance(policy, dict) or any(not isinstance(policy.get(k), str) or not 1 <= len(policy[k]) <= 320 for k in ["guidance", "situation"])):
            raise ValueError("invalid_policy")
        if engineering and (not isinstance(engineering, dict) or any(not isinstance(engineering.get(k), str) or not 1 <= len(engineering[k]) <= 500 for k in ["problem", "hypothesis", "verification"])):
            raise ValueError("invalid_engineering_candidate")
        target = None
        if validation:
            if not isinstance(validation, dict) or validation.get("verdict") not in {"supports", "contradicts", "unknown"}:
                raise ValueError("invalid_validation")
            target = self.db.execute("SELECT * FROM feedback_policies WHERE owner=? AND id=? AND scope=? AND actor=? AND status IN ('trial','supported')", (self.owner, validation.get("policy_id"), row["scope"], row["actor"])).fetchone()
            delivered = self.db.execute("SELECT * FROM social_events WHERE owner=? AND id=? AND scope=? AND actor=? AND role='alice' AND revoked=0 AND at<=?", (self.owner, validation.get("reply_event_id"), row["scope"], row["actor"], row["at"])).fetchone()
            if not target or not delivered or target["id"] not in (json.loads(delivered["payload_json"]).get("basis") or {}).get("policy_ids", []):
                raise ValueError("validation_requires_related_delivered_reply")
        result = {"accepted": accepted, "policy_id": None, "engineering_id": None}
        with self.db:
            if policy:
                pid = digest([event_id, policy])[:24]
                self.db.execute("INSERT OR IGNORE INTO feedback_policies VALUES (?,?,?,?,?,?,?,?,?,?)", (self.owner, pid, row["scope"], row["actor"], policy["guidance"], policy["situation"], event_id, quote, "trial", "{}"))
                question='检验针对这条反馈的调整是否有效：'+quote
                if len(question)>240:question='在后续互动中检验针对这条反馈的调整是否有效'
                self.db.execute("INSERT OR IGNORE INTO goals(owner,id,question,need,origin,status,updated,scope,actor) VALUES (?,?,?,'text',?,'active',?,?,?)",
                                (self.owner,'policy:'+pid,question,event_id,time.time(),row['scope'],row['actor']))
                result["policy_id"] = pid
            if engineering:
                eid = digest([event_id, engineering])[:24]
                self.db.execute("INSERT OR IGNORE INTO engineering_candidates(owner,id,scope,actor,source_event,proposal_json,status,evidence_json,updated) VALUES (?,?,?,?,?,?,?,?,?)",
                                (self.owner, eid, row["scope"], row["actor"], event_id, encoded(engineering), "proposed", "{}", time.time()))
                result["engineering_id"] = eid
            if target:
                status = {"supports": "supported", "contradicts": "revoked", "unknown": target["status"]}[validation["verdict"]]
                self.db.execute("UPDATE feedback_policies SET status=?,validation_json=? WHERE owner=? AND id=?", (status, encoded({**validation, "feedback_event": event_id, "quote": quote, "basis": "speaker_feedback_not_objective_proof"}), self.owner, target["id"]))
                if validation['verdict']!='unknown':
                    self.db.execute("UPDATE goals SET status=?,updated=? WHERE owner=? AND id=?",('completed' if status=='supported' else 'abandoned',time.time(),self.owner,'policy:'+target['id']))
            self.db.execute("UPDATE social_events SET status=? WHERE owner=? AND id=?", ("assessed" if accepted else "declined", self.owner, event_id))
            if episode:
                changed = self.db.execute("UPDATE episodes SET status='reflected',finished=?,result_json=? WHERE owner=? AND id=? AND status='started'",
                                          (time.time(), encoded(result), self.owner, episode)).rowcount
                if not changed:
                    raise ValueError("reflection_episode_not_in_flight")
                self.db.execute("UPDATE cognitive_state SET next_at=? WHERE owner=?", (self.ledger.next_after(episode, next_seconds), self.owner))
            self.db.execute("UPDATE cognitive_state SET revision=revision+1 WHERE owner=?", (self.owner,))
        return result

    def revoke(self, event_id):
        with self.db:
            self.db.execute("UPDATE social_events SET revoked=1 WHERE owner=? AND id=?", (self.owner, event_id))
            self.db.execute("UPDATE feedback_policies SET status='revoked' WHERE owner=? AND source_event=?", (self.owner, event_id))
            for row in self.db.execute("SELECT p.id,p.source_event,p.validation_json,e.revoked FROM feedback_policies p JOIN social_events e ON e.owner=p.owner AND e.id=p.source_event WHERE p.owner=?", (self.owner,)).fetchall():
                if not row["revoked"] and row["source_event"] != event_id and json.loads(row["validation_json"]).get("feedback_event") == event_id:
                    self.db.execute("UPDATE feedback_policies SET status='trial',validation_json=? WHERE owner=? AND id=?",
                                    (encoded({"withdrawn_validation_event": event_id}), self.owner, row["id"]))
                    self.db.execute("UPDATE goals SET status='active',updated=? WHERE owner=? AND id=?",(time.time(),self.owner,'policy:'+row['id']))
            self.db.execute("UPDATE engineering_candidates SET status='withdrawn' WHERE owner=? AND source_event=?", (self.owner, event_id))
            self.db.execute("UPDATE goals SET status='paused' WHERE owner=? AND origin=?", (self.owner, event_id))
            self.db.execute("UPDATE cognitive_state SET revision=revision+1 WHERE owner=?", (self.owner,))

    def reply_packet(self, scope, actor):
        from .self_model import synthesize_self_model
        view = self.ledger.view(scope, actor)
        local = self.context(scope, actor)
        self_model = synthesize_self_model(view)
        return {"kind": "shared_cognitive_state", "revision": view["revision"], "scope": scope,
                "goals": [{k: g[k] for k in ["id", "question", "status"]} for g in view["goals"][:3]],
                "recent_experiences": [{"id": e["id"], "status": e["status"], "source": e["result"].get("source_ref"),
                                         "text": str(e["result"].get("text", ""))[:400]}
                                        for e in view["episodes"]
                                        if not e["revoked"] and (e["result"].get("source_ref") or e["result"].get("text"))][:2],
                "consolidated_memories": view.get("consolidations", [])[:3],
                "inquiry_learning": {"beliefs": view.get("inquiry_model", [])[:12],
                                     "basis": "fallible_model_assessments_of_observed_material",
                                     "not_equivalent_to": "objective_truth_or_permanent_personality"},
                "experimental_beliefs": [{"id": h['id'], "claim": h['plan']['claim'], "status": h['status'],
                                         "support_estimate": h['confidence'], "observed_relevance_rates": h['observed_relevance_rates'],
                                         "scope": h['plan']['scope'], "basis": "fallible_blind_candidate_relevance_scores"}
                                        for h in view.get('experimental_world_model', [])[:4]],
                'method_experience':[{k:t[k] for k in ['goal_id','method','status','known_feedback','new_progress','supporting_episodes','counterexamples','current_feedback','last_known_gap','scope']}
                                     for t in view.get('workspace',{}).get('method_trials',[])][-4:],
                'question_states':list(view.get('question_states',{}).values())[:3],
                "self_model": {k: self_model[k] for k in ["emerged_interests", "activity_topics", "interest_cues", "capabilities", "limitations", "unresolved_questions", "current_concerns"]},
                "policy_trials": [{k: p[k] for k in ["id", "guidance", "situation", "status"]} for p in local["policy_trials"]],
                "engineering_candidates": local['engineering_candidates'],
                "note": "来源与试行策略供内部判断；不是固定人格或已验证能力，不向对方播报字段。"}
