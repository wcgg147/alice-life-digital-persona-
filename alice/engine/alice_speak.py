# -*- coding: utf-8 -*-
"""她在 B 站开口说话（评论）。

跟"动手"分开一个文件：说话和点赞不是一类事。
- 说什么是模型的事（`compose_comment`）；
- 说不说、说几次、说不说得出来，由代码守（日上限、最小间隔、长度、禁 @/链接）；
- 发出去之后就是公开的，收不回来——所以宁可不发，也不发空话。
"""
from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

from engine.alice_actions import _API, _cookies, _get, _post

_MAX_LEN = 60


def enabled() -> bool:
    return str(os.getenv("ALICE_SPEAK_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}


def daily_max() -> int:
    try:
        return int(os.getenv("ALICE_SPEAK_DAILY_MAX", "5") or 5)
    except ValueError:
        return 5


COMMENT_SYSTEM = "\n".join([
    "你刚看完一个 B 站视频，也看了评论区。现在只想留一句你自己的话。",
    "要求：像人在评论区随手打的，不像总结、不像评价视频、不夸 UP 主。",
    "可以只是接一句评论区里的话、一个反驳、一个疑问、一个自己的类似经历。",
    "不要@人、不要放链接、不要说『这个视频』『up主辛苦了』这类客套。",
    "40 字以内，口语。真没什么想说的就只输出 NONE。",
    "只输出评论本身，不要引号、不要解释。",
])


def _clean(text: str) -> str:
    text = " ".join(str(text or "").split())
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\S+", "", text)
    return text.strip().strip('"“”「」')[: _MAX_LEN]


async def compose_comment(title: str, opinion: str, comments_excerpt: str = "") -> str:
    """她想说的一句。返回空串 = 这次不说。"""
    from alice_reading import _llm  # SPLIT-PLAN: alice/life canonical

    user_msg = f"视频：{title}\n你自己的看法：{opinion or '（没什么特别的）'}"
    if comments_excerpt:
        user_msg += f"\n评论区里别人的话（只当参考，别复述）：\n{comments_excerpt[:600]}"
    try:
        raw = await _llm([{"role": "system", "content": COMMENT_SYSTEM},
                          {"role": "user", "content": user_msg}],
                         max_tokens=80, temperature=0.8)
    except Exception:
        return ""
    text = _clean(raw)
    if not text or text.upper().startswith("NONE") or len(text) < 4:
        return ""
    return text


def budget() -> dict[str, Any]:
    """今天还能说几条、还要等多久。评论单独计数，不跟点赞抢额度。"""
    from datetime import datetime, timedelta, timezone

    from engine.alice_actions import _ledger_rows

    now = datetime.now(timezone.utc)
    cst = timezone(timedelta(hours=8))
    used = 0
    last_ts = None
    for row in _ledger_rows(days=2):
        if str(row.get("kind") or "action") != "comment":
            continue
        ts = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.astimezone(cst).date() == now.astimezone(cst).date():
            used += 1
        if last_ts is None or ts > last_ts:
            last_ts = ts
    wait = 0.0
    if last_ts:
        wait = max(0.0, _min_gap_min() - (now - last_ts).total_seconds() / 60.0)
    return {"used_today": used, "left": max(0, daily_max() - used), "wait_min": round(wait, 1)}


def _min_gap_min() -> float:
    try:
        return float(os.getenv("ALICE_SPEAK_MIN_GAP_MIN", "15") or 15)
    except ValueError:
        return 15.0


def record(row: dict[str, Any]) -> None:
    from datetime import datetime, timezone

    from engine.alice_actions import _LEDGER

    row = dict(row)
    row["kind"] = "comment"
    row["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        with open(_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def post_comment(aid: int, message: str) -> dict[str, Any]:
    """发一条评论。type=1 是视频。返回 rpid 以便回查。"""
    try:
        r = _post(f"{_API}/x/v2/reply/add",
                  {"oid": aid, "type": 1, "message": message,
                   "plat": 1, "ordering": "heat"})
        data = r.get("data") or {}
        return {"ok": r.get("code") == 0, "code": r.get("code"),
                "msg": r.get("message"), "rpid": data.get("rpid")}
    except Exception as exc:
        return {"ok": False, "code": -1, "msg": type(exc).__name__, "rpid": None}


def my_recent_comments(mid: int, page_size: int = 5) -> list[dict[str, Any]]:
    """回查她发出去的评论（审核中/已发都会在列表里）。"""
    try:
        d = _get(f"{_API}/x/v2/reply/main?type=1&oid=0&next=0&ps={page_size}&mode=3")
        return d.get("data") or []
    except Exception:
        return []
