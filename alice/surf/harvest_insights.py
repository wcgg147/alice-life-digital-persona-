"""回填项目情报夹：把已记录的历史发言再过一遍，只抽「对本产品有参考价值」的原句。

翻群（`SocialMemory.consolidate`）会顺手抽情报，但一天只跑几次、每次 40 条，
积压几千条时情报夹会长期是空的。这个脚本按群回填，只看历史、不改 processed 状态、
不动任何人的人格事实；每条都必须逐字来自原消息（校验在 `save_insight` 里）。

    py -3.12 tools/alice-surf/harvest_insights.py --list
    py -3.12 alice/surf/harvest_insights.py --scope <authorized_group_id>
    py -3.12 tools/alice-surf/harvest_insights.py --limit 400        # 每个群最多看多少条历史

私聊不抽：主人自己的话归反馈采集（community.py 的私有队列），不混进这份公开情报夹。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "qq-social"
QQ_TOOLS = ROOT / "alice" / "qq"

sys.path.insert(0, str(QQ_TOOLS))

PROMPT = (
    "从这些 QQ 群聊天里挑出「对 Alice 这个 QQ 助手产品有参考价值」的原句。判断标准是这句话说的对象是不是她："
    "回复方式、语音、表情包、收发消息、记忆、人格、接入与工具能力，或者做这件事的具体工程做法。\n"
    "群友吐槽他们正在玩的游戏（抽卡、版本、匹配、段位）、硬件价格、八卦与情绪，一律不要。\n"
    "只输出 JSON：{\"insights\":[{\"source_id\":\"原消息ID\",\"kind\":\"defect|preference|capability\","
    "\"quote\":\"逐字原文\",\"why\":\"一句为什么值得记\",\"action\":\"一句可能的动作\"}]}。"
    "quote 必须逐字来自对应消息，不能改写或拼接；没有就返回空数组，不要为凑数硬找。"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="", help="只回填这个群号；留空=所有群")
    ap.add_argument("--limit", type=int, default=300, help="每个群最多看多少条历史")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--list", action="store_true", help="只列各群条数和现有情报数")
    args = ap.parse_args()

    from channel import Store
    from social_memory import SocialMemory
    from alice import Alice

    config = json.loads((RUNTIME / "config.json").read_text(encoding="utf-8-sig"))
    store = Store(RUNTIME / "channel.sqlite3")
    memory = SocialMemory(config, store)

    scopes = [s for s in (str(g) for g in config.get("allowed_groups", [])) if not s.startswith("private:")]
    if args.scope:
        scopes = [args.scope]

    if args.list:
        for scope in scopes:
            n = store.db.execute("SELECT COUNT(*) FROM qq_observed_messages WHERE scope=? AND role='person'", (scope,)).fetchone()[0]
            got = store.db.execute("SELECT COUNT(*) FROM qq_project_insights WHERE scope=? AND status='observed'", (scope,)).fetchone()[0]
            name = (store.db.execute("SELECT name FROM qq_contexts WHERE scope=?", (scope,)).fetchone() or [""])[0]
            print(json.dumps({"scope": scope, "群": name, "已记录发言": n, "现有情报": got}, ensure_ascii=False))
        store.db.close()
        return

    brain = Alice(config, store)

    async def run() -> None:
        await brain.connect()
        added = 0
        try:
            for scope in scopes:
                rows = store.db.execute(
                    "SELECT id,message_id,actor,content FROM qq_observed_messages "
                    "WHERE scope=? AND role='person' ORDER BY id DESC LIMIT ?", (scope, args.limit)).fetchall()
                rows = [r for r in rows if len(r[3].strip()) >= 6][::-1]
                async def ask(chunk: list) -> tuple:
                    """返回 (新增条数, 失败原因)。输出被截断时对半拆开重试一次，别整批丢掉。"""
                    try:
                        raw = await brain.completion(
                            [{"role": "system", "content": PROMPT},
                             {"role": "user", "content": json.dumps(
                                 {"scope": scope, "messages": [{"source_id": r[1], "speaker_qq": r[2], "text": r[3]} for r in chunk]},
                                 ensure_ascii=False)}], max_tokens=3000, temperature=0.2)
                    except Exception as exc:
                        return 0, f"{type(exc).__name__}: {exc}"[:160]
                    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                    try:
                        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
                    except ValueError:
                        if len(chunk) > 8:
                            half = len(chunk) // 2
                            left, err = await ask(chunk[:half])
                            if err:
                                return left, err
                            right, err = await ask(chunk[half:])
                            return left + right, err
                        return 0, "回答被截断且无法再拆"
                    sources = {str(r[1]): r for r in chunk}
                    saved = 0
                    for item in (data.get("insights") or [])[:6]:
                        if not isinstance(item, dict):
                            continue
                        source = sources.get(str(item.get("source_id", "")))
                        if source and memory.save_insight(scope, source, item.get("kind"), item.get("quote"),
                                                          item.get("why"), item.get("action")):
                            saved += 1
                    store.db.commit()
                    return saved, ""

                for start in range(0, len(rows), args.batch):
                    chunk = rows[start:start + args.batch]
                    saved, err = await ask(chunk)
                    if err:
                        print(json.dumps({"scope": scope, "跳过": err, "条数": len(chunk)}, ensure_ascii=False), flush=True)
                        continue
                    added += saved
                    print(json.dumps({"scope": scope, "看过": len(chunk), "新增情报": saved}, ensure_ascii=False), flush=True)
        finally:
            pool = getattr(brain, "pool", None)
            if pool is not None:
                await pool.close()
            store.db.close()
        print(json.dumps({"本次新增": added}, ensure_ascii=False))

    asyncio.run(run())


if __name__ == "__main__":
    main()
