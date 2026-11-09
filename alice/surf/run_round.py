"""手工让她逛一轮：调用产品现有阅读器，只读不公开互动。

用途：主人明确要求「让她去冲浪」时，从本机跑一轮真实阅读并写入她自己的记忆。
不复制阅读逻辑，只做三件事：关掉公开动作、指定平台、把结果打出来。

    py -3.12 tools/alice-surf/run_round.py --platform qidian [--user <persona_uuid>]
    py -3.12 tools/alice-surf/run_round.py --platform bilibili
    py -3.12 tools/alice-surf/run_round.py --platform fanqie|live|x

注意：`--force` 默认开，表示忽略「4 小时内已读过」的节奏间隔——这是主人手工点的一次，
不是常驻循环；常驻循环走 engine/alice_loop.py，不要用这个脚本。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "alice"  # 数字人项目包根（memory/engine/agent/llm_client/rag）
ALICE_LIFE = ROOT / "alice" / "life"
ALICE_CFG = ROOT / "alice" / "config"
sys.path.insert(0, str(API))
sys.path.insert(0, str(ALICE_LIFE))
sys.path.insert(0, str(ALICE_CFG))
import config as product_config

DEFAULT_USER = product_config.ALICE_TEST_USER_ID

# 只读约束：任何平台的公开动作都在模型调用之前就关掉，不给她"投一票/发一条"的机会。
for _flag in (
    "ALICE_INTERACT_ENABLED",
    "ALICE_SPEAK_ENABLED",
    "ALICE_X_INTERACT_ENABLED",
    "ALICE_X_SPEAK_ENABLED",
):
    os.environ[_flag] = "false"
os.environ["ALICE_NOVEL_INTERACT_ENABLED"] = "false"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=["qidian", "fanqie", "bilibili", "live", "x"])
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--force", dest="force", action="store_true", default=True)
    args = ap.parse_args()

    import config as product_config

    # alice_novel 是从 config 读开关的，环境变量不够，必须同时改内存里的属性。
    for attr in ("ALICE_INTERACT_ENABLED", "ALICE_SPEAK_ENABLED", "ALICE_X_INTERACT_ENABLED",
                 "ALICE_X_SPEAK_ENABLED", "ALICE_NOVEL_INTERACT_ENABLED"):
        if hasattr(product_config, attr):
            setattr(product_config, attr, False)

    from memory.l2_postgres import L2PostgresMemory

    l2 = L2PostgresMemory()
    try:
        if args.platform in ("qidian", "fanqie"):
            import alice_novel
            result = await alice_novel.run_novel_reading_round(
                l2, args.user, platform=args.platform, force=args.force
            )
        elif args.platform == "bilibili":
            import alice_reading
            result = await alice_reading.run_once(l2, args.user, force=args.force)
        elif args.platform == "live":
            import alice_bili_live
            result = await alice_bili_live.run_live_round(l2, args.user, force=args.force)
        else:
            import alice_x
            result = await alice_x.run_once(l2, args.user, force=args.force)
    finally:
        pool = getattr(l2, "_pool", None)
        if pool is not None:
            await pool.close()

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
