# -*- coding: utf-8 -*-
"""她在 B 站上"动手"：点赞、投币、收藏、关注。

设计原则（跟阅读线一致）：
- **判断交给模型**：动不动手、动哪个，由 `decide_action` 这一次小调用决定，不写死规则。
- **边界交给代码**：每日上限、最小间隔、硬币余额、从不取关——这些是代码守的，不是让她自律。
- **账要落**：每次动手写一行 `interactions.jsonl`，出事能查、能停。

风控：B 站对写操作比读敏感得多。返回 -352 / -412 / -111 时立刻记下来并停手，
不重试、不换参数硬顶——被风控的号会连读都读不了。
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_COOKIE_PATH = _ROOT / ".runtime" / "bili_cookies.json"
_LEDGER = _ROOT / ".runtime" / "interactions.jsonl"

_API = "https://api.bilibili.com"


def _cookies() -> dict[str, str]:
    try:
        data = json.loads(_COOKIE_PATH.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in (data or {}).items() if k and v}
    except Exception:
        return {}


def _csrf() -> str:
    return _cookies().get("bili_jct", "")


def enabled() -> bool:
    return str(os.getenv("ALICE_INTERACT_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}


def _daily_max() -> int:
    try:
        return int(os.getenv("ALICE_INTERACT_DAILY_MAX", "30") or 30)
    except ValueError:
        return 30


def _min_gap_min() -> float:
    try:
        return float(os.getenv("ALICE_INTERACT_MIN_GAP_MIN", "6") or 6)
    except ValueError:
        return 6.0


def _get(url: str, timeout: int = 15) -> dict[str, Any]:
    cookie = "; ".join(f"{k}={v}" for k, v in _cookies().items())
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Referer": "https://www.bilibili.com/", "Cookie": cookie})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _post(url: str, data: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    cookie = "; ".join(f"{k}={v}" for k, v in _cookies().items())
    body = urllib.parse.urlencode({**data, "csrf": _csrf()}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie,
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


# ---------------- 账号状态 ----------------

def wallet() -> dict[str, Any]:
    """硬币余额、是否已登录。动手前先看这个。"""
    try:
        d = _get(f"{_API}/x/web-interface/nav").get("data") or {}
        return {"login": bool(d.get("isLogin")), "coins": int(d.get("money") or 0),
                "mid": int(d.get("mid") or 0), "uname": str(d.get("uname") or "")}
    except Exception:
        return {"login": False, "coins": 0, "mid": 0, "uname": ""}


def video_meta(bvid: str) -> dict[str, Any]:
    try:
        d = _get(f"{_API}/x/web-interface/view?bvid={urllib.parse.quote(bvid)}").get("data") or {}
        owner = d.get("owner") or {}
        return {"aid": int(d.get("aid") or 0), "mid": int(owner.get("mid") or 0),
                "owner": str(owner.get("name") or "")}
    except Exception:
        return {"aid": 0, "mid": 0, "owner": ""}


def default_fav_folder(mid: int) -> int:
    """默认收藏夹 id。没有就新建不了（B 站接口新建需要更复杂签名），返回 0。"""
    try:
        d = _get(f"{_API}/x/v3/fav/folder/created/list-all?up_mid={mid}").get("data") or {}
        folders = d.get("list") or []
        for f in folders:
            if str(f.get("title") or "") in {"默认收藏夹", "默认"}:
                return int(f.get("id") or 0)
        return int((folders[0] or {}).get("id") or 0) if folders else 0
    except Exception:
        return 0


# ---------------- 写操作 ----------------

def like(bvid: str, on: bool = True) -> dict[str, Any]:
    try:
        r = _post(f"{_API}/x/web-interface/archive/like",
                  {"bvid": bvid, "like": 1 if on else 2})
        return {"ok": r.get("code") == 0, "code": r.get("code"), "msg": r.get("message")}
    except Exception as exc:
        return {"ok": False, "code": -1, "msg": type(exc).__name__}


def coin(bvid: str, n: int = 1) -> dict[str, Any]:
    try:
        r = _post(f"{_API}/x/web-interface/coin/add",
                  {"bvid": bvid, "multiply": max(1, min(int(n), 2)), "select_like": 0})
        return {"ok": r.get("code") == 0, "code": r.get("code"), "msg": r.get("message")}
    except Exception as exc:
        return {"ok": False, "code": -1, "msg": type(exc).__name__}


def favorite(aid: int, account_mid: int | None = None) -> dict[str, Any]:
    """收藏到默认收藏夹。account_mid 是**这个账号**的 mid（不是 UP 主的），
    漏传就用 wallet 现取——曾经把它和视频作者的 mid 搞混，结果永远找不到收藏夹。"""
    mid = int(account_mid or 0) or int(wallet().get("mid") or 0)
    folder = default_fav_folder(mid)
    if not folder:
        return {"ok": False, "code": -2, "msg": "no_folder"}
    try:
        r = _post(f"{_API}/x/v3/fav/resource/deal",
                  {"rid": aid, "type": 2, "add_media_ids": folder, "del_media_ids": ""})
        return {"ok": r.get("code") == 0, "code": r.get("code"), "msg": r.get("message")}
    except Exception as exc:
        return {"ok": False, "code": -1, "msg": type(exc).__name__}


def follow(mid: int) -> dict[str, Any]:
    if not mid:
        return {"ok": False, "code": -2, "msg": "no_mid"}
    try:
        r = _post(f"{_API}/x/relation/modify",
                  {"fid": mid, "act": 1, "re_src": 11})
        return {"ok": r.get("code") == 0, "code": r.get("code"), "msg": r.get("message")}
    except Exception as exc:
        return {"ok": False, "code": -1, "msg": type(exc).__name__}


# ---------------- 预算：每日上限 + 最小间隔 ----------------

def _ledger_rows(days: int = 2) -> list[dict[str, Any]]:
    if not _LEDGER.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for line in _LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            out.append(row)
    return out


def budget() -> dict[str, Any]:
    """今天还能动几次手、还要等多久。"""
    rows = _ledger_rows(days=2)
    now = datetime.now(timezone.utc)
    today = now.astimezone(timezone(timedelta(hours=8))).date()
    used = 0
    last_ts = None
    for row in rows:
        ts = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.astimezone(timezone(timedelta(hours=8))).date() == today:
            used += 1
        if last_ts is None or ts > last_ts:
            last_ts = ts
    wait = 0.0
    if last_ts:
        wait = max(0.0, _min_gap_min() - (now - last_ts).total_seconds() / 60.0)
    return {"used_today": used, "left": max(0, _daily_max() - used), "wait_min": round(wait, 1)}


def record(row: dict[str, Any]) -> None:
    row = dict(row)
    row["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------- 她自己决定动不动手 ----------------

DECIDE_SYSTEM = "\n".join([
    "你刚看完一个 B 站视频，评论区也看了。现在只有一件事：你真想不想在这个视频上做点什么。",
    "可选项：点赞（觉得好）、投币（觉得值得，消耗你自己的硬币）、收藏（想以后再翻）、关注这个 UP 主（想看他别的）。",
    "按你真实的感觉来。**大多数时候什么都不做才是正常的**——真人一天刷几十个视频，点赞的可能只有两三个。",
    "不要为了让别人觉得你有品味就点什么，也不要每个都点。",
    '只输出一行 JSON，不要解释、不要代码块：{"like":true|false,"coin":true|false,"fav":true|false,"follow":true|false,"why":"一句话，你自己的话，不超过20字"}',
])


PRACTICE_SYSTEM = "\n".join([
    "你回头翻到自己之前看过的一条东西。你是专门回来看看的，不是随手刷到的。",
    "现在想不想为它做点什么：点赞（值得）、投币（特别值，花你自己的币）、收藏（想留着）、关注作者（想看他别的）。",
    "也可以只是看看就走——但这次是你自己走回来的，跟随手刷过去不一样。",
    '只输出一行 JSON：{"like":true|false,"coin":true|false,"fav":true|false,"follow":true|false,"why":"一句话"}',
])


async def decide_action(title: str, opinion: str, taste: str = "",
                        coins: int | None = None, practice: bool = False) -> dict[str, Any]:
    """她要不要动手。失败就当不动手——宁可不做，不要瞎做。"""
    from alice_reading import _llm  # SPLIT-PLAN: alice/life canonical

    user_msg = f"视频：{title}\n你刚说的看法：{opinion or '（没什么特别的）'}"
    if coins is not None:
        user_msg += f"\n你钱包里还有 {coins} 个硬币，投一次币花 1 个。"
    if taste:
        user_msg += f"\n你一贯的口味：{taste}"
    try:
        raw = await _llm([{"role": "system",
                           "content": PRACTICE_SYSTEM if practice else DECIDE_SYSTEM},
                          {"role": "user", "content": user_msg}],
                         max_tokens=80, temperature=0.6)
    except Exception:
        return {}
    text = str(raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return {}
    out: dict[str, Any] = {k: bool(data.get(k)) for k in ("like", "coin", "fav", "follow")}
    out["why"] = " ".join(str(data.get("why") or "").split())[:40]
    return out


def apply(bvid: str, decision: dict[str, Any]) -> dict[str, Any]:
    """按她的决定真的动手。点赞→投币→收藏 顺序，关注单独。返回每一步结果。"""
    done: dict[str, Any] = {}
    if not any(decision.get(k) for k in ("like", "coin", "fav", "follow")):
        return done

    meta = video_meta(bvid)
    if decision.get("like"):
        done["like"] = like(bvid)
    if decision.get("coin"):
        coins = wallet().get("coins", 0)
        if coins >= 1:  # 她只有 1 个币时也该能投出去；转正后每日登录会补
            done["coin"] = coin(bvid, 1)
        else:
            done["coin"] = {"ok": False, "code": -3, "msg": f"coins_low:{coins}"}
    if decision.get("fav") and meta.get("aid"):
        done["fav"] = favorite(meta["aid"])
    if decision.get("follow") and meta.get("mid"):
        done["follow"] = follow(meta["mid"])
    done["_meta"] = meta
    return done
