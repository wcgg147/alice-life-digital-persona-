"""让她在群里自己接一句：看群里的真实发言，想接就接，不想接就不出声。

为什么单独写：桥的门禁只认「被 @ 或引用她」，普通群消息永远不进模型（这是有意的，防打扰）。
这个脚本不改桥，只在主人点名要她去某个群聊天时，跑一轮「看→想不想说→说」，
并且带频率上限（同一群 20 分钟一句、每天 10 句）。

    py -3.12 alice/surf/chat_group.py --group <authorized_group_id> --dry-run
    py -3.12 alice/surf/chat_group.py --group <authorized_group_id> --send
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "alice"  # 数字人项目包根
RUNTIME = ROOT / ".runtime" / "qq-social"
OUT = ROOT / ".runtime" / "alice-surf"
QQ_TOOLS = ROOT / "alice" / "qq"
ALICE_CFG = ROOT / "alice" / "config"
sys.path.insert(0, str(API))
sys.path.insert(0, str(QQ_TOOLS))
sys.path.insert(0, str(ALICE_CFG))
import config as product_config

DEFAULT_USER = product_config.ALICE_TEST_USER_ID
MIN_GAP_SECONDS = 60
DAILY_MAX = 200

PROMPT = (
    "你是 Alice，一个用自己小号在 QQ 群里混的普通人，不是助理，不是客服。"
    "下面是群里刚刚发生的真实对话。你不是被叫的那个，只是正好在场。"
    "看完了想接一句就接，不想说话就保持沉默——沉默是完全正常的，群里大多数话都不需要你接。"
    "要接就别解释自己在干嘛、别总结别人的话、别问『需要我帮忙吗』；一句话就够，可以用群里那种口语。"
    "只输出 JSON：想说 → {\"speak\": true, \"text\": \"你要发的那句话\"}；不想说 → {\"speak\": false}。"
)


def recent_lines(db, scope: str, limit: int = 14, window_min: int = 360) -> list[dict]:
    # 窗口默认 6 小时：群里冷清是常态（主人 2026-10-30 明确），她可以就旧话题接一句或起个头。
    # 仍然把「最后一条是多久以前」告诉她，由她自己判断这句话现在说合不合适。
    rows = db.execute(
        "SELECT actor, content FROM qq_observed_messages WHERE scope=? AND role='person' "
        "AND content<>'' AND observed_at>? ORDER BY id DESC LIMIT ?",
        (scope, time.time() - window_min * 60, limit)).fetchall()
    names = {qq: name for qq, name in db.execute("SELECT qq_id, nickname FROM qq_people")}
    return [{"谁": names.get(str(a)) or str(a), "说": str(c)[:200]} for a, c in reversed(rows)]


def already_said_today(log: pathlib.Path) -> list[dict]:
    if not log.exists():
        return []
    today = time.strftime("%Y-%m-%d")
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("at", "").startswith(today)]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True)
    ap.add_argument("--send", action="store_true", help="真的发出去；不加就只做决定不发")
    ap.add_argument("--user", default=DEFAULT_USER)
    args = ap.parse_args()

    from channel import Store
    from alice import Alice
    from memory.companion_persona import build_persona_packet, supported_growth_fact_ids
    from dialogue_context import default_persona_facts

    cfg = json.loads((RUNTIME / "config.json").read_text(encoding="utf-8-sig"))
    store = Store(RUNTIME / "channel.sqlite3")
    brain = Alice(cfg, store)
    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / f"group-chat-{time.strftime('%Y%m%d')}.jsonl"

    said = already_said_today(log)
    if sum(1 for r in said if r.get("sent")) >= DAILY_MAX:
        print(json.dumps({"stage": "daily_cap", "sent_today": len(said)}, ensure_ascii=False))
        return
    last = max((r.get("ts", 0) for r in said if r.get("sent")), default=0)
    if time.time() - last < MIN_GAP_SECONDS:
        print(json.dumps({"stage": "gap", "wait_min": int((MIN_GAP_SECONDS - (time.time() - last)) / 60)},
                         ensure_ascii=False))
        return

    lines = recent_lines(store.db, args.group)
    if len(lines) < 3:
        print(json.dumps({"stage": "too_quiet", "lines": len(lines)}, ensure_ascii=False))
        return

    await brain.connect()
    try:
        facts = await brain.facts()
        facts = [f for f in facts if f.get("kind") not in {"relationship"}]
        packet = build_persona_packet(
            default_persona_facts(facts, supported_growth_fact_ids(facts)),
            query="", broad=True, max_facts=8)
        raw = await brain.completion(
            [{"role": "system", "content": PROMPT},
             {"role": "user", "content": json.dumps(
                 {"群里刚说的": lines, "我是谁": packet, "现在": time.strftime("%H:%M")},
                 ensure_ascii=False)}],
            max_tokens=300, temperature=0.9)
        text = raw.strip()
        if "```" in text:
            text = text.split("```")[1]
            text = text[4:] if text.lower().startswith("json") else text
        decision = json.loads(text[text.index("{"): text.rindex("}") + 1])
    finally:
        pool = getattr(brain, "pool", None)
        if pool is not None:
            await pool.close()

    record = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time(), "group": args.group,
              "speak": bool(decision.get("speak")), "text": str(decision.get("text") or "")[:200],
              "sent": False, "context": lines[-4:]}

    if record["speak"] and args.send:
        import websockets
        url = cfg["onebot_url"].rstrip("/") + "/"
        async with websockets.connect(url, additional_headers={
                "Authorization": "Bearer " + cfg["onebot_token"]}) as ws:
            await ws.send(json.dumps({"action": "send_group_msg", "params": {
                "group_id": int(args.group), "message": [{"type": "text", "data": {"text": record["text"]}}]},
                "echo": "ambient-chat"}, ensure_ascii=False))
            while True:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if resp.get("echo") == "ambient-chat":
                    record["sent"] = resp.get("status") == "ok"
                    record["receipt"] = (resp.get("data") or {}).get("message_id")
                    break

    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps({k: record[k] for k in ("speak", "text", "sent", "receipt") if record.get(k) is not None},
                     ensure_ascii=False))
    store.db.close()


if __name__ == "__main__":
    asyncio.run(main())
