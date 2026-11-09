"""给每个群/好友写一张印象卡（`qq_group_profiles`）——一个 scope 一次模型调用。

按真实使用面来：配置里开着的每个群、每个私聊都写，不是抽样挑几个。
材料是本机 QQ 桥已记录的原始发言（`qq_observed_messages`），代码先算出谁说得最多、
一天里什么时候人多，再交给模型写「聊什么 / 什么性质 / 谁靠谱 / 她的印象」。
只读 + 写这一张表，不改任何人的事实条目，也不发言。

    py -3.12 tools/alice-surf/profile_groups.py            # 所有开着的 scope
    py -3.12 tools/alice-surf/profile_groups.py --scope 1103411350
    py -3.12 tools/alice-surf/profile_groups.py --list     # 只看有哪些 scope、有没有卡

依赖：QQ 那套代码当前只在 Codex 的隔离工作树里（`tools/qq-social`，未提交）。
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


def scopes_from(config: dict, db) -> list[str]:
    known = [str(g) for g in config.get("allowed_groups", [])] + \
            ["private:" + str(u) for u in config.get("allowed_private_users", [])]
    present = [r[0] for r in db.execute("SELECT scope FROM qq_contexts")]
    ordered = [s for s in known if s in present] + [s for s in present if s not in known]
    return ordered


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="", help="只写这一个群/私聊（群号或 private:QQ）")
    ap.add_argument("--list", action="store_true", help="只列 scope 和现有印象卡，不调模型")
    ap.add_argument("--calls", type=int, default=12, help="本次运行内放宽每日整理次数上限（只改内存配置）")
    args = ap.parse_args()

    from channel import Store
    from social_memory import SocialMemory
    from alice import Alice

    config = json.loads((RUNTIME / "config.json").read_text(encoding="utf-8-sig"))
    config["social_memory_calls_per_day"] = max(int(config.get("social_memory_calls_per_day", 6)), args.calls)
    store = Store(RUNTIME / "channel.sqlite3")
    memory = SocialMemory(config, store)

    scopes = [args.scope] if args.scope else scopes_from(config, store.db)
    cards = {r[0]: r[1:] for r in store.db.execute(
        "SELECT scope,profile_type,summary,updated_at FROM qq_group_profiles")}

    if args.list:
        for scope in scopes:
            card = cards.get(scope)
            print(json.dumps({"scope": scope, "有卡": bool(card),
                              "类型": card[0] if card else None,
                              "摘要": card[1] if card else None,
                              "更新时间": time.strftime("%m-%d %H:%M", time.localtime(card[2])) if card else None},
                             ensure_ascii=False))
        store.db.close()
        return

    brain = Alice(config, store)

    async def run() -> None:
        await brain.connect()
        try:
            for scope in scopes:
                payload = memory.profile_payload(scope)
                if not payload or not payload.get("messages"):
                    print(json.dumps({"scope": scope, "跳过": "没有可用的发言"}, ensure_ascii=False), flush=True)
                    continue
                try:
                    result = await memory.refresh_profile(brain.completion, scope)
                except Exception as exc:
                    print(json.dumps({"scope": scope, "失败": f"{type(exc).__name__}: {exc}"[:200]},
                                     ensure_ascii=False), flush=True)
                    continue
                print(json.dumps({**result, "材料条数": payload["message_total"],
                                  "采样": result.get("sample_messages")}, ensure_ascii=False), flush=True)
        finally:
            pool = getattr(brain, "pool", None)
            if pool is not None:
                await pool.close()
            store.db.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
