# -*- coding: utf-8 -*-
"""这个人在这个会话里对某些词的说法（只做词义提示，不做意图判断）。

    py -3.12 tools/user_terms.py list
    py -3.12 tools/user_terms.py add --term a味 --meaning "AI味；指说话像不像真人、像不像客服，不是在问味道。" --source -301728089
    py -3.12 tools/user_terms.py disable --term a味
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alice" / "qq"))

from channel import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / ".runtime" / "qq-social" / "config.json"))
    ap.add_argument("action", choices=["list", "add", "disable"])
    ap.add_argument("--scope", default="")
    ap.add_argument("--term", default="")
    ap.add_argument("--meaning", default="")
    ap.add_argument("--source", default="")
    args = ap.parse_args()

    config_path = pathlib.Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    store = Store(config_path.parent / "channel.sqlite3")
    scope = args.scope or ("private:" + str(config["account_owner_qq"]))

    try:
        if args.action == "list":
            rows = store.db.execute(
                "SELECT scope,term,meaning,source_message_id,status,created_at "
                "FROM qq_user_terms ORDER BY scope,created_at DESC").fetchall()
            print(json.dumps([{"scope": r[0], "term": r[1], "meaning": r[2],
                               "source": r[3], "status": r[4],
                               "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[5] or 0))}
                              for r in rows], ensure_ascii=False, indent=2))
            return
        if not args.term.strip():
            raise SystemExit("--term required")
        if args.action == "disable":
            cur = store.db.execute("UPDATE qq_user_terms SET status='disabled' WHERE scope=? AND term=?",
                                   (scope, args.term.strip()))
        else:
            if not args.meaning.strip():
                raise SystemExit("--meaning required")
            cur = store.db.execute(
                "INSERT INTO qq_user_terms(scope,term,meaning,source_message_id,status,created_at) "
                "VALUES(?,?,?,?,'active',?) ON CONFLICT(scope,term) DO UPDATE SET "
                "meaning=excluded.meaning,source_message_id=excluded.source_message_id,"
                "status='active',created_at=excluded.created_at",
                (scope, args.term.strip(), args.meaning.strip(), args.source.strip(), time.time()))
        store.db.commit()
        print(json.dumps({"scope": scope, "term": args.term, "changed": cur.rowcount,
                          "action": args.action}, ensure_ascii=False))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
