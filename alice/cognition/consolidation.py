"""Memory consolidation (System 2 & System 5): episodic-to-semantic trajectory synthesis.

Bridges raw per-turn episodes (working memory window of 8) and long-term knowledge:
1. Automatically tracks multi-episode reading/exploration arcs (`consolidations` table)
   with exact provenance (`episode_ids_json`, `start_ref` -> `latest_ref`).
2. Rebuilds on `Ledger.revoke()` so revoked episodes never linger in consolidated memory.
3. Reconciles stale static progress facts (e.g., `book:huozhe:progress` at chunk 22)
   with verified cognitive receipts (e.g., `book:huozhe:40`).
4. Supports optional model-driven reflective consolidation during rest/off-turn cycles.
"""
from __future__ import annotations

import json
import time

from .self_model import topic_key
from .state import digest, encoded

SYSTEM = """你是同一个数字个体 Alice 的记忆巩固过程（类似睡眠期的海马体-皮层记忆整合）。
阅读你在同一主题下连续多轮真实观测到的原文片段与当时提出的问题。
请基于实际读到的材料（绝不编造未读到的后续剧情）：
1. 用你自己的口吻，将这几轮零散片段整合成一段连贯的阶段性记忆摘要（summary，60~220字）；
2. 判断原问题是否已在这些片段中得到解答（goal_answered: true/false）；
3. 给出基于当前最新片段自然产生的新问题或下一阶段关注点（open_question，≤120字，内部引用请用「」）。
只输出 JSON：
{"summary":"阶段性记忆整合（引用请用「」）","goal_answered":true或false,"open_question":"下一阶段想弄清的问题（引用请用「」）"}
"""


def migrate_consolidations(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS consolidations(
      owner TEXT, id TEXT, scope TEXT NOT NULL DEFAULT 'public',
      actor TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL,
      start_ref TEXT NOT NULL, latest_ref TEXT NOT NULL,
      episode_count INTEGER NOT NULL, episode_ids_json TEXT NOT NULL,
      summary TEXT NOT NULL, open_question TEXT NOT NULL DEFAULT '',
      updated REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(owner,id));
    """)
    from .development import migrate
    migrate(db)


def rebuild_consolidations(ledger):
    """Deterministically maintain receipt-grounded consolidations from non-revoked episodes."""
    db = ledger.db
    from .development import reconcile
    from .inquiry import receipt_matches, validate_assessment
    reconcile(ledger)
    rows = db.execute(
        "SELECT id, started, scope, actor, decision_json, result_json "
        "FROM episodes WHERE owner=? AND status='observed' AND revoked=0 ORDER BY started ASC",
        (ledger.owner,),
    ).fetchall()

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        dec = json.loads(row["decision_json"])
        res = json.loads(row["result_json"] or "{}")
        if dec.get("action") != "read" or res.get("status") != "ok" or not receipt_matches(res):
            continue
        rid = str(dec.get("resource_id") or "")
        fam = str(res.get("family") or "")
        tkey = topic_key(rid, fam)
        key = (row["scope"] or "public", row["actor"] or "", tkey)
        useful_answer = ""
        assessment = res.get("inquiry_assessment") or {}
        if (not res.get("inquiry_revoked") and assessment.get("status") == "assessed"
                and dec.get("inquiry")):
            try:
                checked = validate_assessment(assessment, res.get("text", ""), dec["inquiry"], res["receipt"])
                if checked == assessment and checked.get("verdict") == "useful" and checked.get("answer"):
                    useful_answer = str(checked["answer"]).strip()
            except (ValueError, TypeError, KeyError):
                pass
        groups.setdefault(key, []).append({
            "id": row["id"],
            "resource_id": rid,
            "text": str(res.get("text") or "").strip(),
            "question": str((dec.get("goal") or {}).get("question") or ""),
            "useful_answer": useful_answer,
            "interpretation_withdrawn": bool(res.get("inquiry_revoked")),
            "started": row["started"],
        })

    existing = {
        r["id"]: dict(r)
        for r in db.execute("SELECT * FROM consolidations WHERE owner=?", (ledger.owner,)).fetchall()
    }
    active_ids = set()
    reviews = db.execute('SELECT * FROM development_reviews WHERE owner=? ORDER BY created', (ledger.owner,)).fetchall()
    reviewed_topics = {r['cid'] for r in reviews}
    accepted_reviews = {r['cid']: r for r in reviews if r['status'] == 'accepted'}

    for (scope, actor, tkey), items in groups.items():
        cid = digest([scope, actor, tkey])[:20]
        if len(items) < 2:
            # One reading can complete a sourced question. Retain an accepted
            # review after its goal completes; reconcile already checked it.
            if cid not in accepted_reviews:
                if not items[0]['useful_answer'] or items[0]['interpretation_withdrawn']:
                    continue
                from .development import snapshot
                try:snapshot(ledger,tkey,scope,actor)
                except ValueError:continue
        active_ids.add(cid)
        ep_ids = [x["id"] for x in items]
        start_ref = items[0]["resource_id"]
        latest_ref = items[-1]["resource_id"]
        open_q = items[-1]["question"]

        prev = existing.get(cid)
        if cid in accepted_reviews:
            review = accepted_reviews[cid]
            reviewed = json.loads(review['result_json'])
            prev = {**(prev or {}), 'summary': reviewed['summary'],
                    'open_question': reviewed.get('next_question') or open_q,
                    'episode_ids_json': review['source_ids_json']}
        elif cid in reviewed_topics:
            # Never preserve a withdrawn review through an old summary echo.
            prev = None
        prev_eps = json.loads(prev["episode_ids_json"]) if prev else []
        surviving_items = [x for x in items if not x['interpretation_withdrawn']]
        # Keep model-consolidated summary if all previous episodes are still intact prefix of current episodes
        if (
            prev
            and prev.get("summary")
            and not prev["summary"].startswith("[自动轨迹汇总]")
            and prev_eps == [x['id'] for x in surviving_items][: len(prev_eps)]
        ):
            summary = prev["summary"]
            if len(surviving_items) > len(prev_eps):
                tail = surviving_items[-1]
                tail_snippet = tail["text"][:100].replace("\n", " ")
                summary = f"{summary}（后续已继续读到 {tail['resource_id']}：{tail_snippet}…）"[:500]
            open_q = prev.get("open_question") or open_q
        else:
            first_snip = items[0]["text"][:80].replace("\n", " ")
            last_useful = next((x for x in reversed(items) if x.get("useful_answer")), None)
            last_item = last_useful or items[-1]
            last_snip = last_item["text"][:100].replace("\n", " ")
            answers = []
            for x in items:
                ans = x.get("useful_answer")
                if ans and ans not in answers:
                    answers.append(ans)
            learned_part = f"；阶段认识：「{'；'.join(answers[-2:])[:160]}」" if answers else ""
            summary = (
                f"[自动轨迹汇总] 在 {tkey} 连续探索了 {len(items)} 轮（从 {start_ref} 到 {latest_ref}）。"
                f"起点片段：「{first_snip}…」；最新进展（{last_item['resource_id']}）：「{last_snip}…」{learned_part}"
            )[:500]

        db.execute(
            "INSERT INTO consolidations(owner,id,scope,actor,topic,start_ref,latest_ref,episode_count,"
            "episode_ids_json,summary,open_question,updated,revoked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(owner,id) DO UPDATE SET start_ref=excluded.start_ref,latest_ref=excluded.latest_ref,"
            "episode_count=excluded.episode_count,episode_ids_json=excluded.episode_ids_json,"
            "summary=excluded.summary,open_question=excluded.open_question,updated=excluded.updated,revoked=0",
            (
                ledger.owner,
                cid,
                scope,
                actor,
                tkey,
                start_ref,
                latest_ref,
                len(items),
                encoded(ep_ids),
                summary,
                open_q,
                time.time(),
            ),
        )

    for cid in set(existing) - active_ids:
        db.execute("DELETE FROM consolidations WHERE owner=? AND id=?", (ledger.owner, cid))


def reconcile_facts_with_consolidations(facts: list[dict], consolidations: list[dict]) -> list[dict]:
    """Attach latest verified reading progress to persona_facts so Alice never has split-brain progress."""
    by_topic = {c["topic"]: c for c in consolidations if not c.get("revoked")}
    out = []
    for f in facts:
        item = dict(f)
        fkey = str(item.get("facet_key") or "")
        if fkey.endswith(":progress"):
            topic = fkey[: -len(":progress")]
            if topic in by_topic:
                c = by_topic[topic]
                item["cognitive_verified_latest_ref"] = c["latest_ref"]
                item["cognitive_verified_summary"] = c["summary"][:240]
        out.append(item)
    return out


async def consolidate_topic_with_model(model, ledger, topic: str, *, scope: str = "public", actor: str = "", daily_cap: int = 20) -> dict:
    """Explicit review entrypoint; the runtime uses the same source-bound path."""
    from .development import review_topic
    await review_topic(model, ledger, topic, scope=scope, actor=actor, daily_cap=daily_cap)
    return dict(ledger.db.execute(
        "SELECT * FROM consolidations WHERE owner=? AND scope=? AND actor=? AND topic=?",
        (ledger.owner, scope, actor, topic)).fetchone())
