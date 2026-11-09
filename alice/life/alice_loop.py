# -*- coding: utf-8 -*-
"""她的一天：读 → 做 → 记。回合调度。

为什么要有这一层：
`run_once` 一次把"读+写看法+存记忆+顺手动手"做完，它是一轮**消费**。
但近期她要的是"读完之后真的去做点什么"——回到读过的东西上点赞、收藏、
评论。这件事需要：记得住那条内容（`ref`）、有预算、别在同一件事上反复做。
这些是**调度**的事，不该塞进 `run_once`。

分工（跟整条线一致）：
- 代码定：回合配比、每日预算、同一件事不重复做、被限流就歇；
- 她自己定：读什么、动不动手、说什么话。
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STATE_DIR = _ROOT / ".runtime" / "alice-loop"
_JOURNAL = _STATE_DIR / "journal.jsonl"


def enabled() -> bool:
    return str(os.getenv("ALICE_LOOP_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}


def _practice_every() -> int:
    """读几轮，插一轮实践。默认 4 → 读 3 做 1。"""
    try:
        return max(1, int(os.getenv("ALICE_LOOP_PRACTICE_EVERY", "4") or 4))
    except ValueError:
        return 4


def _state_path(user_id: str) -> pathlib.Path:
    return _STATE_DIR / f"{user_id}.json"


def load_state(user_id: str) -> dict[str, Any]:
    p = _state_path(user_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(user_id: str, state: dict[str, Any]) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state_path(user_id).write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def _roll_day(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("day") != _today():
        state["day"] = _today()
        state["today"] = {"read": 0, "practice": 0, "rest": 0}
    state.setdefault("today", {"read": 0, "practice": 0, "rest": 0})
    return state


def choose_kind(state: dict[str, Any]) -> str:
    """这一轮是读还是做。纯计数，不看内容。"""
    every = _practice_every()
    done = int(state.get("rounds") or 0)
    # 每 every 轮里插一轮实践（第 every 轮做）
    if done > 0 and done % every == every - 1:
        return "practice"
    return "read"


def journal(row: dict[str, Any]) -> None:
    row = dict(row)
    row["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_JOURNAL, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _is_x_item(item: dict[str, Any]) -> bool:
    src = str((item or {}).get("source") or "").lower()
    ref = str((item or {}).get("ref") or "").lower()
    return src == "x" or "x.com/" in ref or "twitter.com/" in ref


def _is_bili_item(item: dict[str, Any]) -> bool:
    ref = str((item or {}).get("ref") or "")
    if "live.bilibili.com/" in ref:
        return False  # 直播间不是视频：没有 bvid，不能送进视频点赞/投币接口
    return "bilibili.com/" in ref or ref.startswith("BV")


async def _practice_round(l2, user_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """回到她读过的一条上，真去做点什么（严格按平台分流：B站走 alice_actions，X 走 alice_x）。"""
    from engine import alice_actions as acts
    from engine import alice_speak as speak
    import alice_x

    acted = set(state.get("acted") or [])
    try:
        items = await l2.list_life_items(user_id, limit=8)
    except Exception:
        items = []

    x_can_act = alice_x.is_configured() and (alice_x.interact_enabled() or alice_x.speak_enabled())
    pool = [
        x for x in items
        if str(x.get("kind")) == "reading"
        and str(x.get("item_id") or "") not in acted
        and (_is_bili_item(x) or (_is_x_item(x) and x_can_act))
    ]
    if not pool:
        return {"stage": "practice_no_target"}

    # 优先挑还没动过手的内容
    pool.sort(key=lambda x: 0 if str(x.get("actions_json") or "{}") in ("", "{}") else 1)
    target = pool[0]
    ref = str(target.get("ref") or "")
    title = str(target.get("title") or target.get("text") or "")
    view_memo = str(target.get("detail") or target.get("text") or "（你之前看过、现在还留着的一条）")

    if _is_x_item(target):
        x_res = await alice_x.maybe_interact_on_x(l2, user_id, target, practice=True)
        acted.add(str(target.get("item_id") or ""))
        state["acted"] = sorted(acted)[-200:]
        return {
            "stage": "practice",
            "platform": "x",
            "item_id": target.get("item_id"),
            "title": title[:40],
            "action": x_res.get("actions"),
            "comment": x_res.get("reply"),
            "skipped": x_res.get("skipped"),
        }

    bvid = ref.rsplit("/", 1)[-1] if "/" in ref else ref
    out: dict[str, Any] = {"stage": "practice", "platform": "bilibili",
                           "item_id": target.get("item_id"), "title": title[:40]}

    # 动手：赞/藏/关注（预算由 alice_actions 自己守）
    budget = acts.budget()
    if budget["left"] > 0 and budget["wait_min"] <= 0:
        decision = await acts.decide_action(
            title, view_memo,
            coins=int(acts.wallet().get("coins") or 0), practice=True)
        if any(decision.get(k) for k in ("like", "coin", "fav", "follow")):
            result = acts.apply(bvid, decision)
            acts.record({"user_id": user_id, "bvid": bvid, "title": title[:60],
                         "decision": decision, "result": result, "round": "practice"})
            ok_acts = {k: bool((result.get(k) or {}).get("ok"))
                       for k in ("like", "coin", "fav", "follow") if k in result}
            out["action"] = ok_acts
            if any(ok_acts.values()) and hasattr(l2, "record_life_action"):
                await l2.record_life_action(
                    user_id,
                    item_id=str(target.get("item_id") or ""),
                    ref=ref,
                    actions={**ok_acts, "why": str(decision.get("why") or "")},
                    author=str(target.get("author") or ""),
                    title=title,
                )
    # 说话：预算由 alice_speak 自己守
    sb = speak.budget()
    if sb["left"] > 0 and sb["wait_min"] <= 0:
        text = await speak.compose_comment(title, view_memo)
        if text:
            meta = acts.video_meta(bvid)
            posted = speak.post_comment(int(meta.get("aid") or 0), text)
            speak.record({"user_id": user_id, "bvid": bvid, "title": title[:60],
                          "text": text, "result": posted, "round": "practice"})
            out["comment"] = {"text": text, "ok": bool(posted.get("ok"))}
            if posted.get("ok") and hasattr(l2, "record_life_action"):
                await l2.record_life_action(
                    user_id,
                    item_id=str(target.get("item_id") or ""),
                    ref=ref,
                    comment=text,
                    author=str(target.get("author") or ""),
                    title=title,
                )

    acted.add(str(target.get("item_id") or ""))
    state["acted"] = sorted(acted)[-200:]
    return out


async def _read_round(l2, user_id: str, state: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """在 B 站、X、网文（起点/番茄）、B 站直播几条真实生活渠道间温和轮转（各自遵守独立间隔，互不挤占）。"""
    import alice_bili_live, alice_novel, alice_reading, alice_x

    last_plat = str(state.get("last_read_platform") or "")
    base_order = ["bilibili", "x", "novel", "live"]
    if last_plat in base_order:
        idx = base_order.index(last_plat)
        order = base_order[idx + 1 :] + base_order[: idx + 1]
    else:
        order = base_order

    last_res: dict[str, Any] = {"ok": False, "stage": "skip_recent"}
    for plat in order:
        if plat == "x":
            if not alice_x.is_configured():
                continue
            res = await alice_x.run_once(l2, user_id, force=force)
        elif plat == "novel":
            if not alice_novel.enabled() or not alice_novel.is_configured():
                continue
            res = await alice_novel.run_novel_reading_round(l2, user_id, force=force)
        elif plat == "live":
            if not alice_bili_live.enabled():
                continue
            res = await alice_bili_live.run_live_round(l2, user_id, force=force)
        else:
            res = await alice_reading.run_once(l2, user_id, force=force)
            res.setdefault("platform", "bilibili")
        last_res = res
        stg = str(res.get("stage") or "")
        if not stg.startswith(("skip_recent", "skip_no_live", "cooldown:", "x_not_configured", "no_cookies", "disabled")):
            state["last_read_platform"] = plat
            return res
    return last_res


async def run_round(l2, user_id: str, force: bool = False) -> dict[str, Any]:
    """一轮：读 / 做 / 歇。失败不抛，交给调用方记日志。"""
    state = _roll_day(load_state(user_id))
    kind = choose_kind(state)
    if kind == "practice":
        result = await _practice_round(l2, user_id, state)
        if result.get("stage") == "practice_no_target":
            # 没有可实践的目标就退回读一轮，别空转
            result = await _read_round(l2, user_id, state, force=force)
            kind = "read"
    else:
        result = await _read_round(l2, user_id, state, force=force)

    state["rounds"] = int(state.get("rounds") or 0) + 1
    state["today"][kind] = int(state["today"].get(kind) or 0) + 1
    state["last_kind"] = kind
    state["last_stage"] = str(result.get("stage") or "")
    save_state(user_id, state)
    journal({"user_id": user_id, "kind": kind, "platform": result.get("platform", "bilibili"),
             "stage": result.get("stage"), "chosen": result.get("chosen"),
             "action": result.get("action") or result.get("actions") or result.get("interaction"),
             "comment": result.get("comment") or result.get("speech"),
             "body_chars": result.get("body_chars")})
    return {**result, "round_kind": kind, "round_no": state["rounds"]}
