"""让她逛群聊：把已记录的群发言整理成「谁说过什么」，不进群、不发言。

数据来源是本机 QQ 桥记录的 `qq_observed_messages`（不需要 QQ 在线，也不需要桥在跑）。
整理由产品自己的 `SocialMemory.consolidate` 做：每次最多 40 条、只接受逐字可定位的原文、
默认每天 6 次。这里只在主人明确要求时多推几批。

    py -3.12 tools/alice-surf/visit_groups.py --batches 3
    py -3.12 tools/alice-surf/visit_groups.py --batches 1 --scope 1103411350

依赖：QQ 那套代码当前只在 Codex 的隔离工作树里（`tools/qq-social`，未提交）。
Codex 收树/合并后要把 QQ_TOOLS 指到新位置。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "qq-social"
QQ_TOOLS = ROOT / "alice" / "qq"

sys.path.insert(0, str(QQ_TOOLS))


def pending_by_scope(db) -> list[tuple[str, int]]:
    return db.execute(
        "SELECT scope, COUNT(*) FROM qq_observed_messages "
        "WHERE processed=0 AND role='person' GROUP BY scope ORDER BY 2 DESC"
    ).fetchall()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", type=int, default=3, help="本次最多整理几批（每批 40 条）")
    ap.add_argument("--scope", default="", help="只逛某个群/私聊的 scope；留空按待整理条数从多到少逐个群轮着逛")
    ap.add_argument("--daily-cap", type=int, default=12,
                    help="本次运行内放宽每日整理次数（只改内存里的 config，不写回 config.json）")
    args = ap.parse_args()

    from channel import Store
    from social_memory import SocialMemory
    from alice import Alice

    config = json.loads((RUNTIME / "config.json").read_text(encoding="utf-8-sig"))
    # 只影响本次运行：一次整理 = 一次模型调用，回填积压时 6 次/天明显不够。
    config["social_memory_calls_per_day"] = max(int(config.get("social_memory_calls_per_day", 6)), args.daily_cap)
    store = Store(RUNTIME / "channel.sqlite3")
    memory = SocialMemory(config, store)
    brain = Alice(config, store)

    print("待整理:", pending_by_scope(store.db))
    await brain.connect()

    async def completion(messages, **kwargs):
        return await brain.completion(messages, **kwargs)

    names = {scope: name for scope, _kind, name in
             store.db.execute("SELECT scope, kind, name FROM qq_contexts")}

    done = 0
    visited: list[str] = []
    for _ in range(max(1, args.batches)):
        if args.scope:
            scope = args.scope
        else:
            # 轮流逛：同一个群里待整理的永远最多，不能一直只挑它。
            rows = [(s, n) for s, n in pending_by_scope(store.db) if s not in visited and n > 0]
            if not rows:
                rows = pending_by_scope(store.db)
                visited.clear()
            if not rows:
                print("没有待整理的新发言了")
                break
            scope = rows[0][0]
            visited.append(scope)
        try:
            result = await memory.consolidate(completion, focus_scope=scope, immediate=True)
        except Exception as exc:
            print(f"[{scope}] 失败 {type(exc).__name__}: {exc}"[:300], flush=True)
            break
        if not result:
            print(f"[{scope}] 这一批没有可整理的（可能已整理过或额度用完）", flush=True)
            break
        done += 1
        print(json.dumps({"scope": scope, "群": names.get(scope, ""), **result}, ensure_ascii=False), flush=True)

    total = store.db.execute("SELECT COUNT(*) FROM qq_social_facts").fetchone()[0]
    print(json.dumps({"整理批数": done, "累计条目": total,
                      "时间": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False))
    pool = getattr(brain, "pool", None)
    if pool is not None:
        await pool.close()
    store.db.close()


if __name__ == "__main__":
    asyncio.run(main())
