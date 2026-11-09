"""给她排一个本机小班次：按产品自己的节奏轮一轮，读到的存进她的记忆。

和 run_round.py 的分工：
- run_round.py：主人点一下，指定平台，忽略间隔（手工）。
- shift.py：本机挂着跑几轮，交给 engine/alice_loop 自己挑平台、自己守各站间隔（自然节奏）。

    py -3.12 tools/alice-surf/shift.py --rounds 3 --gap-min 40

只读约束与 run_round.py 相同：公开动作（投币/评论/发言）在模型调用前就关掉。
结果追加到 .runtime/alice-surf/shift-YYYYMMDD.jsonl，跑完打印摘要。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "alice"  # 数字人项目包根（memory/engine/agent/llm_client/rag）
ALICE_LIFE = ROOT / "alice" / "life"
ALICE_CFG = ROOT / "alice" / "config"
sys.path.insert(0, str(API))
sys.path.insert(0, str(ALICE_LIFE))
sys.path.insert(0, str(ALICE_CFG))
import config as product_config
OUT = ROOT / ".runtime" / "alice-surf"

DEFAULT_USER = product_config.ALICE_TEST_USER_ID

for _flag in ("ALICE_INTERACT_ENABLED", "ALICE_SPEAK_ENABLED",
              "ALICE_X_INTERACT_ENABLED", "ALICE_X_SPEAK_ENABLED"):
    os.environ[_flag] = "false"
os.environ["ALICE_NOVEL_INTERACT_ENABLED"] = "false"
# X 没有代理时只会 28 秒超时并把整轮拖死（实测 2026-10-30 02:25），默认跳过；有代理再 --with-x。
os.environ.setdefault("ALICE_X_ENABLED", "false")


def _gate_off(keep_x: bool = False) -> None:
    # .env.local 用 override=True 把 ALICE_X_ENABLED 固定成 true，改环境变量没用，
    # 必须在 import config 之后改模块属性（engine.alice_x 是 `from config import ...` 现读的）。
    import config as product_config
    for attr in ("ALICE_INTERACT_ENABLED", "ALICE_SPEAK_ENABLED", "ALICE_X_INTERACT_ENABLED",
                 "ALICE_X_SPEAK_ENABLED", "ALICE_NOVEL_INTERACT_ENABLED"):
        if hasattr(product_config, attr):
            setattr(product_config, attr, False)
    if not keep_x:
        product_config.ALICE_X_ENABLED = False
    # 2026-10-30 02:38 实测：alice_loop 的「实践轮」会绕过上面那些开关——
    # acts.apply() 和 speak.post_comment() 都不看 enabled()，结果她真点赞、真投币、
    # 真发了一条评论。班次里必须把这两条出口本身换掉。
    os.environ["ALICE_LOOP_PRACTICE_EVERY"] = "1000000"  # choose_kind 永远选「读」
    from engine import alice_actions, alice_speak
    alice_actions.apply = lambda *a, **k: {}
    alice_speak.post_comment = lambda *a, **k: {"ok": False, "disabled_by_shift": True}


async def one_round(user_id: str) -> dict:
    from memory.l2_postgres import L2PostgresMemory
    import alice_loop

    l2 = L2PostgresMemory()
    try:
        return await alice_loop.run_round(l2, user_id, force=False)
    finally:
        pool = getattr(l2, "_pool", None)
        if pool is not None:
            await pool.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--gap-min", type=float, default=40)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--with-x", action="store_true", help="本机有能出海的代理时才开；否则 X 只会超时")
    args = ap.parse_args()

    if args.with_x:
        os.environ["ALICE_X_ENABLED"] = "true"

    _gate_off(keep_x=args.with_x)
    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / f"shift-{time.strftime('%Y%m%d')}.jsonl"

    for i in range(args.rounds):
        started = time.time()
        try:
            result = asyncio.run(one_round(args.user))
            record = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "round": i + 1,
                      "platform": result.get("platform"), "stage": result.get("stage"),
                      "chosen": result.get("chosen"), "body_chars": result.get("body_chars"),
                      "ok": result.get("ok")}
        except Exception as exc:  # 网络/站点异常不中断班次
            record = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "round": i + 1,
                      "error": f"{type(exc).__name__}: {exc}"[:300]}
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

        if i + 1 < args.rounds:
            remain = args.gap_min * 60 - (time.time() - started)
            if remain > 0:
                time.sleep(remain)


if __name__ == "__main__":
    main()
