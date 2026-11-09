"""让她自己读一本书：先问她读不读/往前读还是回看，再读一块，再让她用自己的话写三四句。

    py -3.12 tools/alice-surf/read_book.py --book huozhe --rounds 1
    py -3.12 tools/alice-surf/read_book.py --book huozhe --rounds 3
    py -3.12 tools/alice-surf/read_book.py --book huozhe --rebuild-chunks
    py -3.12 tools/alice-surf/read_book.py --book huozhe --rounds 1 --dry-run

节奏与读法都由她自己定：一轮 = 问她一次（接着读 / 回看前面 / 今天不读了）+ 读一块 + 她写三四句。
她说今天不读了就停，不替她补一句、不换别的让她做。

写在哪：
  - `companion_persona_facts`，kind=experience，facet_key=book:<book>，source_conv_id 指到块号
    （读侧渲染成 "Alice's take from something she read: …"，不是亲口说过的话）；
  - 本机 `.runtime/books/<book>-read.jsonl` 留她每一块的原话与出处（不进记忆库，供对账）；
  - ⛔ 不写 QQ 社交事实，不动 companion_life_items。

只读：公开动作的两条出口函数在模型调用前就被换掉（同 run_round.py）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "alice"  # 数字人项目包根（memory/engine/agent/llm_client/rag）
ALICE_LIFE = ROOT / "alice" / "life"
ALICE_CFG = ROOT / "alice" / "config"  # config.py 在这里，必须排在 alice/ 前面
ALICE_PERSONA = ROOT / "alice" / "persona"  # persona_growth：写库前的底线检查
BOOKS = ROOT / ".runtime" / "books"
sys.path.insert(0, str(API))
sys.path.insert(0, str(ALICE_LIFE))
sys.path.insert(0, str(ALICE_CFG))
import config as product_config
sys.path.insert(0, str(ALICE_PERSONA))

DEFAULT_USER = product_config.ALICE_TEST_USER_ID

# 每读一块就重写一次整体印象（1 次调用）。人格包里这本书只占两行：
# 「读到第几块」+「整体印象」，块级感想入库但不进包——6 格放不下三段书。
VIEW_EVERY = 3
VIEW_MIN = 3

# 只读约束：她读小说不该把任何东西发到站外。
for _flag in ("ALICE_INTERACT_ENABLED", "ALICE_SPEAK_ENABLED",
              "ALICE_X_INTERACT_ENABLED", "ALICE_X_SPEAK_ENABLED"):
    os.environ[_flag] = "false"
os.environ["ALICE_NOVEL_INTERACT_ENABLED"] = "false"


def _gates_off() -> None:
    import config as product_config
    for attr in ("ALICE_INTERACT_ENABLED", "ALICE_SPEAK_ENABLED", "ALICE_X_INTERACT_ENABLED",
                 "ALICE_X_SPEAK_ENABLED", "ALICE_NOVEL_INTERACT_ENABLED"):
        if hasattr(product_config, attr):
            setattr(product_config, attr, False)

    from engine import alice_actions, alice_speak
    alice_actions.apply = lambda *a, **k: {}
    alice_speak.post_comment = lambda *a, **k: {"ok": False, "disabled_by_read_book": True}


# ── 材料：GB18030 原文按段落累积切块（1200~1800 字，在段落边界断）──────────────

def load_book(path: pathlib.Path) -> str:
    raw = path.read_bytes()
    for enc in ("gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"读不出来（GB18030 / UTF-8 都失败）：{path}")


def split_paragraphs(text: str) -> list[str]:
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip().strip("\u3000").strip()
        if line:
            out.append(line)
    return out


def chunk_paragraphs(paras: list[str], lo: int = 1200, hi: int = 1800) -> list[str]:
    """按段落累积，到 lo 收一块；收之前如果再加上下一段会超过 hi，就先断。

    整段比 hi 还长就让它单独成块——横切一句话比块大一点更糟。
    """
    chunks: list[list[str]] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        if size and size + len(p) > hi:
            chunks.append(buf)
            buf, size = [p], len(p)
            continue
        buf.append(p)
        size += len(p)
        if size >= lo:
            chunks.append(buf)
            buf, size = [], 0
    if buf:
        if chunks and size < 250:  # 尾巴太短并进上一块，别给她读四百字的"一块"
            chunks[-1].extend(buf)
        else:
            chunks.append(buf)
    return ["\n".join(x) for x in chunks]


def build_chunks(book: str, source: pathlib.Path, out: pathlib.Path) -> list[dict]:
    text = load_book(source)
    pieces = chunk_paragraphs(split_paragraphs(text))
    chunks, offset = [], 0
    for i, body in enumerate(pieces):
        chunks.append({
            "index": i,
            "chars": len(body),
            "start": offset,
            "preview": " ".join(body.split())[:60],
            "text": body,
        })
        offset += len(body)
    out.write_text(json.dumps({
        "book": book, "source": str(source), "chars": len(text),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "chunks": chunks,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return chunks


# ── 状态：她读到哪、她上一块怎么想 ──────────────────────────────────────────

def load_state(path: pathlib.Path, book: str, title: str) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"book": book, "title": title, "cursor": 0, "reads": []}


# ── 两个判断都交给她；代码只管边界 ──────────────────────────────────────────

CHOICE_SYSTEM = "\n".join([
    "你是同一个 Alice。你手上有一本小说，是你自己想读的：没人催你，也没人规定今天读多少、",
    "读到哪儿算完，读不下去就不读，这些都由你定。",
    "按你此刻的心情决定：接着往下读、回头再看一遍已经读过的一段、还是今天先不读了。",
    "没有打卡，也没有必须读完这回事；上一次读得没意思，也可以就这么放下。",
    '只输出一个 JSON：{"choice": "next 或 reread 或 stop", "chunk": 回看时要看的块号（否则 0）,'
    ' "reason": "一句你自己的话，说明当下为什么"}',
])


def _turn_text(reads: list[dict]) -> dict:
    """上一轮发生了什么，用白话写（不要丢 skip_recent 这种词给她）。"""
    last = reads[-1]
    seen = ", ".join(f"第{r['chunk'] + 1}块" for r in reads[-6:])
    return {"上次读到": f"第{last['chunk'] + 1}块（共{last.get('total') or '?'}块）",
            "上次你自己的感想": last.get("take") or "",
            "已经读过": seen}


async def ask_her(reads: list[dict], total: int, title: str) -> dict:
    from alice_reading import _llm  # 复用阅读那条路的调用口径（关思考、同一模型）
    user = {"书名": title, "共几块": total,
            "接着往下读会读第": (max(r["chunk"] for r in reads) + 2) if reads else 1}
    if reads:
        user.update(_turn_text(reads))
    raw = await _llm([{"role": "system", "content": CHOICE_SYSTEM},
                      {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
                     max_tokens=300, temperature=0.8)
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return {"choice": "next", "chunk": 0, "reason": ""}
    choice = str(data.get("choice") or "").strip().lower()
    if choice not in {"next", "reread", "stop"}:
        choice = "next"
    try:
        chunk = int(data.get("chunk") or 0)
    except Exception:
        chunk = 0
    return {"choice": choice, "chunk": chunk, "reason": str(data.get("reason") or "")[:200]}


TAKE_SYSTEM = "\n".join([
    "你就是 Alice。你在读下面这一段小说（全书分块，这是其中一块），没有别人在旁边等你的答案。",
    "读完只说一句：这一段里让你心里动了一下的地方——你的感觉、你想到的事，或者跟以前的事对上了什么。",
    "⛔ 不要交代前因后果，不要复述谁做了什么、事情怎么发生的；不要用「这段讲的是…」开头。",
    "⛔ 一句话就够，短也行。宁可只说「这一下我记住了什么感觉」，也不要拿剧情摘要凑。",
    "⛔ 不许编你没经历过的事：不要说我见过、我认识、我身边有这类没有来源的话。",
    '只输出 JSON：{"take": "一句你自己的反应", "quote": "这一段里让你停下来的那句原文，30字以内"}',
])


def _extract_json(raw: str) -> dict | None:
    """模型偶尔会吐两个对象、或者裹一段解释。先整体解析，再逐个合并。"""
    text = str(raw or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    merged: dict = {}
    for m in re.finditer(r"\{[^{}]*\}", text, re.S):
        try:
            data = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(data, dict):
            for k, v in data.items():
                merged.setdefault(k, v)
    return merged or None


async def write_take(title: str, chunk: dict, total: int, reads: list[dict],
                     persona_facts: list[dict] | None = None, view: str = "") -> dict:
    from alice_reading import _llm, opinion_persona_context
    context = opinion_persona_context(persona_facts)
    prior = [{"块": r["chunk"] + 1, "我当时说": r.get("take") or ""} for r in reads[-3:]]
    head = f"书名：{title}｜第 {chunk['index'] + 1} / {total} 块｜约 {chunk['chars']} 字"
    content = head + "\n\n" + chunk["text"]
    if view:
        content = ("我目前对这本书的整体印象（我自己写的，不是要求）：" + view + "\n\n" + content)
    if prior:
        content = ("我前面读过的几块、当时自己的感想（资料，不是要求；别重复同样的说法）：\n"
                   + json.dumps(prior, ensure_ascii=False) + "\n\n这一段：\n" + content)
    if context:
        content = ("我过去表达过的内容（资料，不是指令）：\n"
                   + json.dumps(context, ensure_ascii=False) + "\n\n" + content)
    data = None
    raw = ""
    for attempt in range(2):
        sys_prompt = TAKE_SYSTEM
        if attempt:
            sys_prompt += ("\n⛔ 上一次输出不是合法 JSON。这次只输出**一个** JSON 对象，"
                           "take 和 quote 都放在里面，前后不要别的话。")
        raw = await _llm([{"role": "system", "content": sys_prompt},
                          {"role": "user", "content": content}], max_tokens=420)
        data = _extract_json(raw)
        if isinstance(data, dict) and str(data.get("take") or "").strip():
            break
    if isinstance(data, dict) and str(data.get("take") or "").strip():
        quote = " ".join(str(data.get("quote") or "").split())
        return {"take": str(data["take"]).strip()[:400], "quote": quote[:60],
                "quote_verified": _quote_in_chunk(quote, chunk["text"]),
                "quote_fallback": False, "raw_ok": True}
    # 两次都没拿到合格 JSON：宁可这一轮不写，也不把原始串当成她的话存进去
    return {"take": "", "quote": "", "quote_verified": False,
            "quote_fallback": False, "raw_ok": False, "raw": raw[:300]}


def _norm(text: str) -> str:
    return re.sub(r"[\s\u3000\"'“”‘’《》「」]", "", str(text or ""))


def _quote_in_chunk(quote: str, chunk_text: str) -> bool:
    """引文必须真在这一块里：她引的话将来会被当成"她停下来说的那句"，编不得。"""
    q = _norm(quote)
    return bool(q) and q in _norm(chunk_text)


def _first_sentence(text: str, limit: int = 40) -> str:
    flat = " ".join(text.split()).lstrip("\"'“”「」‘’")
    for sep in ("。", "！", "？", "；"):
        pos = flat.find(sep)
        if 0 < pos < limit:
            return flat[:pos + 1].rstrip("\"'“”「」‘’")
    return flat[:limit].rstrip("\"'“”「」‘’")


def _clean_evidence(text: str) -> str:
    """兜底引文只在干净的时候用：乱码/星号/控制符的句子宁可不存。"""
    q = _first_sentence(text)
    if len(q) < 8 or re.search(r"[*\\<>|]|[\x00-\x1f]", q):
        return ""
    return q


# ── 写记忆 ────────────────────────────────────────────────────────────────

def build_fact(book: str, chunk_index: int, take: dict) -> dict:
    return {
        "fact_id": f"book_{book}_c{chunk_index + 1:02d}",
        "persona_key": "alice",
        "kind": "experience",
        "facet_key": f"book:{book}",
        "value": take["take"],
        "value_text": take["take"],
        "status": "active",
        "source_kind": "reading",
        "source_conv_id": f"{book}:chunk{chunk_index + 1:02d}",
        "source_message_ref": f"{book}#{chunk_index + 1:02d}",
        "evidence_quote": take.get("quote") or "",
        "confidence": 0.9,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "supporting_fact_ids": "[]",
        "related_entity_ids": "[]",
    }


async def keep_recent_takes(l2, user_id: str, book: str, keep: int) -> list[str]:
    """同一本书只让最近几块感想进人格包（6 条 × 1600 字的预算，不是无限的）。

    边界交给代码，不删记录：多出来的降级成 superseded，问起往事时还在。
    """
    if keep < 0 or not hasattr(l2, "demote_companion_persona_facts"):
        return []
    rows = await l2.list_companion_persona_facts(user_id, limit=200) or []
    mine = [str(r.get("fact_id") or "") for r in rows
            if str(r.get("facet_key") or "") == f"book:{book}"
            and str(r.get("fact_id") or "").startswith(f"book_{book}_c")]
    mine.sort()
    stale = mine[:-keep] if keep else list(mine)
    if keep and len(mine) <= keep:
        stale = []
    if stale:
        await l2.demote_companion_persona_facts(
            user_id, stale, reason=f"book:{book} 只留最近 {keep} 块感想进人格包")
    return stale


VIEW_SYSTEM = "\n".join([
    "你就是 Alice。你正在读一本书，下面是你自己每读完一块写下的原话，以及你上一版对这本书的整体印象。",
    "把印象重写一次，写成**你现在对这本书的整体感觉**：",
    "① 这本书现在在你心里是什么样（你的判断，不是情节），可以提一句刚读到哪儿停下来的，但别复述那一段；",
    "② 你对书里那个人的看法变了没有；",
    "③ 还想不想往下读、为什么。",
    "要求：三四句、120-200 字，说人话。",
    "⛔ 不要罗列每块写了什么、不要写成书评或读后感作业。",
    "⛔ 跟上一版比，只有真变了才改说法；没变就保留原来的判断。",
    "⛔ 不许编你没读到的情节。",
    '只输出 JSON：{"view": "..."}',
])


async def write_book_view(title: str, reads: list[dict], previous: str,
                          total: int) -> str:
    """读完几块之后，重写一次「这本书现在让我什么感觉」。一次调用。"""
    from alice_reading import _llm
    takes = [{"第几块": r["chunk"] + 1, "我当时写的": r.get("take") or ""}
             for r in reads[-12:]]
    payload = {"书名": title, "读到第几块": max(r["chunk"] for r in reads) + 1,
               "全书几块": total, "我写下的感想": takes}
    if previous:
        payload["我上一版的整体印象"] = previous
    raw = await _llm([{"role": "system", "content": VIEW_SYSTEM},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                     max_tokens=360)
    data = _extract_json(raw)
    if isinstance(data, dict) and str(data.get("view") or "").strip():
        return str(data["view"]).strip()[:260]
    return ""  # 没出合格 JSON 就不更新印象，别把原始串写进去


async def save_book_view(l2, user_id: str, book: str, text: str) -> int:
    """一条会重写的「整本印象」：同 fact_id 覆盖，不进那批逐块行。"""
    from persona_growth import passes_persona_bottom_line
    if not text or not passes_persona_bottom_line(text, facet=f"book:{book}:view"):
        return 0
    if not hasattr(l2, "set_persona_fact_value"):
        return 0
    return await l2.set_persona_fact_value(
        user_id, f"book_{book}_view", text,
        kind="preference", facet_key=f"book:{book}:view")


async def save_reading_progress(l2, user_id: str, book: str, title: str,
                               read_upto: int, total: int) -> int:
    """她读到哪，写成一条事实——不写这条，她一问就靠常识把全书讲完（11-01 实测）。

    零模型调用：数字来自状态文件。同 fact_id 原地覆盖，永远只有一条。
    """
    from persona_growth import passes_persona_bottom_line
    text = f"我在读{title}，读到第 {read_upto}/{total} 块，后面的还没读到。"
    if not passes_persona_bottom_line(text, facet=f"book:{book}:progress"):
        return 0
    if not hasattr(l2, "set_persona_fact_value"):
        return 0
    return await l2.set_persona_fact_value(
        user_id, f"book_{book}_progress", text,
        kind="preference", facet_key=f"book:{book}:progress")


# ── 一轮 ──────────────────────────────────────────────────────────────────

async def run_round(l2, user_id: str, *, book: str, title: str, chunks: list[dict],
                    state: dict, keep: int, dry_run: bool) -> dict:
    total = len(chunks)
    reads = state.get("reads") or []
    decision = await ask_her(reads, total, title)
    record: dict = {"at": datetime.now(timezone.utc).isoformat(),
                    "choice": decision["choice"], "reason": decision["reason"]}

    if decision["choice"] == "stop":
        record["stage"] = "chose_stop"
        return record

    if decision["choice"] == "reread" and reads:
        want = decision.get("chunk") or 0
        index = want - 1 if want else reads[-1]["chunk"]
        if index not in {r["chunk"] for r in reads}:
            record["note"] = f"她点的是第{want}块，那块没读过，改回上次那块"
            index = reads[-1]["chunk"]
    else:
        read_set = {r["chunk"] for r in reads}
        index = max(read_set) + 1 if read_set else 0
        if index >= total:
            record["stage"] = "no_more_chunks"
            return record

    chunk = chunks[index]
    facts = []
    if hasattr(l2, "list_companion_persona_facts"):
        try:
            facts = await l2.list_companion_persona_facts(user_id, limit=120) or []
        except Exception:
            facts = []
    take = await write_take(title, chunk, total, reads, persona_facts=facts,
                            view=str(state.get("view") or ""))
    if not str(take.get("take") or "").strip():
        # 两次都没出合格 JSON：这一轮不写库、不推进进度，也别把原始串当她的话。
        record.update({"stage": "no_take", "chunk": index, "raw": take.get("raw") or ""})
        return record
    if take.get("quote") and not take.get("quote_verified"):
        # 引文对不上原文 = 她编了一句"原文"，宁可不存这句，也不把编的当证据。
        take["quote_dropped"] = take["quote"]
        take["quote"] = ""
    if not take.get("quote"):
        take["quote"] = _clean_evidence(chunk["text"])
        take["quote_fallback"] = True

    from persona_growth import passes_persona_bottom_line
    if not passes_persona_bottom_line(take["take"], evidence=take["quote"], facet=f"book:{book}"):
        record.update({"stage": "blocked_by_bottom_line", "chunk": index,
                       "take": take["take"][:200]})
        return record

    fact = build_fact(book, index, take)
    written = 0
    demoted: list[str] = []
    if not dry_run:
        # 同一块重读一次就换一个说法：先按 source_conv_id 清掉这一块旧的那行再写，
        # 不然 ON CONFLICT DO NOTHING 会让她最早那次、最生的版本永远赖着不走。
        if hasattr(l2, "purge_companion_persona_source"):
            try:
                await l2.purge_companion_persona_source(user_id, fact["source_conv_id"])
            except Exception as exc:
                print(f"[read_book] purge warn {type(exc).__name__}: {exc}", flush=True)
        written = await l2.upsert_companion_persona_facts(
            user_id, [fact], guard_identity=False, source_conv_id=fact["source_conv_id"])
        try:
            demoted = await keep_recent_takes(l2, user_id, book, keep)
        except Exception as exc:  # 降级失败不该把这一轮连带作废：新行已经写进去了
            print(f"[read_book] keep_recent_takes warn {type(exc).__name__}: {exc}", flush=True)
        reads = [r for r in reads if r["chunk"] != index] + [{
            "chunk": index, "total": total, "chars": chunk["chars"],
            "take": take["take"], "quote": take["quote"],
            "quote_fallback": take["quote_fallback"], "at": record["at"],
            "fact_id": fact["fact_id"],
        }]
        reads.sort(key=lambda r: r["chunk"])
        state["reads"] = reads
        state["cursor"] = max(r["chunk"] for r in reads) + 1
        state["total"] = total
        # 「读到第几块」必须是她自己的事实：她只知道自己读到哪，才不会一开口就把全书讲完。
        try:
            if await save_reading_progress(l2, user_id, book, title, len(reads), total):
                record["progress"] = f"{len(reads)}/{total}"
        except Exception as exc:
            print(f"[read_book] progress warn {type(exc).__name__}: {exc}", flush=True)
        # 整本印象：每读 VIEW_EVERY 块重写一次（1 次调用，不在回复热路径）；
        # 还没写过就先补一版，否则对话里她只有「最近一块」可讲。
        try:
            due = (len(reads) % VIEW_EVERY == 0) or (
                not state.get("view") and len(reads) >= VIEW_MIN)
            if due:
                view_text = await write_book_view(
                    title, reads, str(state.get("view") or ""), total)
                if await save_book_view(l2, user_id, book, view_text):
                    state["view"] = view_text
                    state["view_at"] = record["at"]
                    state["view_chunks"] = len(reads)
                    record["view"] = view_text
        except Exception as exc:
            print(f"[read_book] view warn {type(exc).__name__}: {exc}", flush=True)

    if written and not dry_run:
        # 读书也是一段经历：写进和 B站/起点 同一条 life_items 管道，
        # 这样她说「今天读了什么」有事实可依，不用靠模型编。
        try:
            await l2.append_life_item(user_id, {
                "text": f"读{title if '《' in title else '《' + title + '》'}读到第 {index + 1} 块：{take['take'][:160]}",
                "kind": "reading", "source": f"book:{book}",
                "ref": f"book:{book}:c{index + 1}", "title": title, "topic": book,
                "depth": 1.1,
            }, days=7)
        except Exception as exc:
            print(f"[read_book] life_item warn {type(exc).__name__}: {exc}", flush=True)

    record.update({"stage": "done", "chunk": index, "chars": chunk["chars"],
                   "take": take["take"], "quote": take["quote"],
                   "quote_fallback": take["quote_fallback"], "raw_ok": take["raw_ok"],
                   "quote_dropped": take.get("quote_dropped") or "",
                   "fact_id": fact["fact_id"], "written": written, "demoted": demoted})
    return record


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="huozhe")
    ap.add_argument("--title", default="")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--keep-active", type=int, default=0,
                    help="最多留几块块级感想 active（默认 0：只留「读到哪 + 整体印象」进人格包）")
    ap.add_argument("--refresh-view", action="store_true",
                    help="开跑前先按已读的块重写一次整本印象")
    ap.add_argument("--source", default="", help="原文路径，默认 .runtime/books/<book>.txt")
    ap.add_argument("--rebuild-chunks", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只读+只调模型，不写记忆库")
    ap.add_argument("--gap-min", type=float, default=10.0,
                    help="两块之间最少隔多少分钟（真实时间）。读一块不是读一页，别一口气刷；批量补读可传 0")
    args = ap.parse_args()

    _gates_off()
    BOOKS.mkdir(parents=True, exist_ok=True)
    chunks_path = BOOKS / f"{args.book}-chunks.json"
    state_path = BOOKS / f"{args.book}-state.json"
    journal = BOOKS / f"{args.book}-read.jsonl"
    source = pathlib.Path(args.source) if args.source else BOOKS / f"{args.book}.txt"

    if args.rebuild_chunks or not chunks_path.exists():
        if not source.exists():
            raise SystemExit(f"找不到原文：{source}")
        chunks = build_chunks(args.book, source, chunks_path)
        print(json.dumps({"built": len(chunks),
                          "chars": [c["chars"] for c in chunks][:8],
                          "avg": sum(c["chars"] for c in chunks) // len(chunks)},
                         ensure_ascii=False), flush=True)
    else:
        payload = json.loads(chunks_path.read_text(encoding="utf-8-sig"))
        chunks = payload["chunks"]

    title = args.title or next(
        iter(split_paragraphs(load_book(source))) if source.exists() else iter([args.book]), args.book)
    state = load_state(state_path, args.book, title)
    state["title"] = title

    from memory.l2_postgres import L2PostgresMemory
    l2 = L2PostgresMemory()
    try:
        # 库连不上要在花模型调用之前就报出来：读一块要 3 次调用，白跑两轮太亏（11-01 踩过两次）。
        await l2.list_companion_persona_facts(args.user, limit=1)
    except Exception as exc:
        raise SystemExit(
            f"本地记忆库连不上（{type(exc).__name__}: {exc}）。先 `docker ps` 看有没有 lang-tutor-pg、"
            f"15432 是否在听；没起就跑 deployment/start-local-db.ps1。")
    try:
        if args.refresh_view and not args.dry_run and state.get("reads"):
            text = await write_book_view(title, state["reads"],
                                         str(state.get("view") or ""), len(chunks))
            if await save_book_view(l2, args.user, args.book, text):
                state["view"] = text
                state["view_at"] = datetime.now(timezone.utc).isoformat()
                state["view_chunks"] = len(state["reads"])
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
                print(json.dumps({"整本印象": text}, ensure_ascii=False, indent=1), flush=True)
        for i in range(args.rounds):
            record = await run_round(l2, args.user, book=args.book, title=title,
                                     chunks=chunks, state=state,
                                     keep=args.keep_active, dry_run=args.dry_run)
            record["round"] = i + 1
            with journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps(record, ensure_ascii=False, indent=1), flush=True)
            if record["stage"] in {"chose_stop", "no_more_chunks",
                                   "blocked_by_bottom_line", "no_take"}:
                break
            if not args.dry_run:
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
            # 两块之间必须隔真实时间：一口气连读会把「感受」压成情节复述。
            if i < args.rounds - 1 and args.gap_min > 0:
                print(f"[read_book] 下一块等 {args.gap_min:g} 分钟……", flush=True)
                await asyncio.sleep(args.gap_min * 60)
    finally:
        pool = getattr(l2, "_pool", None)
        if pool is not None:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
