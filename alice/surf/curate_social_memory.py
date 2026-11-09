"""清理存量记忆：把碎句标 noise、把私聊里的「对 AI 的指令」标 exclude。

只标不删——`qq_social_facts.status` 变成 `noise` / `exclude` 后行还在表里，
随时可以改回 `ok`。标记理由写进 `status_note`，时间写进 `marked_at`。

判据交给模型（一条引用能不能撑起「关于这个人」的事实，是语义判断，不是长度阈值），
代码只负责取数、分批、校验它回的 key、落库和统计。

    py -3.12 tools/alice-surf/curate_social_memory.py --dry-run    # 先看模型会怎么标
    py -3.12 tools/alice-surf/curate_social_memory.py              # 落库
    py -3.12 tools/alice-surf/curate_social_memory.py --scope <authorized_user_id>

依赖：QQ 那套代码当前只在 Codex 的隔离工作树里（`tools/qq-social`，未提交）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "qq-social"
QQ_TOOLS = ROOT / "alice" / "qq"

sys.path.insert(0, str(QQ_TOOLS))

PROMPT = (
    "你在清理 QQ 社交记忆表里抽出来的条目。每条都声称「某个群友/好友说过这句话」。逐条判断，只标记，不删除、不改写原文：\n"
    "- noise：不是一句能独立读懂的完整意思（单个词、纯数字或型号、半句话、离开上下文就不知所云）。"
    "这类撑不起一条关于这个人的事实，留着会让 Alice 把碎片当成别人的偏好。\n"
    "- exclude：这句话其实是在对 AI 助手发指令、提要求或评测（「来条语音」「你回太多了」「发个表情包」「你能发表情包吗」），"
    "也不是这个人在讲自己：对它提问、催它、评价它上一句回得好不好，都算这一类。别把这些当成他的偏好。特别注意私聊：句子里带「你」在问对方、要对方做点什么、或评价对方的，主语其实不是说话人自己，一律 exclude。\n"
    "- ok：其余保留。宁愿少标：一句完整、能看懂的话即使很短也算 ok；"
    "如果一句话确实暴露了这个人自己的经历、偏好、打算、身份或关系，也算 ok。\n"
    "只输出 JSON：{\"marks\":[{\"key\":\"条目key\",\"status\":\"ok|noise|exclude\",\"why\":\"不超过20字的理由\"}]}，"
    "必须覆盖给出的每一个 key，一条不少。"
)


def load_facts(db, scope: str, recheck: bool) -> list[dict]:
    where = "marked_at IS NULL" if not recheck else "marked_at IS NOT NULL"
    args: list = []
    if scope:
        where += " AND (scope=? OR scope=?)"
        args = [scope, scope if scope.startswith("private:") else "private:" + scope]
    names = {r[0]: r[1] for r in db.execute("SELECT qq_id,name FROM qq_member_names")}
    nick = {r[0]: r[1] for r in db.execute("SELECT qq_id,nickname FROM qq_people")}
    contexts = {r[0]: (r[1], r[2]) for r in db.execute("SELECT scope,kind,name FROM qq_contexts")}
    out = []
    for key, fact_scope, subject, category, quote in db.execute(
            f"SELECT fact_key,scope,subject_qq,category,quotation FROM qq_social_facts WHERE {where} ORDER BY scope,created_at", args):
        kind, name = contexts.get(fact_scope, ("group" if not fact_scope.startswith("private:") else "private", ""))
        out.append({"key": key, "scope": fact_scope, "where": name or ("私聊 " + subject if kind == "private" else fact_scope),
                    "kind": kind, "speaker": names.get(subject) or nick.get(subject) or subject,
                    "category": category, "quote": quote})
    return out


def parse(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    marks = data.get("marks")
    if not isinstance(marks, list):
        raise ValueError("invalid_curation_response")
    return {str(m.get("key")): (str(m.get("status")), str(m.get("why") or "")[:80])
            for m in marks if isinstance(m, dict) and m.get("status") in {"ok", "noise", "exclude"}}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="", help="只处理某个群/私聊")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recheck", action="store_true", help="重标已标过的条目")
    args = ap.parse_args()

    from channel import Store
    from social_memory import SocialMemory
    from alice import Alice

    config = json.loads((RUNTIME / "config.json").read_text(encoding="utf-8-sig"))
    store = Store(RUNTIME / "channel.sqlite3")
    SocialMemory(config, store)  # 建表/补列
    brain = Alice(config, store)
    facts = load_facts(store.db, args.scope, args.recheck)
    print(json.dumps({"待判断": len(facts)}, ensure_ascii=False), flush=True)
    if not facts:
        store.db.close()
        return

    tally = {"ok": 0, "noise": 0, "exclude": 0}
    missed: list[str] = []
    await brain.connect()
    try:
        for start in range(0, len(facts), args.batch):
            batch = facts[start:start + args.batch]
            try:
                raw = await brain.completion(
                    [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
                    max_tokens=2000, temperature=0.1)
                marks = parse(raw)
            except Exception as exc:
                print(json.dumps({"批次": start // args.batch + 1, "失败": f"{type(exc).__name__}: {exc}"[:200]},
                                 ensure_ascii=False), flush=True)
                continue
            now = time.time()
            for item in batch:
                status, why = marks.get(item["key"], ("", ""))
                if not status:
                    missed.append(item["key"])
                    continue
                tally[status] += 1
                if not args.dry_run:
                    store.db.execute("UPDATE qq_social_facts SET status=?,status_note=?,marked_at=? WHERE fact_key=?",
                                     (status, why, now, item["key"]))
            if not args.dry_run:
                store.db.commit()
            print(json.dumps({"批次": start // args.batch + 1, "条数": len(batch),
                              "标了": len([1 for i in batch if i["key"] in marks]),
                              "本批": {s: sum(1 for k, (v, _) in marks.items()
                                              if v == s and any(i["key"] == k for i in batch)) for s in tally}},
                             ensure_ascii=False), flush=True)
    finally:
        pool = getattr(brain, "pool", None)
        if pool is not None:
            await pool.close()

    total = {r[0]: r[1] for r in store.db.execute("SELECT COALESCE(status,'ok'),COUNT(*) FROM qq_social_facts GROUP BY 1")}
    print(json.dumps({"dry_run": args.dry_run, "本次": tally, "模型漏掉的条数": len(missed),
                      "全表状态": total}, ensure_ascii=False))
    store.db.close()


if __name__ == "__main__":
    asyncio.run(main())
