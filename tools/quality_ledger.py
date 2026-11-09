# -*- coding: utf-8 -*-
"""私聊质量账本：把「主人和 Alice 的一来一回」变成可复核的质量事件。

只读 `qq_dialogue_episodes`，不改 QQ 运行库、不写人格、不动 processed 状态。
模型只做判断，代码只做边界：逐字校验、去重、条数上限、来源定位。

    py -3.12 tools/quality_ledger.py --config .runtime/qq-social/config.json --days 1 --dry-run
    py -3.12 tools/quality_ledger.py --config .runtime/qq-social/config.json --days 1

结果落在 `.runtime/qq-social/quality/ledger.sqlite3`，不进源码、不进群聊。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
QQ_TOOLS = ROOT / "alice" / "qq"

PROMPT = """你在读主人和 Alice 的真实私聊。任务：找出主人对 Alice 的回复不满意、或者不得不纠正她、重问她、替她补话的瞬间。

只记有证据的：
- 主人明确抱怨（太长、像客服、又在解释、又这句、编造、没接住）；
- 主人下一句在纠正她的理解、指代、事实或态度。例如「你搞错了吧」「你会理解成……」「不是这个意思」；
- 主人问「你什么意思」「哪件事」「你说的是哪个」这种她上一句没说清造成的重问；
- 主人重复同一要求，说明上一轮没做到。

quote_user 必须是这一轮 user 的原话；quote_alice 必须是这一轮 alice 的原话，不能从上下文的上一轮拿。

不要记：
- 正常讨论剧情、观点不同、主人自己在讲自己的事；
- 单纯的话题切换、道别、表情包；
- 没有主人口头证据的「我觉得她可以更好」。

真实例子（同类，不是本次数据）：
1. 她说「他两开始聊婚姻了」，主人回「你搞错了吧，是聊各自的婚姻」——她把人物关系理解错了，记 defect。
2. 她说了一段绕的话，主人回「你说的这件事啥意思」「哪件事」——指代不清，记 defect。
3. 主人回「又开始口头认错了」——她重复同一种回应方式，记 defect。

每条输出：
{"user_message_id":"这一轮主人消息的 id","kind":"defect|taste|oneoff",
 "quote_user":"主人原话，逐字，不超过80字","quote_alice":"Alice 那一句原话，逐字，不超过120字",
 "what_happened":"她具体做了什么","what_he_wanted":"主人实际想要什么",
 "confidence":0.0到1.0}
kind 说明：defect=她做错了（理解/事实/指代/重复/啰嗦/客服腔）；taste=主人明确说希望她以后怎么说话；oneoff=当时情境，不适合改产品。
没有就返回 {"findings":[]}。不要为凑数硬找。只输出 JSON。字符串里不要出现英文双引号；原文里的中文引号照抄（“”「」都可以）。"""


def load_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def connect_db(path: pathlib.Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS findings(
          finding_key TEXT PRIMARY KEY,
          scope TEXT, user_message_id TEXT, kind TEXT,
          quote_user TEXT, quote_alice TEXT,
          what_happened TEXT, what_he_wanted TEXT,
          confidence REAL, quote_verified INTEGER,
          status TEXT DEFAULT 'observed',
          created_at REAL
        );
        CREATE TABLE IF NOT EXISTS runs(
          at REAL, scope TEXT, episodes INTEGER, batches INTEGER,
          findings INTEGER, errors TEXT
        );
        """
    )
    db.commit()


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no json object")
    return json.loads(text[start:end + 1], strict=False)


_STRIP = str.maketrans({c: "" for c in " \t\r\n\u3000“”「」『』\""})


def norm(text: str) -> str:
    return str(text or "").translate(_STRIP)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / ".runtime" / "qq-social" / "config.json"))
    ap.add_argument("--days", type=int, default=7, help="看最近几天；0=全部")
    ap.add_argument("--date", default="", help="只看某一天（本地日期 YYYY-MM-DD）")
    ap.add_argument("--limit", type=int, default=240)
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    config_path = pathlib.Path(args.config).resolve()
    config = load_config(config_path)
    db_path = config_path.parent / "channel.sqlite3"
    out_dir = config_path.parent / "quality"
    out_dir.mkdir(exist_ok=True)

    db = connect_db(db_path)
    out = sqlite3.connect(out_dir / "ledger.sqlite3")
    out.row_factory = sqlite3.Row
    ensure_schema(out)

    scope = "private:" + str(config["account_owner_qq"])
    since = 0 if args.days <= 0 else time.time() - args.days * 86400
    where = "scope=? AND occurred_at>=?"
    params = [scope, since]
    if args.date:
        where += " AND date(occurred_at,'unixepoch','localtime')=?"
        params.append(args.date)
    rows = db.execute(
        "SELECT user_message_id,user_text,alice_text,occurred_at FROM qq_dialogue_episodes "
        f"WHERE {where} ORDER BY occurred_at LIMIT ?",
        (*params, args.limit),
    ).fetchall()
    db.close()

    if not rows:
        print(json.dumps({"scope": scope, "episodes": 0, "note": "no episodes"}, ensure_ascii=False))
        return

    episodes = [
        {"i": i, "id": str(r["user_message_id"]), "at": time.strftime("%m-%d %H:%M", time.localtime(r["occurred_at"])),
         "user": str(r["user_text"] or ""), "alice": str(r["alice_text"] or "")}
        for i, r in enumerate(rows)
    ]

    if args.dry_run:
        print(json.dumps({"scope": scope, "episodes": len(episodes), "batches": (len(episodes) + args.batch - 1) // args.batch,
                          "first": episodes[:2], "last": episodes[-1:]}, ensure_ascii=False, indent=2))
        return

    sys.path.insert(0, str(QQ_TOOLS))
    sys.path.insert(0, str(ROOT / "alice"))
    from alice import Alice
    from channel import Store
    from llm_client import robust_llm_client

    store = Store(db_path)
    brain = Alice(config, store)
    brain.original_client = robust_llm_client

    total = 0
    errors = []
    batches = 0

    async def run() -> None:
        nonlocal total, batches
        try:
            for start in range(0, len(episodes), args.batch):
                chunk = episodes[start:start + args.batch]
                # 给模型前两条上下文，帮助判断「主人是不是在纠正她」；但只允许对 chunk 内的轮次出 finding。
                lo = max(0, start - 2)
                context = episodes[lo:start]
                payload = {"context_before": context, "episodes": chunk}
                try:
                    raw = await brain.completion(
                        [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                        max_tokens=2200, temperature=0.1,
                    )
                    (out_dir / "raw").mkdir(exist_ok=True)
                    (out_dir / "raw" / f"{int(time.time())}-{start // args.batch}.json").write_text(raw, encoding="utf-8")
                    data = parse_json(raw)
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}"[:180])
                    print(json.dumps({"batch": start // args.batch, "error": errors[-1]}, ensure_ascii=False), flush=True)
                    continue
                batches += 1
                by_id = {e["id"]: e for e in chunk}
                for item in (data.get("findings") or [])[:8]:
                    if not isinstance(item, dict):
                        continue
                    e = by_id.get(str(item.get("user_message_id", "")))
                    if not e:
                        continue
                    qu, qa = str(item.get("quote_user") or ""), str(item.get("quote_alice") or "")
                    verified = int(bool(qu and len(norm(qu)) >= 4 and norm(qu) in norm(e["user"])
                                        and (not qa or (len(norm(qa)) >= 4 and norm(qa) in norm(e["alice"])))))
                    if not verified:
                        continue  # 逐字对不上就不要，宁可漏
                    key = f"{e['id']}:{item.get('kind')}"
                    out.execute(
                        "INSERT OR IGNORE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, scope, e["id"], str(item.get("kind") or "defect"), qu, qa,
                         str(item.get("what_happened") or "")[:300], str(item.get("what_he_wanted") or "")[:300],
                         float(item.get("confidence") or 0), verified, "observed", time.time()),
                    )
                    total += 1
                out.commit()
                print(json.dumps({"batch": start // args.batch, "episodes": len(chunk),
                                  "findings": len(data.get("findings") or []), "saved": total}, ensure_ascii=False), flush=True)
        finally:
            out.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                        (time.time(), scope, len(episodes), batches, total, json.dumps(errors, ensure_ascii=False)))
            out.commit()
            store.db.close()

    asyncio.run(run())

    rows = out.execute("SELECT kind,COUNT(*) c FROM findings WHERE scope=? GROUP BY kind", (scope,)).fetchall()
    print(json.dumps({"scope": scope, "episodes": len(episodes), "model_calls": batches,
                      "saved": total, "by_kind": {r["kind"]: r["c"] for r in rows},
                      "ledger": str(out_dir / "ledger.sqlite3"), "errors": errors}, ensure_ascii=False, indent=2))
    out.close()


if __name__ == "__main__":
    main()
