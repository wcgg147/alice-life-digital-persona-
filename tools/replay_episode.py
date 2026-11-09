# -*- coding: utf-8 -*-
"""用真实私聊的一轮做本地回放（E1），不发送 QQ、不写消息表。

    py -3.12 tools/replay_episode.py --user-message-id -301728089
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alice" / "qq"))
sys.path.insert(0, str(ROOT / "alice"))

from channel import Store
from alice import Alice


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / ".runtime" / "qq-social" / "config.json"))
    ap.add_argument("--user-message-id", required=True)
    ap.add_argument("--history", type=int, default=8)
    args = ap.parse_args()

    config_path = pathlib.Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    store = Store(config_path.parent / "channel.sqlite3")
    scope = "private:" + str(config["account_owner_qq"])
    row = store.db.execute(
        "SELECT user_message_id,user_text,alice_text,occurred_at FROM qq_dialogue_episodes "
        "WHERE scope=? AND user_message_id=?", (scope, args.user_message_id)).fetchone()
    if not row:
        raise SystemExit("episode not found")
    mid, user_text, old_reply, at = str(row[0]), str(row[1]), str(row[2]), float(row[3])
    prior = store.db.execute(
        "SELECT user_text,alice_text FROM qq_dialogue_episodes WHERE scope=? AND occurred_at<? "
        "ORDER BY occurred_at DESC LIMIT ?", (scope, at, args.history)).fetchall()
    history = []
    for u, a in reversed(prior):
        history.append({"role": "user", "content": str(u), "at": at - 1})
        history.append({"role": "assistant", "content": str(a), "at": at - 0.5})
    event = {
        "message_type": "private", "self_id": str(config["expected_qq"]),
        "user_id": str(config["account_owner_qq"]), "group_id": scope,
        "message_id": mid, "time": int(at),
        "sender": {"nickname": "", "card": ""},
        "message": [{"type": "text", "data": {"text": user_text}}],
    }
    work = {"event": event, "text": user_text, "key": "replay:" + mid,
            "actor": str(config["account_owner_qq"]), "group": scope,
            "received_at": at, "trigger": "private"}

    async def run() -> None:
        brain = Alice(config, store)
        await brain.connect()
        try:
            _text = user_text.casefold()
            terms = [t for t in store.user_terms(scope)
                     if str(t.get('term') or '').strip().casefold() in _text]
            reply, conv = await brain.reply(work, history)
            print(json.dumps({
                "user_message_id": mid,
                "user": user_text,
                "old_reply": old_reply,
                "new_reply": reply if isinstance(reply, str) else reply.get("text", ""),
                "new_reply_raw": reply,
                "terms_injected": terms,
                "conv": conv,
            }, ensure_ascii=False, indent=2))
        finally:
            await brain.close()
            store.db.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
