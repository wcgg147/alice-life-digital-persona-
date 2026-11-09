"""WSL/Windows cognitive runtime; one owner, one durable ledger, read-only actions."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "alice"), str(ROOT / "alice/life")]

from cognition.state import InstanceLock, Ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, default=ROOT)
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("ALICE_COGNITION_STATE", str(Path.home() / ".local/state/alice-cognition/ledger.sqlite3"))))
    parser.add_argument("--rounds", type=int, default=1, help="0: continuous; default: one opportunity")
    parser.add_argument("--interval", type=float, help="explicit experiment interval, otherwise honor saved next wake")
    parser.add_argument("--daily-cap", type=int, default=0, help="0: no daily ceiling; positive: limit model calls, preserving usage counts")
    parser.add_argument("--export", type=Path)
    parser.add_argument("--delete", action="store_true", help="delete this ledger owner's cognitive data, never persona")
    parser.add_argument("--revoke", help="revoke an episode's support for future learning")
    parser.add_argument("--revoke-inquiry", help="withdraw a question-progress judgment, retaining the observed read")
    parser.add_argument("--revoke-development", help="withdraw a memory review and its dependent goal changes")
    parser.add_argument("--revoke-experiment", help="withdraw trial interpretation while retaining its raw comparisons")
    parser.add_argument("--reason", help="reason for withdrawing an inquiry judgment")
    parser.add_argument("--bridge", action="store_true", help="serve authenticated local QQ cognition bridge")
    parser.add_argument("--stop-file", type=Path, help="shared operator stop path, including Windows supervisor")
    args = parser.parse_args()
    if (args.revoke_inquiry or args.revoke_development or args.revoke_experiment) and not args.reason:
        parser.error('withdrawing a judgment or review requires --reason')
    if sum(bool(x) for x in (args.export, args.delete, args.revoke, args.revoke_inquiry, args.revoke_development, args.revoke_experiment)) > 1:
        parser.error('choose one ledger maintenance action')
    if args.rounds < 0 or (args.interval is not None and args.interval < 0) or not 0 <= args.daily_cap <= 200:
        parser.error("invalid rounds/interval/daily cap")
    with InstanceLock(args.state.with_suffix(".lock")):
        ledger = Ledger(args.state)
        try:
            if args.export:
                args.export.write_text(json.dumps(ledger.export(), ensure_ascii=False, indent=2), encoding="utf-8")
            elif args.delete:
                ledger.delete()
            elif args.revoke:
                ledger.revoke(args.revoke)
            elif args.revoke_inquiry:
                ledger.revoke_inquiry(args.revoke_inquiry, args.reason)
            elif args.revoke_development:
                ledger.revoke_development(args.revoke_development, args.reason)
            elif args.revoke_experiment:
                ledger.revoke_experiment(args.revoke_experiment, args.reason)
            else:
                asyncio.run(run(args, ledger))
        finally:
            ledger.close()


async def run(args, ledger):
    from cognition.environment import LiveModel, ReadingEnvironment, load_runtime
    from cognition.loop import CognitiveLoop
    cfg = load_runtime(args.config_root)
    model = LiveModel(cfg)
    environment = ReadingEnvironment(args.config_root, cfg, ledger)
    runtime = CognitiveLoop(ledger, model, environment, daily_cap=args.daily_cap,
                            stop_path=args.stop_file or args.state.parent / "STOP")
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
    try:
        from cognition.bridge import serve
        config_path = args.config_root / ".runtime/qq-social/config.json"
        manager = serve(args.state, args.config_root / ".runtime/cognition-bridge.json",
                        lambda: json.loads(config_path.read_text(encoding="utf-8-sig"))) if args.bridge else nullcontext()
        with manager:
            await runtime.run(args.rounds, args.interval)
    finally:
        await model.close()


if __name__ == "__main__":
    main()
