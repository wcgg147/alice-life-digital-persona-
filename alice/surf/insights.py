"""看项目情报夹（`qq_project_insights`）：群里冒出来的、对本产品有参考价值的原句。

情报夹归工程，不进 Alice 的人格；每条都带原句、发言人、群、时间、来源消息 ID。
这里只读；`--dismiss <key>` 只改状态（只标不删）。

    py -3.12 tools/alice-surf/insights.py                 # 未处理的
    py -3.12 tools/alice-surf/insights.py --all           # 含已驳回的
    py -3.12 tools/alice-surf/insights.py --dismiss abc123def456
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import time

RUNTIME = pathlib.Path(__file__).resolve().parents[2] / ".runtime" / "qq-social"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dismiss", default="", help="把某条标成 dismissed（不删除）")
    ap.add_argument("--use", default="", help="把某条标成 adopted（已采纳为本项目的一条结论/任务）")
    args = ap.parse_args()

    db = sqlite3.connect(RUNTIME / "channel.sqlite3")
    db.row_factory = sqlite3.Row
    if args.dismiss or args.use:
        key = args.dismiss or args.use
        status = "dismissed" if args.dismiss else "adopted"
        cur = db.execute("UPDATE qq_project_insights SET status=? WHERE insight_key=?", (status, key))
        db.commit()
        print(json.dumps({"key": key, "status": status, "changed": cur.rowcount}, ensure_ascii=False))
        return

    names = {r[0]: r[1] for r in db.execute("SELECT qq_id,name FROM qq_member_names")}
    nick = {r[0]: r[1] for r in db.execute("SELECT qq_id,nickname FROM qq_people")}
    contexts = {r[0]: r[1] for r in db.execute("SELECT scope,name FROM qq_contexts")}
    where = "" if args.all else "WHERE status='observed'"
    rows = db.execute(f"SELECT * FROM qq_project_insights {where} ORDER BY created_at DESC").fetchall()
    print(f"情报 {len(rows)} 条" + ("" if args.all else "（未处理；--all 看全部）"))
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["event_time"])) if r["event_time"] else "时间未知"
        print(json.dumps({
            "key": r["insight_key"], "类型": r["kind"], "状态": r["status"],
            "群": contexts.get(r["scope"], r["scope"]),
            "发言人": names.get(r["speaker_qq"]) or nick.get(r["speaker_qq"]) or r["speaker_qq"],
            "时间": when, "原句": r["quote"], "为什么记": r["why"], "可能动作": r["suggested_action"],
            "来源消息ID": r["source_message_id"],
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
