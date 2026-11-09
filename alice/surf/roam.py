"""她自己决定这一轮干什么：逛哪个站、继续眼前的事，还是干脆歇着。

和 shift.py 的区别（这是关键）：shift.py 用产品里的「轮转」排站点，什么时候逛、逛哪儿都由程序定；
这里每一轮先问她，把「休息」也放进选项，并把她上一轮想了什么、结果如何带进这一轮。
她选了什么、为什么，逐条记到 `.runtime/alice-surf/roam-YYYYMMDD.jsonl`。

    py -3.12 tools/alice-surf/roam.py --rounds 6 --gap-min 40

只读：公开动作的两条出口函数在模型调用前就被换掉（见 README 坑 0）。
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
API = ROOT / "alice"  # 数字人项目包根（memory/engine/agent/llm_client/rag）            # 只读：engine.alice_actions / memory.* / llm_client
ALICE_LIFE = ROOT / "alice" / "life"             # 她的阅读引擎
ALICE_CFG = ROOT / "alice" / "config"            # SPLIT-PLAN site 1：她的 config
RUNTIME = ROOT / ".runtime" / "qq-social"
OUT = ROOT / ".runtime" / "alice-surf"
QQ_TOOLS = ROOT / "alice" / "qq"
sys.path.insert(0, str(API))
sys.path.insert(0, str(ALICE_LIFE))              # alice/life：阅读引擎
sys.path.insert(0, str(ALICE_CFG))               # alice/config 优先
import config as product_config
sys.path.insert(0, str(QQ_TOOLS))

DEFAULT_USER = product_config.ALICE_TEST_USER_ID

for _flag in ("ALICE_INTERACT_ENABLED", "ALICE_SPEAK_ENABLED",
              "ALICE_X_INTERACT_ENABLED", "ALICE_X_SPEAK_ENABLED"):
    os.environ[_flag] = "false"
os.environ["ALICE_NOVEL_INTERACT_ENABLED"] = "false"

# 产品里各条路的节奏是固定的（B站视频 20 小时一条、直播 6 小时、小说 4 小时），
# 不看这个她只会反复挑到「刚看过、被跳过」的站，还会以为自己哪儿都去不了。
# 产品自带的间隔（B站 20 小时/条、直播 6 小时、小说 4 小时）是替她排的作息，不是她自己的意愿，
# 主人 2026-10-30 明确不要它：只要她想去就真的去。这里唯一保留的是「同一个站别连着刷」的工程护栏。
PACING_NOTE = "（这些地方现在都随时能去，没有额度限制；只是同一个地方别在短时间内连着刷。）"
SITE_FLOOR_MIN = 25

CHOICE_PROMPT = (
    "你是同一个使用 QQ 的测试人格 Alice。现在没人在跟你说话，你手里有段空白时间，本来也打算出去转转、看点东西。"
    "按你此刻自己的兴趣和你已有的经历决定：去哪里逛、翻翻哪个群之前的聊天，还是这会儿确实不想动、就想空着。"
    "不用轮流打卡，不用为了回答谁的问题去找答案，也没有人规定你每天必须看什么。"
    "只输出一个 JSON：{\"choice\": \"可用列表里的一个键\", \"reason\": \"一句你自己的话，说明当下为什么\"}。"
    + PACING_NOTE
)


def _gates_off() -> None:
    import config as product_config
    for attr in ("ALICE_INTERACT_ENABLED", "ALICE_SPEAK_ENABLED", "ALICE_X_INTERACT_ENABLED",
                 "ALICE_X_SPEAK_ENABLED", "ALICE_NOVEL_INTERACT_ENABLED"):
        if hasattr(product_config, attr):
            setattr(product_config, attr, False)
    product_config.ALICE_X_ENABLED = False  # 本机无出海代理，见 README 坑 1
    from engine import alice_actions, alice_speak
    alice_actions.apply = lambda *a, **k: {}
    alice_speak.post_comment = lambda *a, **k: {"ok": False, "disabled_by_roam": True}


def _one_line(summary: str, limit: int = 40) -> str:
    """画像摘要只留第一句，别把半句话塞给她。"""
    text = (summary or "").strip()
    for sep in ("。", "；", "！", "？"):
        idx = text.find(sep)
        if 0 < idx < limit:
            return text[:idx]
    clipped = text[:limit]
    for sep in ("，", "、", ",", " "):
        idx = clipped.rfind(sep)
        if idx > limit // 2:
            return clipped[:idx] + "…"
    return clipped + "…"


def available_platforms(cfg: dict, store, allow_rest: bool = True,
                        groups_only: bool = False) -> dict[str, str]:
    import alice_bili_live, alice_novel, alice_x

    out: dict[str, str] = {}
    if not groups_only:
        out["bilibili"] = "B站普通视频：标题、简介、热门评论（没有画面和音轨）"
        if alice_bili_live.enabled():
            out["live"] = "B站直播：听声音、读弹幕（没有画面）"
    if not groups_only and alice_novel.is_configured():
        cookies = alice_novel.load_novel_cookies()
        if alice_novel._get_qidian_playwright_cookies(cookies):
            out["qidian"] = "起点：书架和章节正文片段、本章说"
        if alice_novel._get_fanqie_playwright_cookies(cookies):
            out["fanqie"] = "番茄：书架和章节正文片段、评论"
    if not groups_only and alice_x.is_configured():
        out["x"] = "X：时间线和评论串"
    names = {scope: name for scope, _kind, name in
             store.db.execute("SELECT scope, kind, name FROM qq_contexts")}
    try:  # 群画像（qq_group_profiles）给她做选择时的一句话线索，没有就照旧只显示群名
        profiles = {scope: summary for scope, summary in
                    store.db.execute("SELECT scope, summary FROM qq_group_profiles")}
    except Exception:
        profiles = {}
    pending = {scope: n for scope, n in store.db.execute(
        "SELECT scope, COUNT(*) FROM qq_observed_messages WHERE processed=0 AND role='person' GROUP BY scope")}
    for scope in cfg.get("allowed_groups", []):
        left = pending.get(str(scope), 0)
        tail = ("，还有不少没翻过" if left > 400 else f"，还有 {left} 条没翻过") if left else "，之前的都翻完了"
        summary = _one_line(profiles.get(str(scope), ""))
        lead = f"{summary}；" if summary else ""
        out[f"group:{scope}"] = f"翻「{names.get(str(scope)) or scope}」群里之前的聊天记录（{lead}只翻，不发言{tail}）"
    if allow_rest:
        out["rest"] = "这会儿确实不想动，就想空着（不逛，也不翻记录）"
    return out


def outcome_text(record: dict) -> str:
    """把上一轮发生了什么写成人话，给她下一轮做判断用（不要原样丢 skip_recent 这种词给她）。"""
    result = record.get("result") or {}
    stage = str(result.get("stage") or "")
    label = str(record.get("choice_label") or record.get("choice") or "")
    if stage == "chosen_rest":
        return "我选了空着，哪儿也没去"
    if stage.startswith("skip_recent") or stage.startswith("cooldown"):
        return f"我想{label}，但那个刚看过、还在间隔里，没看成"
    if stage == "flipped":
        saved = result.get("quoted_facts_saved")
        if saved:
            return f"翻了一份{label}，记下了 {saved} 条别人说过的话，自己没说话"
        return f"翻了{label}，这一次没翻出新的可记的东西"
    if stage in ("done", "saved") or result.get("ok"):
        chosen = result.get("chosen")
        return f"{label}，看/听了《{chosen}》" if chosen else f"{label}，看了一会儿"
    if stage == "blocked":
        return f"想去{label}，但被挡住了"
    return f"{label}，结果：{stage or '没成'}"


async def ask_and_go(user_id: str, brain, recent: list[dict], cfg: dict, store,
                     cooling: dict[str, float] | None = None, allow_rest: bool = True,
                     groups_only: bool = False) -> dict:
    import alice_bili_live, alice_novel, alice_reading, alice_x
    from memory.l2_postgres import L2PostgresMemory

    # 先把窗口外的旧消息归档，否则选项里的「还有 N 条没翻」永远是她追不上的账。
    try:
        from social_memory import SocialMemory
        archived = SocialMemory(cfg, store).archive_stale()
        if archived:
            print('[roam] archived %d stale messages' % archived, flush=True)
    except Exception as exc:
        print('[roam] archive warn %s' % exc, flush=True)
    available = available_platforms(cfg, store, allow_rest, groups_only)
    now = time.time()
    for key, until in (cooling or {}).items():
        if available.get(key) and until > now:
            available.pop(key, None)  # 刚去过，这一轮先不摆出来（工程护栏，不是她的作息）
    raw = await brain.completion(
        [{"role": "system", "content": CHOICE_PROMPT},
         {"role": "user", "content": json.dumps(
             {"available": available,
              "recent": [{"我上轮": outcome_text(r)} for r in recent[-3:]],
              "now": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False)}],
        max_tokens=300, temperature=0.8,
    )
    text = raw.strip()
    if "```" in text:
        text = text.split("```")[1]
        text = text[4:] if text.lower().startswith("json") else text
    choice = json.loads(text[text.index("{"): text.rindex("}") + 1])
    platform = str(choice.get("choice") or "")
    reason = str(choice.get("reason") or "")[:200]
    if platform not in available:
        raise ValueError(f"invalid_choice:{platform}")

    record: dict = {"choice": platform, "choice_label": available[platform],
                    "reason": reason, "available": list(available)}
    if platform == "rest":
        record["result"] = {"stage": "chosen_rest"}
        return record

    if platform.startswith("group:"):
        from social_memory import SocialMemory
        memory = SocialMemory(cfg, store)
        scope = platform.split(":", 1)[1]
        flipped = await memory.consolidate(brain.completion, focus_scope=scope, immediate=True)
        record["result"] = {"stage": "flipped", **(flipped or {})}
        # 翻群也是一段经历：和读书/B站/起点写同一条 life_items 管道，
        # 这样「今天」不空着，她不用靠嘴补。
        try:
            l2 = L2PostgresMemory()
            try:
                row = store.db.execute("SELECT name FROM qq_contexts WHERE scope=?", (scope,)).fetchone()
                name = str((row or [""])[0] or scope)
                seen = int((flipped or {}).get("source_messages") or 0)
                saved = int((flipped or {}).get("quoted_facts_saved") or 0)
                if seen:
                    # 她是抽样地看群，不是群的记录员：不写「还剩 N 条没翻」，
                    # 那个数字只会让她觉得自己永远欠着账（已定：接受抽样）。
                    await l2.append_life_item(user_id, {
                        "text": f"翻了「{name}」的聊天，看了 {seen} 条"
                                + (f"，记下 {saved} 句别人说的话" if saved else "，没翻出新的"),
                        "kind": "activity", "source": f"qq_group:{scope}",
                        "ref": f"qq:{scope}:{int(time.time())}", "topic": name, "depth": 0.8,
                    }, days=7)
            finally:
                pool = getattr(l2, "_pool", None)
                if pool is not None:
                    await pool.close()
        except Exception as exc:
            print(f"[roam] life_item warn {type(exc).__name__}: {exc}", flush=True)
        return record

    l2 = L2PostgresMemory()
    try:
        # 小说这条路没有「读下一章」的游标，force 绕过间隔只会把同一章重读一遍
        # （2026-10-30 04:36 与 04:47 两次都是《玄鉴仙族》1839 字）。所以小说保留它自己的间隔，
        # 其余三条路 force：她想看就看，站点各自会挑新内容。
        if platform in ("qidian", "fanqie"):
            res = await alice_novel.run_novel_reading_round(l2, user_id, platform=platform)
        elif platform == "bilibili":
            res = await alice_reading.run_once(l2, user_id, force=True)
        elif platform == "live":
            res = await alice_bili_live.run_live_round(l2, user_id, seconds=60, force=True)
        else:
            res = await alice_x.run_once(l2, user_id, force=True)
    finally:
        pool = getattr(l2, "_pool", None)
        if pool is not None:
            await pool.close()
    record["result"] = {k: res.get(k) for k in ("ok", "stage", "chosen", "body_chars") if k in res}
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--gap-min", type=float, default=40)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--groups-only", action="store_true", help="这一轮只让她在几个群之间挑，不去站外")
    ap.add_argument("--no-rest", action="store_true",
                    help="这一班不把「歇着」摆出来（主人点名要她出去时才用；平常规矩是给她这个选项）")
    args = ap.parse_args()

    _gates_off()
    from channel import Store  # noqa: E402  (QQ runtime 目录在 Codex 工作树里，见 README)
    from alice import Alice

    cfg = json.loads((RUNTIME / "config.json").read_text(encoding="utf-8-sig"))
    # 一次「翻群」= 一次模型调用；产品默认 6 次/天是给常驻桥留的余量，这里放宽以免她选了翻不成。
    cfg["social_memory_calls_per_day"] = max(40, int(cfg.get("social_memory_calls_per_day", 6)))
    store = Store(RUNTIME / "channel.sqlite3")
    brain = Alice(cfg, store)

    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / f"roam-{time.strftime('%Y%m%d')}.jsonl"
    recent: list[dict] = []
    cooling: dict[str, float] = {}

    async def run() -> None:
        await brain.connect()
        try:
            for i in range(args.rounds):
                started = time.time()
                try:
                    record = await ask_and_go(args.user, brain, recent, cfg, store, cooling,
                                              allow_rest=not args.no_rest,
                                              groups_only=args.groups_only)
                except Exception as exc:
                    record = {"error": f"{type(exc).__name__}: {exc}"[:300]}
                record["outcome"] = outcome_text(record)
                if record.get("choice") and not str(record["choice"]).startswith(("rest", "group:")):
                    cooling[record["choice"]] = time.time() + SITE_FLOOR_MIN * 60
                record["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                record["round"] = i + 1
                recent.append(record)
                with log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(json.dumps({k: record.get(k) for k in ("round", "choice", "reason", "result", "error")
                                  if record.get(k) is not None}, ensure_ascii=False), flush=True)
                if i + 1 < args.rounds:
                    remain = args.gap_min * 60 - (time.time() - started)
                    if remain > 0:
                        time.sleep(remain)
        finally:
            pool = getattr(brain, "pool", None)
            if pool is not None:
                await pool.close()
            store.db.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
