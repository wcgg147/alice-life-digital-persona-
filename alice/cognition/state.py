"""Single-runtime cognitive ledger; separate from the authoritative persona store."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

NEEDS = {"text", "audio", "primary"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class InstanceLock:
    """Kernel-owned lock: process death releases it on both Linux and Windows."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            # Windows byte-range locks also reject reading the locked byte.
            # Treat that admission failure like the locking call itself.
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("cognition_already_running") from None
        return self

    def __exit__(self, *args):
        self.file.close()


class Ledger:
    def __init__(self, path, owner="alice"):
        self.path, self.owner = Path(path), owner
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS cognitive_state(
          owner TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS goals(
          owner TEXT, id TEXT, question TEXT, need TEXT, origin TEXT,
          status TEXT, updated REAL, PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS episodes(
          owner TEXT, id TEXT, started REAL, finished REAL, status TEXT,
          before_json TEXT, decision_json TEXT, result_json TEXT,
          revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS strategies(
          owner TEXT, id TEXT, need TEXT, family TEXT, successes INTEGER,
          failures INTEGER, supports_json TEXT, status TEXT,
          PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS call_budget(
          owner TEXT, day TEXT, calls INTEGER NOT NULL, PRIMARY KEY(owner,day));
        CREATE INDEX IF NOT EXISTS episodes_recent ON episodes(owner,started DESC);
        """)
        from .social import migrate
        from .consolidation import migrate_consolidations, rebuild_consolidations
        migrate(self.db)
        migrate_consolidations(self.db)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO cognitive_state(owner) VALUES (?)", (owner,))
            rebuild_consolidations(self)

    def close(self):
        self.db.close()

    def check_budget(self, cap):
        if type(cap) is not int or cap<0:raise ValueError('invalid_daily_cap')
        if cap==0:return  # Explicit operator policy: count calls without a daily ceiling.
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        row = self.db.execute("SELECT calls FROM call_budget WHERE owner=? AND day=?", (self.owner, day)).fetchone()
        if row and row[0] >= cap:
            raise RuntimeError("daily_model_budget_exhausted")

    def goal_supported(self, goal_id):
        """Withdraw an idle choice without deleting its historical goal row.

        Legacy/manual goals remain compatible. Only another surviving idle
        adoption can retain an idle-born goal; cached execution cannot revive it.
        """
        rows=self.db.execute("SELECT decision_json,revoked FROM episodes WHERE owner=? AND json_extract(decision_json,'$.goal.id')=? ORDER BY started,rowid",
                             (self.owner,goal_id)).fetchall()
        if not rows or json.loads(rows[0]['decision_json']).get('selection_process')!='idle_goal_selection':
            return True
        return any(not row['revoked'] and json.loads(row['decision_json']).get('selection_process')=='idle_goal_selection'
                   for row in rows)

    def reserve_call(self, cap):
        if type(cap) is not int or cap<0:raise ValueError('invalid_daily_cap')
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        # Conditional UPDATE prevents two connections exceeding the daily cap.
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO call_budget VALUES (?,?,0)", (self.owner, day))
            if cap==0:
                changed=self.db.execute('UPDATE call_budget SET calls=calls+1 WHERE owner=? AND day=?',(self.owner,day)).rowcount
            else:
                changed = self.db.execute("UPDATE call_budget SET calls=calls+1 WHERE owner=? AND day=? AND calls<?",
                                          (self.owner, day, cap)).rowcount
        if not changed:
            raise RuntimeError("daily_model_budget_exhausted")

    def recover(self):
        with self.db:
            count = self.db.execute("UPDATE episodes SET status='unknown',finished=? WHERE owner=? AND status='started'",
                                    (time.time(), self.owner)).rowcount
            self.db.execute("UPDATE development_reviews SET status='interrupted',updated=? WHERE owner=? AND status='started'", (time.time(), self.owner))
            return count

    def view(self, scope="public", actor=""):
        state = dict(self.db.execute("SELECT * FROM cognitive_state WHERE owner=?", (self.owner,)).fetchone())
        visibility = "owner=? AND (scope='public' OR (scope=? AND actor=?))"
        args = (self.owner, scope, actor)
        state["goals"] = [dict(r) for r in self.db.execute(
            f"SELECT * FROM goals WHERE {visibility} AND NOT EXISTS (SELECT 1 FROM social_events e WHERE e.owner=goals.owner AND e.id=goals.origin AND e.revoked=1) AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.owner=goals.owner AND e.id=goals.origin AND e.revoked=1) AND NOT EXISTS (SELECT 1 FROM development_reviews d WHERE d.owner=goals.owner AND goals.origin='review:'||d.id AND d.status!='accepted') ORDER BY CASE WHEN status IN ('active','paused') THEN 0 ELSE 1 END,updated DESC LIMIT 32", args)]
        state['goals']=[g for g in state['goals'] if self.goal_supported(g['id'])]
        state["episodes"] = [dict(id=r["id"], status=r["status"], revoked=bool(r["revoked"]),
                                  decision=json.loads(r["decision_json"]), result=json.loads(r["result_json"] or "{}"))
                             for r in self.db.execute(f"SELECT * FROM episodes WHERE {visibility} ORDER BY started DESC,rowid DESC LIMIT 8", args)]
        state["strategies"] = []
        for row in self.db.execute("SELECT * FROM strategies WHERE owner=? AND status='active' ORDER BY id LIMIT 40", (self.owner,)):
            item = dict(row)
            supports = json.loads(item.pop("supports_json"))
            item.update(support_count=len(supports), supporting_episodes=supports[-4:])
            state["strategies"].append(item)
        state["consolidations"] = [
            {
                "id": r["id"],
                "topic": r["topic"],
                "start_ref": r["start_ref"],
                "latest_ref": r["latest_ref"],
                "episode_count": r["episode_count"],
                "summary": r["summary"],
                "open_question": r["open_question"],
                "updated": r["updated"],
            }
            for r in self.db.execute(
                f"SELECT * FROM consolidations WHERE {visibility} AND revoked=0 ORDER BY updated DESC LIMIT 6",
                args,
            )
        ]
        from .inquiry import derive_model, validate_intent, progress_memory
        history=[]
        for r in self.db.execute(f"SELECT * FROM episodes WHERE {visibility} AND revoked=0 AND status!='started' ORDER BY started DESC,rowid DESC", args):
            before=json.loads(r['before_json'] or '{}')
            ep=dict(id=r['id'],status=r['status'],revoked=bool(r['revoked']),scope=r['scope'],actor=r['actor'],
                    opportunity=before.get('opportunity',{'source':'unknown'}),
                    decision=json.loads(r['decision_json']),result=json.loads(r['result_json'] or '{}'))
            if r['status']=='integrated':ep['integration_inputs']=before.get('question_integrations',[])
            history.append(ep)
        # Windows wall-clock timestamps may tie across consecutive commits.
        # Break tied goal timestamps by actual episode insertion order.
        recent_selection={}
        for index,ep in enumerate(history):
            gid=(ep['decision'].get('goal') or {}).get('id')
            if gid:recent_selection.setdefault(gid,index)
        state['goals'].sort(key=lambda g:(-g['updated'],recent_selection.get(g['id'],len(history))))
        state['inquiry_model'] = derive_model(history)
        state['progress_memory'] = progress_memory(history)
        state['inquiry_intents'] = {}
        for ep in history:
            decision = ep['decision']
            if ep['status']=='observed' and decision.get('action') == 'read' and decision.get('inquiry') and decision.get('goal'):
                try:
                    state['inquiry_intents'].setdefault(decision['goal']['id'], validate_intent(decision['inquiry']))
                except (ValueError, TypeError):
                    pass
        from .development import opportunities
        state['development_opportunities'] = opportunities(self, scope, actor)
        from .world_model import derive, sources
        active = {g['id'] for g in state['goals'] if g['status']=='active' and g['scope']=='public'}
        state['experimental_world_model'] = derive(history, active)
        state['hypothesis_sources'] = sources(history, active)
        from .motivation import derive as motivation,memory_goal_origins,source_feedback
        state['motivation'] = motivation(state['goals'], history)
        state['source_feedback']=source_feedback(history)
        state['memory_goal_origins']=memory_goal_origins(history,{e['id'] for e in state['episodes']})
        from .noticing import derive as noticings
        state['inspection_sources'],state['open_questions']=noticings(history,state['goals'])
        from .question_state import derive as questions,opportunities as question_opportunities
        state['question_states']=questions(state['goals'],history)
        state['question_integrations']=question_opportunities(state['goals'],history,state['question_states'])
        waiting={o['goal_id'] for o in state['question_integrations'] if o.get('assessed_useful_count',0)>0}
        for gid,question in state['question_states'].items():question['has_unintegrated_sources']=gid in waiting
        from .pending_learning import candidates
        state['pending_learning']=candidates(self,scope,actor)
        from .workspace import derive as workspace
        state['workspace'] = workspace(state['goals'],history,state['motivation'],state['progress_memory'],
                                       question_states=state['question_states'],
                                       question_integrations=state['question_integrations'],
                                       pending_learning=state['pending_learning'],
                                       experimental_world_model=state['experimental_world_model'],
                                       hypothesis_sources=state['hypothesis_sources'])
        return state

    def revoke_experiment(self, episode, reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300:
            raise ValueError('experiment_withdrawal_reason_required')
        with self.db:
            row = self.db.execute('SELECT result_json FROM episodes WHERE owner=? AND id=?', (self.owner, episode)).fetchone()
            if not row:
                raise KeyError('unknown_episode')
            r = json.loads(row[0] or '{}')
            if 'experiment_assessment' not in r:
                raise ValueError('not_an_experiment')
            if not r.get('experiment_revoked'):
                r['withdrawn_experiment_assessment'] = r['experiment_assessment']
                r['experiment_revoked'] = {'reason': reason.strip(), 'at': time.time()}
                r['experiment_assessment'] = {'status': 'withdrawn'}
                self.db.execute('UPDATE episodes SET result_json=? WHERE owner=? AND id=?', (encoded(r), self.owner, episode))
                self.db.execute('UPDATE cognitive_state SET revision=revision+1 WHERE owner=?', (self.owner,))

    def revoke_development(self, review_id, reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300:
            raise ValueError('development_withdrawal_reason_required')
        from .development import withdraw
        from .consolidation import rebuild_consolidations
        with self.db:
            if withdraw(self, review_id, reason.strip()):
                rebuild_consolidations(self)
                self.db.execute('UPDATE cognitive_state SET revision=revision+1 WHERE owner=?', (self.owner,))

    def annotate_inquiry(self, episode, assessment, receipt):
        """Attach assessment only to the same surviving, already durable observation.

        Keeping I/O committed before the model call preserves evidence on a crash.
        Revocation during assessment prevents a late result from resurrecting it.
        """
        from .inquiry import receipt_matches, validate_assessment
        with self.db:
            row = self.db.execute("SELECT * FROM episodes WHERE owner=? AND id=? AND status='observed' AND revoked=0",
                                  (self.owner, episode)).fetchone()
            if not row:
                return False
            result, decision = json.loads(row['result_json']), json.loads(row['decision_json'])
            if (result.get('receipt') != receipt or not receipt_matches(result) or result.get('inquiry_revoked')
                    or result.get('inquiry_assessment', {}).get('status') == 'assessed'):
                return False
            if assessment.get('status') == 'assessed':
                checked = validate_assessment(assessment, result['text'], decision['inquiry'], receipt)
                if assessment != checked:
                    raise ValueError('assessment_receipt_mismatch')
            result['inquiry_assessment'] = assessment
            self.db.execute("UPDATE episodes SET result_json=? WHERE owner=? AND id=?", (encoded(result), self.owner, episode))
            from .consolidation import rebuild_consolidations
            rebuild_consolidations(self)
            self.db.execute("UPDATE cognitive_state SET revision=revision+1 WHERE owner=?", (self.owner,))
        return True

    def revoke_inquiry(self, episode, reason):
        """Withdraw a fallible interpretation while retaining the observed material."""
        with self.db:
            if self._withdraw_inquiry(episode, reason):
                from .consolidation import rebuild_consolidations
                rebuild_consolidations(self)
                self.db.execute('UPDATE cognitive_state SET revision=revision+1 WHERE owner=?', (self.owner,))

    def _withdraw_inquiry(self, episode, reason):
        """Row update only; the caller owns the transaction and derived rebuild."""
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300:
            raise ValueError('inquiry_revocation_reason_required')
        row = self.db.execute('SELECT result_json FROM episodes WHERE owner=? AND id=?',
                             (self.owner, episode)).fetchone()
        if not row:
            raise KeyError('unknown_episode')
        result = json.loads(row[0] or '{}')
        if not result.get('inquiry_assessment'):
            raise ValueError('episode_has_no_inquiry_assessment')
        if result.get('inquiry_revoked'):
            return False
        result['withdrawn_inquiry_assessment'] = result['inquiry_assessment']
        result['inquiry_revoked'] = {'reason': reason.strip(), 'at': time.time()}
        result['inquiry_assessment'] = {'status': 'withdrawn', 'reason': reason.strip()}
        self.db.execute('UPDATE episodes SET result_json=? WHERE owner=? AND id=?',
                        (encoded(result), self.owner, episode))
        return True

    def begin(self, before, decision):
        episode = uuid4().hex
        with self.db:
            scope, actor = decision.get("_scope", "public"), decision.get("_actor", "")
            self.db.execute("INSERT INTO episodes(owner,id,started,status,before_json,decision_json,scope,actor) VALUES (?,?,?,'started',?,?,?,?)",
                            (self.owner, episode, time.time(), encoded(before), encoded(decision), scope, actor))
            if decision.get("goal"):
                g = decision["goal"]
                self.db.execute("INSERT INTO goals(owner,id,question,need,origin,status,updated,scope,actor) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET status=excluded.status,updated=excluded.updated",
                                (self.owner, g["id"], g["question"], g["need"], g["origin"], g["status"], time.time(), scope, actor))
        return episode

    def finish(self, episode, status, result, next_seconds=60, *, inquiry_withdrawals=()):
        with self.db:
            if inquiry_withdrawals:
                from .inquiry import receipt_matches
                action = self.db.execute("SELECT * FROM episodes WHERE owner=? AND id=? AND status='started'",
                                         (self.owner, episode)).fetchone()
                if not action or action['revoked']:
                    raise RuntimeError('episode_not_in_flight')
                decision = json.loads(action['decision_json'])
                if (decision.get('action') != 'integrate' or status != 'integrated'
                        or result.get('goal_id') != decision.get('integration_goal_id')
                        or not isinstance(inquiry_withdrawals, list) or len(inquiry_withdrawals) != 1
                        or result.get('withdraw_interpretations') != inquiry_withdrawals or not receipt_matches(result)):
                    raise ValueError('invalid_integration_withdrawal_commit')
                for revision in inquiry_withdrawals:
                    target = self.db.execute('SELECT * FROM episodes WHERE owner=? AND id=?',
                                             (self.owner, revision['episode_id'])).fetchone()
                    old = json.loads(target['result_json'] or '{}') if target else {}
                    old_decision = json.loads(target['decision_json']) if target else {}
                    if (not target or target['revoked'] or target['status'] != 'observed'
                            or target['scope'] != action['scope'] or target['actor'] != action['actor']
                            or old_decision.get('action') != 'read'
                            or (old_decision.get('goal') or {}).get('id') != decision.get('integration_goal_id')
                            or old.get('inquiry_revoked') or not receipt_matches(old)
                            or old.get('receipt') != revision['source_receipt']
                            or digest(old.get('inquiry_assessment')) != revision['judgment_version']):
                        raise ValueError('stale_or_out_of_scope_interpretation_revision')
                    self._withdraw_inquiry(revision['episode_id'], 'integration '+episode+': '+revision['reason'])
            changed = self.db.execute("UPDATE episodes SET status=?,finished=?,result_json=? WHERE owner=? AND id=? AND status='started'",
                                      (status, time.time(), encoded(result), self.owner, episode)).rowcount
            if not changed:
                raise RuntimeError("episode_not_in_flight")
            self.db.execute("UPDATE cognitive_state SET revision=revision+1,next_at=? WHERE owner=?",
                            (self.next_after(episode, next_seconds), self.owner))
            self._rebuild_strategies()
            from .consolidation import rebuild_consolidations
            rebuild_consolidations(self)

    def next_after(self, episode, seconds):
        row = self.db.execute("SELECT started,before_json FROM episodes WHERE owner=? AND id=?", (self.owner, episode)).fetchone()
        watermark = json.loads(row["before_json"]).get("social_watermark", row["started"])
        newer = self.db.execute("SELECT 1 FROM social_events WHERE owner=? AND status='pending' AND revoked=0 AND received>? LIMIT 1", (self.owner, watermark)).fetchone()
        return time.time() if newer else time.time() + seconds

    def _rebuild_strategies(self):
        """Abstract executable affordance beliefs from independent tool receipts.

        Textual self-reflections and transport errors never become skill evidence.
        Two URLs/chunks are required; rereading the same source cannot promote.
        """
        groups = {}
        for row in self.db.execute("SELECT * FROM episodes WHERE owner=? AND status='observed' AND revoked=0 AND scope='public' ORDER BY started", (self.owner,)):
            d, r = json.loads(row["decision_json"]), json.loads(row["result_json"])
            if d.get("action") != "read" or r.get("status") != "ok" or not r.get("receipt"):
                continue
            need = d["goal"]["need"]
            key = (need, r["family"])
            groups.setdefault(key, {})[r["source_ref"]] = (row["id"], bool(r["met_need"]))
        self.db.execute("DELETE FROM strategies WHERE owner=?", (self.owner,))
        for (need, family), observations in groups.items():
            items = list(observations.values())
            successes = sum(ok for _, ok in items)
            failures = len(items) - successes
            sid = digest([need, family])[:20]
            self.db.execute("INSERT INTO strategies VALUES (?,?,?,?,?,?,?,?)",
                            (self.owner, sid, need, family, successes, failures,
                             encoded([i for i, _ in items]), "active" if len(items) >= 2 else "candidate"))

    def revoke(self, episode):
        with self.db:
            changed = self.db.execute("UPDATE episodes SET revoked=1 WHERE owner=? AND id=?", (self.owner, episode)).rowcount
            if not changed:
                raise KeyError("unknown_episode")
            # Remove discovery-derived observations and goals from the same
            # source dependency chain, retaining their raw rows for audit.
            invalid = {r[0] for r in self.db.execute('SELECT id FROM episodes WHERE owner=? AND revoked=1', (self.owner,))}
            growing = True
            while growing:
                growing = False
                for row in self.db.execute('SELECT id,decision_json,result_json FROM episodes WHERE owner=? AND revoked=0', (self.owner,)).fetchall():
                    d, r = json.loads(row['decision_json']), json.loads(row['result_json'] or '{}')
                    if (r.get('discovery_episode_id') in invalid or r.get('hypothesis_episode_id') in invalid
                            or r.get('source_episode_id') in invalid or d.get('hypothesis_id') in invalid
                            or (d.get('hypothesis') or {}).get('source_episode_id') in invalid
                            or (d.get('goal') or {}).get('origin') in invalid):
                        self.db.execute('UPDATE episodes SET revoked=1 WHERE owner=? AND id=?', (self.owner, row['id']))
                        invalid.add(row['id']); growing = True
            self._rebuild_strategies()
            from .consolidation import rebuild_consolidations
            rebuild_consolidations(self)
            self.db.execute("UPDATE cognitive_state SET revision=revision+1 WHERE owner=?", (self.owner,))

    def export(self):
        return {table: [dict(r) for r in self.db.execute(f"SELECT * FROM {table} WHERE owner=?", (self.owner,))]
                for table in ["cognitive_state", "goals", "episodes", "strategies", "consolidations", "development_reviews", "call_budget", "social_events", "feedback_policies", "engineering_candidates"]}

    def delete(self):
        with self.db:
            for table in ["cognitive_state", "goals", "episodes", "strategies", "consolidations", "development_reviews", "call_budget", "social_events", "feedback_policies", "engineering_candidates"]:
                self.db.execute(f"DELETE FROM {table} WHERE owner=?", (self.owner,))
