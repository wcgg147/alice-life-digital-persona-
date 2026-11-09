# -*- coding: utf-8 -*-
"""爱丽丝网文阅读（起点中文网 + 番茄小说）登录态书架、正文试读、本章说热议与互动底座。

职责：
1. 真实登录态书架读取（同步追更进度、感应最新章节与更新）+ 榜单淘书混排；
2. 二阶段真实点进章节阅读：读取书籍简介、章节正文节选、以及起点『本章说』热议段落（绝不只看书名猜剧情）；
3. 真实书架操作与每日免费互动（加入书架 / 投递起点免费推荐票 / 弃坑标记，严禁任何付费订阅与打赏）；
4. 三层人格底线防火墙联动（过滤低俗擦边、极端戾气、剧毒降智，绝不污染爱丽丝人格）；
5. 结果规范沉淀至 companion_life_items、media_taste:qidian / media_taste:fanqie 与独立人格成长。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
COOKIE_PATH = _ROOT / ".runtime" / "novel_cookies.json"
_INTERACT_LOG = _ROOT / ".runtime" / "alice-loop" / "novel_interactions.jsonl"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

NOVEL_CHOOSE_SYSTEM = "\n".join([
    "你就是 Alice。你正在看自己的小说书架和热门榜单（起点中文网 / 番茄小说）。",
    "先对**每一部**说一句真心话：你会不会点开看章节正文，为什么。要说真话，可以因为『书名太套路』『不想看系统流』『最近更得慢』直接跳过。",
    "然后从里面挑**一本**你此刻真的最想点开看正文的（一部都不想看就 pick=null）。",
    "挑中之后，说明你的阅读意图 action：",
    "- 'track'：追看书架里正在追的书；",
    "- 'try_new'：从榜单或书架里点开一本还没细看过的书试读开篇；",
    "- 'vote'：不仅想看，还想顺手给作者投张免费推荐票支持；",
    "- 'drop'：点开核实后如果还是觉得毒就准备弃坑。",
    "⛔ 审美品味要求：",
    "- 喜欢有逻辑的世界观构建、伏笔收束、年代质感、立体配角和自然幽默感；",
    "- 讨厌无脑装逼打脸、战力崩塌、降智强行拉仇恨、以及无意义的凑字数水文；",
    "- 如果一本书还没点开读过正文，在二阶段读完正文前不要假装自己已经背熟了后面的剧情。",
    '只输出严格 JSON：{"judgments":[{"i":1,"open":true/false,"why":"一句话"}],'
    '"pick":序号或null,"why_pick":"一句话","action":"track/try_new/drop/vote",'
    '"recall":"...","view":"...","topic":"...","detail":"...","aliases":"..."}',
])

NOVEL_OPINION_SYSTEM = "\n".join([
    "你就是 Alice，刚在小说平台（起点中文网或番茄小说）点开一本书，读完了作品简介、真实章节正文节选以及读者的『本章说/段评』互动。",
    "请严格基于你刚刚读到的真实章节内容与简介，输出五项阅读笔记（自然中文）：",
    "① recall：以后跟朋友闲聊提起这本书时，你会怎么随口一句话带出来（20-38字，口语自然，带上平台与书名，比如『最近在起点追《…》，里面写到…』，别像官方简介）；",
    "② view：你读完这章正文后的独立看法 1-2 句（≤65字，针对这章的具体情节细节、人物对话节奏、年代/世界观质感或槽点发表真实感受，不写套话）；",
    "③ topic：2-4 个字的网文分类标签（如：年代文、仙侠修真、克系悬疑、历史军事、轻小说、都市异能、古言奇幻）；",
    "④ detail：90-160 字的具体章节内容备忘（记下刚才读到的这章里具体发生了什么事、出场人物是谁、有哪些让你印象深刻的细节或本章说高赞热议点，供日后细聊时核对；严禁超出给定章节材料编造）；",
    "⑤ aliases：4-7 个检索词，空格分隔（必须包含书名、作者、主角名、核心题材词、起点或番茄）。",
    "⛔ 严禁把小说角色的经历当成你自己的现实经历；只根据刚读到的章节实话实说。",
    '只输出严格 JSON：{"recall":"...","view":"...","topic":"...","detail":"...","aliases":"..."}',
])

NOVEL_MERGE_TASTE_SYSTEM = "\n".join([
    "下面是一个人的『看网络小说口味』已有描述（可能为空），以及她今天翻书架和读章节后的取舍判断。",
    "把已有的和今天的**合并重写**成一段 60-110 字的网文口味自述：",
    "① 只写稳定的题材偏好、喜欢的叙事节奏与反感的毒点（例如偏爱考据扎实的历史年代与逻辑自洽的仙侠悬疑，反感无脑装逼与降智套路），不报流水账书单；",
    "② 硬上限 130 字；没新变化就原样输出已有描述；",
    "③ 绝不写入任何低俗、戾气、虐杀或极端倾向。",
    "只输出这一段本身。",
])


def enabled() -> bool:
    try:
        from config import ALICE_NOVEL_ENABLED
        return bool(ALICE_NOVEL_ENABLED)
    except Exception:
        return str(os.getenv("ALICE_NOVEL_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}


def interact_enabled() -> bool:
    try:
        from config import ALICE_NOVEL_INTERACT_ENABLED
        return bool(ALICE_NOVEL_INTERACT_ENABLED)
    except Exception:
        return str(os.getenv("ALICE_NOVEL_INTERACT_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}


def load_novel_cookies() -> dict[str, Any]:
    """读取本地受 gitignore 保护的凭证文件。"""
    if not COOKIE_PATH.exists():
        return {}
    try:
        return json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_configured() -> bool:
    raw = load_novel_cookies()
    return bool(raw.get("qidian") or raw.get("fanqie"))


def clean_fanqie_pua(text: str) -> str:
    """剔除番茄小说自定义 WebFont 映射至 Unicode 私用区（PUA \ue000-\uf8ff）的乱码字符，保留可读汉字与标点。"""
    if not text:
        return ""
    cleaned = re.sub(r"[\ue000-\uf8ff]+", "", str(text))
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _get_qidian_playwright_cookies(raw: dict[str, Any]) -> list[dict[str, Any]]:
    qd = raw.get("qidian", {})
    cookies = []
    for k, v in qd.items():
        if v:
            cookies.append({
                "name": str(k),
                "value": str(v),
                "domain": ".qidian.com",
                "path": "/",
            })
    return cookies


def _get_fanqie_playwright_cookies(raw: dict[str, Any]) -> list[dict[str, Any]]:
    fq = raw.get("fanqie", {})
    cookies = []
    for k, v in fq.items():
        if v:
            cookies.append({
                "name": str(k),
                "value": str(v),
                "domain": ".fanqienovel.com",
                "path": "/",
            })
    return cookies


def fetch_qidian_books(cookies: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """获取起点登录书架（前 N-2 本，随机抽样混排）+ 月票榜新书候选（2 本），兼顾追更与淘书加入书架。"""
    from playwright.sync_api import sync_playwright

    shelf_books: list[dict[str, Any]] = []
    rank_books: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            ctx = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800})
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()

            if cookies:
                try:
                    page.goto("https://my.qidian.com/bookcase", timeout=22000, wait_until="networkidle")
                    page.wait_for_timeout(1800)
                    rows = page.evaluate("""() => {
                        const trs = Array.from(document.querySelectorAll('tr')).slice(1);
                        return trs.map(tr => {
                            const input = tr.querySelector('input[name="bookname"]');
                            const titleEl = tr.querySelector('a[data-eid="qd_M198"], a[href*="/book/"]');
                            const catEl = tr.querySelector('.fen-category');
                            const chapterEl = tr.querySelector('.shelf-table-chapter');
                            const authorEl = tr.querySelector('.shelf-table-author');
                            const progressEl = tr.querySelector('.col5 a, .spTips');
                            const updateEl = tr.querySelector('.col3');
                            if (!titleEl) return null;
                            const title = titleEl.innerText.trim();
                            const href = titleEl.href.startsWith('//') ? 'https:' + titleEl.href : titleEl.href;
                            const chHref = (chapterEl && chapterEl.href)
                                ? (chapterEl.href.startsWith('//') ? 'https:' + chapterEl.href : chapterEl.href)
                                : '';
                            return {
                                platform: 'qidian',
                                source_kind: 'bookshelf',
                                bid: input ? input.value : '',
                                title,
                                category: catEl ? catEl.innerText.replace(/[「」]/g, '').trim() : '',
                                author: authorEl ? authorEl.innerText.trim() : '',
                                latest_chapter: chapterEl ? chapterEl.innerText.trim() : '',
                                chapter_url: chHref,
                                progress: progressEl ? progressEl.innerText.trim() : '',
                                update_time: updateEl ? updateEl.innerText.trim() : '',
                                url: href,
                            };
                        }).filter(Boolean);
                    }""")
                    for r in rows:
                        if r.get("title"):
                            shelf_books.append(r)
                except Exception as e:
                    print(f"[novel] qidian bookcase fetch warn: {e}", flush=True)

            # 顺手拉 2-3 本月票榜作品，供她挑中后真正执行「加入书架」
            try:
                page.goto("https://www.qidian.com/rank/yuepiao/", timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(1800)
                rank_items = page.evaluate("""() => {
                    const items = Array.from(document.querySelectorAll('.book-mid-info, .rank-book-list li'));
                    return items.map(el => {
                        const titleEl = el.querySelector('h2 a, .book-mid-info h2 a, a[href*="/book/"]');
                        const authorEl = el.querySelector('.writer, a.name, .author a');
                        const introEl = el.querySelector('.intro, p.intro');
                        const tagEl = el.querySelector('.tag span, .tag a, p.author a');
                        if (!titleEl) return null;
                        const href = titleEl.href.startsWith('//') ? 'https:' + titleEl.href : titleEl.href;
                        const m = href.match(/\\/book\\/(\\d+)/);
                        return {
                            platform: 'qidian',
                            source_kind: 'ranking',
                            bid: m ? m[1] : '',
                            title: titleEl.innerText.trim(),
                            author: authorEl ? authorEl.innerText.trim() : '',
                            intro: introEl ? introEl.innerText.trim().slice(0, 160) : '',
                            category: tagEl ? tagEl.innerText.trim() : '',
                            url: href,
                        };
                    }).filter(Boolean);
                }""")
                shelf_titles = {b.get("title") for b in shelf_books}
                for r in rank_items:
                    if r.get("title") and r["title"] not in shelf_titles:
                        rank_books.append(r)
            except Exception as e:
                print(f"[novel] qidian rank fetch warn: {e}", flush=True)
        finally:
            browser.close()

    shelf_quota = max(1, limit - 2) if rank_books else limit
    picked_shelf = shelf_books[:shelf_quota]
    picked_rank = rank_books[: max(0, limit - len(picked_shelf))]
    return (picked_shelf + picked_rank)[:limit]


def fetch_fanqie_books(cookies: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """获取番茄登录书架 + 巅峰榜候选。"""
    from playwright.sync_api import sync_playwright

    shelf_books: list[dict[str, Any]] = []
    rank_books: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            ctx = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800})
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()

            if cookies:
                try:
                    page.goto("https://fanqienovel.com/bookshelf", timeout=22000, wait_until="networkidle")
                    page.wait_for_timeout(1800)
                    shelf_items = page.evaluate("""() => {
                        const cards = Array.from(document.querySelectorAll('.book-card__normal'));
                        return cards.map((c, idx) => {
                            const titleEl = c.querySelector('.book-title');
                            const progEl = c.querySelector('.book-progress');
                            if (!titleEl) return null;
                            return {
                                platform: 'fanqie',
                                source_kind: 'bookshelf',
                                shelf_index: idx,
                                title: titleEl.innerText.trim(),
                                progress: progEl ? progEl.innerText.trim() : '',
                            };
                        }).filter(Boolean);
                    }""")
                    for item in shelf_items:
                        t = item.get("title")
                        if t and not any(b["title"] == t for b in shelf_books):
                            shelf_books.append({
                                **item,
                                "author": "",
                                "url": f"https://fanqienovel.com/bookshelf#{t}",
                            })
                except Exception as e:
                    print(f"[novel] fanqie bookcase fetch warn: {e}", flush=True)

            try:
                page.goto("https://fanqienovel.com/rank", timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(1800)
                rank_items = page.evaluate("""() => {
                    const links = Array.from(document.querySelectorAll('a[href*="/page/"]'));
                    return links.map(a => {
                        const title = a.innerText.trim().split('\\n')[0];
                        if (!title || title.length < 2) return null;
                        return {
                            platform: 'fanqie',
                            source_kind: 'ranking',
                            title,
                            url: a.href,
                        };
                    }).filter(Boolean);
                }""")
                shelf_titles = {b.get("title") for b in shelf_books}
                for item in rank_items:
                    t = item.get("title")
                    if t and t not in shelf_titles and not any(b["title"] == t for b in rank_books):
                        rank_books.append(item)
            except Exception as e:
                print(f"[novel] fanqie rank fetch warn: {e}", flush=True)
        finally:
            browser.close()

    shelf_quota = max(1, limit - 2) if rank_books else limit
    picked_shelf = shelf_books[:shelf_quota]
    picked_rank = rank_books[: max(0, limit - len(picked_shelf))]
    return (picked_shelf + picked_rank)[:limit]


def _extract_qidian_chapter_and_hots(raw_text: str) -> tuple[str, list[str]]:
    """从起点阅读页文本中提取干净正文节选与『本章说』高热段落。"""
    lines = [ln.strip() for ln in str(raw_text or "").splitlines() if ln.strip()]
    # 过滤顶部导航噪点
    skip_exact = {"首页", "书架", "客户端", "游戏", "手机阅读", "举报", "章节报错", "上一章", "目录", "下一章", "按 [ 空格键 ] 即可快速翻页"}
    cleaned_lines: list[str] = []
    hot_paras: list[tuple[int, str]] = []

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln in skip_exact or ln.startswith(("书名：", "作者：", "字数：", "更新时间：", "发布时间：")):
            i += 1
            continue
        # 起点每段后面常跟一个纯数字行，代表该段的『本章说』评论数
        if i + 1 < len(lines) and re.fullmatch(r"\d{1,5}", lines[i + 1]):
            cnt = int(lines[i + 1])
            if len(ln) >= 6:
                cleaned_lines.append(ln)
                if cnt >= 15:
                    hot_paras.append((cnt, f"「{ln[:75]}」（{cnt}条本章说热议）"))
            i += 2
            continue
        if not re.fullmatch(r"\d{1,5}", ln):
            cleaned_lines.append(ln)
        i += 1

    hot_paras.sort(key=lambda x: x[0], reverse=True)
    top_hots = [txt for _, txt in hot_paras[:5]]
    body_excerpt = "\n".join(cleaned_lines)[:1500]
    return body_excerpt, top_hots


def fetch_qidian_book_deep(
    cookies: list[dict[str, Any]],
    book: dict[str, Any],
    *,
    action: str = "track",
    do_interact: bool = True,
) -> dict[str, Any]:
    """点进起点单本书详情页与真实章节阅读页，提取简介、正文、本章说热议点，并按需执行真实书架/推荐票互动。"""
    from playwright.sync_api import sync_playwright

    bid = str(book.get("bid") or "").strip()
    url = str(book.get("url") or "").strip()
    if not bid and "/book/" in url:
        m = re.search(r"/book/(\d+)", url)
        if m:
            bid = m.group(1)
    book_url = f"https://www.qidian.com/book/{bid}/" if bid else url

    out: dict[str, Any] = {
        "title": str(book.get("title") or ""),
        "author": str(book.get("author") or ""),
        "category": str(book.get("category") or ""),
        "intro": str(book.get("intro") or ""),
        "chapter_title": "",
        "chapter_excerpt": "",
        "hot_paragraphs": [],
        "url": book_url,
        "executed_actions": {},
    }
    if not book_url:
        return out

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            ctx = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800})
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()

            page.goto(book_url, timeout=22000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            detail = page.evaluate("""() => {
                const title = document.querySelector('#bookName, h1 em')?.innerText?.trim() || '';
                const author = document.querySelector('.writer-name, a.writer, .book-info .writer')?.innerText?.trim() || '';
                const intro = document.querySelector('#book-intro-detail, .book-intro, .intro')?.innerText?.trim() || '';
                const readBtn = document.querySelector('#readBtn, a[href*="/chapter/"]');
                const addBtn = document.querySelector('#addBookBtn');
                const recBtn = document.querySelector('#recBtn');
                return {
                    title,
                    author,
                    intro: intro.slice(0, 400),
                    read_url: readBtn ? (readBtn.href.startsWith('//') ? 'https:' + readBtn.href : readBtn.href) : '',
                    add_btn_text: addBtn ? addBtn.innerText.trim() : '',
                    has_rec_btn: !!recBtn,
                };
            }""")
            if detail.get("title"):
                out["title"] = detail["title"]
            if detail.get("author"):
                out["author"] = detail["author"]
            if detail.get("intro"):
                out["intro"] = detail["intro"]

            # 真实互动 1：榜单淘到的新书，点『加入书架』
            if do_interact and cookies and action in ("try_new", "track", "vote"):
                if detail.get("add_btn_text") == "加入书架":
                    try:
                        page.click("#addBookBtn", timeout=4000)
                        page.wait_for_timeout(1200)
                        after_txt = page.evaluate("() => document.querySelector('#addBookBtn')?.innerText?.trim() || ''")
                        out["executed_actions"]["added_to_shelf"] = ("已在书架" in after_txt or after_txt != "加入书架")
                    except Exception:
                        pass

            # 真实互动 2：投递起点免费推荐票（绝不碰月票/打赏/付费订阅）
            if do_interact and cookies and action == "vote" and detail.get("has_rec_btn"):
                try:
                    page.click("#recBtn", timeout=4000)
                    page.wait_for_timeout(1200)
                    # 若弹出推荐票确认框，尝试点击免费推荐票确认按钮
                    voted = page.evaluate("""() => {
                        const btns = Array.from(document.querySelectorAll('a, button'));
                        const confirmBtn = btns.find(b => {
                            const t = (b.innerText || '').trim();
                            return t === '投推荐票' || t === '确认投推荐票' || t === '确定';
                        });
                        if (confirmBtn) { confirmBtn.click(); return true; }
                        return false;
                    }""")
                    page.wait_for_timeout(800)
                    out["executed_actions"]["rec_ticket"] = bool(voted)
                except Exception:
                    pass

            # 进入真实章节阅读正文（优先读免费首章/当前进度章，确保能读到完整正文与本章说）
            read_url = str(detail.get("read_url") or book.get("chapter_url") or "").strip()
            if read_url:
                try:
                    page.goto(read_url, timeout=22000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2200)
                    chap_raw = page.evaluate("""() => {
                        const h = document.querySelector('h1, .chapter-title, .j_chapterName')?.innerText?.trim() || '';
                        const main = document.querySelector('main, .read-content, article');
                        return {
                            chapter_title: h,
                            text: main ? main.innerText : document.body.innerText.slice(0, 4000),
                        };
                    }""")
                    out["chapter_title"] = str(chap_raw.get("chapter_title") or "")
                    excerpt, hots = _extract_qidian_chapter_and_hots(str(chap_raw.get("text") or ""))
                    out["chapter_excerpt"] = excerpt
                    out["hot_paragraphs"] = hots
                except Exception as e:
                    print(f"[novel] qidian chapter read warn: {e}", flush=True)
        finally:
            browser.close()

    return out


def fetch_fanqie_book_deep(
    cookies: list[dict[str, Any]],
    book: dict[str, Any],
    *,
    action: str = "track",
    do_interact: bool = True,
) -> dict[str, Any]:
    """点进番茄小说书架卡片或详情页，提取无加密简介、作者与清洗 PUA 后的章节正文节选。"""
    from playwright.sync_api import sync_playwright

    title = str(book.get("title") or "").strip()
    url = str(book.get("url") or "").strip()
    out: dict[str, Any] = {
        "title": title,
        "author": str(book.get("author") or ""),
        "category": str(book.get("category") or ""),
        "intro": str(book.get("intro") or ""),
        "chapter_title": "",
        "chapter_excerpt": "",
        "hot_paragraphs": [],
        "url": url,
        "executed_actions": {},
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            ctx = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800})
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()

            book_page_url = url if "/page/" in url else ""
            reader_url = ""

            # 若来自书架，先在书架页点开对应卡片进入阅读器
            if book.get("source_kind") == "bookshelf" and cookies:
                try:
                    page.goto("https://fanqienovel.com/bookshelf", timeout=22000, wait_until="networkidle")
                    page.wait_for_timeout(1800)
                    clicked = page.evaluate("""(targetTitle) => {
                        const cards = Array.from(document.querySelectorAll('.book-card__normal'));
                        const card = cards.find(c => c.querySelector('.book-title')?.innerText?.trim() === targetTitle) || cards[0];
                        if (!card) return false;
                        card.click();
                        return true;
                    }""", title)
                    if clicked:
                        page.wait_for_timeout(2500)
                        if "/reader/" in page.url:
                            reader_url = page.url
                        r_info = page.evaluate("""() => {
                            const pageLink = document.querySelector('a[href*="/page/"]');
                            const chTitle = document.querySelector('h1, .muye-reader-title')?.innerText?.trim() || '';
                            const content = document.querySelector('.muye-reader-content, article, main');
                            return {
                                page_url: pageLink ? pageLink.href : '',
                                chapter_title: chTitle,
                                raw_text: content ? content.innerText : document.body.innerText.slice(0, 3500),
                            };
                        }""")
                        if r_info.get("page_url"):
                            book_page_url = r_info["page_url"]
                            out["url"] = book_page_url
                        out["chapter_title"] = clean_fanqie_pua(str(r_info.get("chapter_title") or ""))
                        out["chapter_excerpt"] = clean_fanqie_pua(str(r_info.get("raw_text") or ""))[:1400]
                except Exception as e:
                    print(f"[novel] fanqie bookshelf click warn: {e}", flush=True)

            # 访问书籍详情页（/page/{book_id}），获取完全未加密的作者名、分类标签与完整简介
            if book_page_url:
                try:
                    page.goto(book_page_url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(1800)
                    p_info = page.evaluate("""() => {
                        const t = document.querySelector('.info-name h1, h1')?.innerText?.trim() || '';
                        const author = document.querySelector('.author-name-text, .author-name')?.innerText?.trim() || '';
                        const cat = Array.from(document.querySelectorAll('.info-label span')).map(s => s.innerText.trim()).join('·');
                        const intro = document.querySelector('.page-abstract-content, .abstract')?.innerText?.trim() || '';
                        const firstCh = document.querySelector('a[href*="/reader/"]');
                        return {
                            title: t,
                            author,
                            category: cat,
                            intro: intro.slice(0, 400),
                            first_reader_url: firstCh ? firstCh.href : '',
                        };
                    }""")
                    if p_info.get("title"):
                        out["title"] = p_info["title"]
                    if p_info.get("author"):
                        out["author"] = p_info["author"]
                    if p_info.get("category"):
                        out["category"] = p_info["category"]
                    if p_info.get("intro"):
                        out["intro"] = p_info["intro"]
                    if not reader_url and p_info.get("first_reader_url"):
                        reader_url = p_info["first_reader_url"]

                    # 若来自榜单且决定试读/追更，尝试在详情页点击『加入书架』
                    if do_interact and cookies and book.get("source_kind") == "ranking" and action in ("try_new", "track"):
                        added = page.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('button, div, span, a'));
                            const addBtn = btns.find(b => (b.innerText || '').trim() === '加入书架');
                            if (addBtn) { addBtn.click(); return true; }
                            return false;
                        }""")
                        if added:
                            page.wait_for_timeout(1000)
                            out["executed_actions"]["added_to_shelf"] = True
                except Exception as e:
                    print(f"[novel] fanqie page detail warn: {e}", flush=True)

            # 若还没有读到章节正文且拿到了 reader_url，点进第一章读正文
            if not out["chapter_excerpt"] and reader_url:
                try:
                    page.goto(reader_url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    r_info = page.evaluate("""() => {
                        const chTitle = document.querySelector('h1, .muye-reader-title')?.innerText?.trim() || '';
                        const content = document.querySelector('.muye-reader-content, article, main');
                        return {
                            chapter_title: chTitle,
                            raw_text: content ? content.innerText : document.body.innerText.slice(0, 3500),
                        };
                    }""")
                    out["chapter_title"] = clean_fanqie_pua(str(r_info.get("chapter_title") or ""))
                    out["chapter_excerpt"] = clean_fanqie_pua(str(r_info.get("raw_text") or ""))[:1400]
                except Exception as e:
                    print(f"[novel] fanqie reader warn: {e}", flush=True)
        finally:
            browser.close()

    return out


def format_novel_deep_body(deep: dict[str, Any]) -> str:
    """将详情简介、章节正文与本章说高热段落组装成供二阶段写读后感的上下文。"""
    parts = []
    if deep.get("author") or deep.get("category"):
        parts.append(f"作者：{deep.get('author') or '未知'}｜分类：{deep.get('category') or '网文'}")
    if deep.get("intro"):
        parts.append(f"作品简介：\n{deep['intro']}")
    if deep.get("chapter_title") or deep.get("chapter_excerpt"):
        ch_head = f"（{deep['chapter_title']}）" if deep.get("chapter_title") else ""
        parts.append(f"本次点开阅读的章节正文节选{ch_head}：\n{deep.get('chapter_excerpt') or ''}")
    hots = deep.get("hot_paragraphs") or []
    if hots:
        parts.append("本章说/段评读者热议最高的段落：\n" + "\n".join(f"- {h}" for h in hots))
    return "\n\n".join(parts).strip()


async def choose_novel_round(candidates: list[dict[str, Any]], taste: str = "", persona_seeds: str = "") -> dict[str, Any]:
    """一阶段：调用大模型浏览书架与榜单，做出翻牌选择（关思考，零额外模型耗损）。"""
    from llm_client import robust_llm_client, thinking_disabled_extra_body
    try:
        from config import MEMORY_CURATOR_MODEL as model
    except Exception:
        model = None

    items_repr = []
    for i, c in enumerate(candidates, 1):
        src_label = "【我的书架】" if c.get("source_kind") == "bookshelf" else "【热门榜单】"
        plat = "起点" if c.get("platform") == "qidian" else "番茄"
        extra = []
        if c.get("author"):
            extra.append(f"作者:{c['author']}")
        if c.get("category"):
            extra.append(f"分类:{c['category']}")
        if c.get("latest_chapter"):
            extra.append(f"最新:{c['latest_chapter']}")
        if c.get("progress"):
            extra.append(f"进度:{c['progress']}")
        if c.get("intro"):
            extra.append(f"简介:{c['intro'][:90]}")
        tail = (" (" + " | ".join(extra) + ")") if extra else ""
        items_repr.append(f"{i}. [{plat}]{src_label} 《{c.get('title', '')}》{tail}")

    feed_str = "\n".join(items_repr)
    taste_str = f"\n你过往的网文阅读口味参考：{taste}" if taste else ""
    seed_str = f"\n你的人格倾向参考：{persona_seeds}" if persona_seeds else ""

    messages = [
        {"role": "system", "content": NOVEL_CHOOSE_SYSTEM},
        {
            "role": "user",
            "content": f"下面是此刻展示给你的小说选项：{taste_str}{seed_str}\n\n{feed_str}\n\n请输出你的选择与感想 JSON：",
        },
    ]

    raw = await robust_llm_client.chat_completion(
        messages,
        model=model,
        temperature=0.75,
        max_tokens=450,
        extra_body=thinking_disabled_extra_body(model),
    )
    text = str(raw or "").strip()
    data = None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                pass
    if isinstance(data, dict):
        return data
    return {"pick": 1 if candidates else None, "why_pick": "书名吸引我，准备点开看两眼", "action": "track", "recall": "", "view": ""}


async def write_novel_opinion(
    platform: str,
    book: dict[str, Any],
    body: str,
    *,
    fallback_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """二阶段：基于点开读到的真实章节正文与本章说热议，撰写接地气的读后感。"""
    from llm_client import robust_llm_client, thinking_disabled_extra_body
    try:
        from config import MEMORY_CURATOR_MODEL as model
    except Exception:
        model = None

    fb = dict(fallback_decision or {})
    if not body or len(body.strip()) < 30:
        return fb

    plat_name = "起点中文网" if platform == "qidian" else "番茄小说"
    user_prompt = (
        f"平台：{plat_name}\n"
        f"书名：《{book.get('title') or ''}》\n"
        f"来源：{'我的书架在追' if book.get('source_kind') == 'bookshelf' else '榜单新书试读'}\n\n"
        f"{body}\n\n"
        "请基于以上刚读到的真实章节内容输出 JSON："
    )
    try:
        raw = await robust_llm_client.chat_completion(
            [
                {"role": "system", "content": NOVEL_OPINION_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            temperature=0.7,
            max_tokens=420,
            extra_body=thinking_disabled_extra_body(model),
        )
        text = str(raw or "").strip()
        data = None
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    pass
        if isinstance(data, dict) and (data.get("view") or data.get("recall")):
            merged = dict(fb)
            for k in ("recall", "view", "topic", "detail", "aliases"):
                if data.get(k):
                    merged[k] = data[k]
            return merged
    except Exception as e:
        print(f"[novel] write_novel_opinion warn: {e}", flush=True)
    return fb


def build_novel_life_item(
    platform: str,
    book: dict[str, Any],
    written: dict[str, Any],
    *,
    executed_actions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成 companion_life_items 规范结构行。"""
    title = str(book.get("title") or "").strip()
    author = str(book.get("author") or "").strip()
    action = str(written.get("action") or "track").strip().lower()
    recall = str(written.get("recall") or "").strip().rstrip("。，,.!！；; ")
    view = str(written.get("view") or "").strip()
    topic = str(written.get("topic") or book.get("category") or "网文").strip()[:16]
    detail = str(written.get("detail") or book.get("intro") or "").strip()[:260]

    plat_name = "起点" if platform == "qidian" else "番茄"
    if recall:
        text = f"{recall}。{view}" if view else recall
    else:
        text = f"在{plat_name}看《{title}》，{view or '感觉设定和行文节奏都挺对我胃口'}。"

    extra_aliases = str(written.get("aliases") or "").strip()
    alias_tokens = [
        title, author, topic, plat_name, "小说", "追更", "书架", extra_aliases,
    ]
    aliases = " ".join(x for x in alias_tokens if x).strip()[:240]

    exec_acts = dict(executed_actions or {})
    actions: dict[str, Any] = {
        "like": action in ("vote", "track") or bool(exec_acts.get("rec_ticket")),
        "fav": action != "drop",
    }
    if action == "vote" or exec_acts.get("rec_ticket"):
        actions["vote"] = True
    if exec_acts.get("added_to_shelf"):
        actions["added_to_shelf"] = True
    if action == "drop":
        actions["dropped"] = True
    if book.get("progress"):
        actions["progress"] = str(book["progress"])

    ref = str(book.get("url") or f"https://www.{platform}.com/book/{title}").strip()[:200]
    seed_key = f"{platform}:{title}" if title else ref
    item_id = "novel_" + hashlib.sha1(seed_key.encode("utf-8")).hexdigest()[:10]

    return {
        "item_id": item_id,
        "text": text[:110],
        "kind": "reading",
        "aliases": aliases,
        "source": platform,
        "topic": topic,
        "ref": ref,
        "title": title[:160],
        "author": author[:80],
        "detail": detail,
        "actions_json": json.dumps(actions, ensure_ascii=False),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def save_novel_taste(
    l2: Any,
    shared_uid: str,
    platform: str,
    new_taste_val: str,
    source_conv_id: str = "",
) -> str:
    """持久化起点或番茄的阅读口味至 companion_persona_facts（兼容 PostgreSQL set_persona_fact_value 与测试桩）。"""
    from persona_growth import passes_persona_bottom_line

    val = str(new_taste_val or "").strip()[:220]
    if not val or not passes_persona_bottom_line(val, facet=f"media-taste-{platform}"):
        return ""
    fact_id = f"media_taste_{platform}"
    facet_key = f"media_taste:{platform}"
    try:
        if hasattr(l2, "set_persona_fact_value"):
            await l2.set_persona_fact_value(
                shared_uid, fact_id, val, kind="preference", facet_key=facet_key
            )
        else:
            await l2.upsert_companion_persona_facts(
                shared_uid,
                [{
                    "fact_id": fact_id,
                    "persona_key": "alice",
                    "kind": "preference",
                    "facet_key": facet_key,
                    "value": val,
                    "value_text": val,
                    "status": "active",
                    "source_kind": "reading",
                    "source_conv_id": source_conv_id,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }],
                source_conv_id=source_conv_id,
                guard_identity=False,
            )
        return val
    except Exception as e:
        print(f"[novel] save_novel_taste warn: {e}", flush=True)
        return ""


async def merge_novel_taste(
    l2: Any,
    shared_uid: str,
    platform: str,
    candidates: list[dict[str, Any]],
    judgments: list[dict[str, Any]],
    *,
    existing_taste: str = "",
    chosen_topic: str = "",
    chosen_view: str = "",
    source_conv_id: str = "",
) -> str:
    """将已有平台口味与本轮书架/榜单翻牌的取舍判断（含喜欢点开什么、反感跳过什么）合并重写，不重复单书具体剧情。"""
    from persona_growth import has_toxic_or_bottomless_exposure, passes_persona_bottom_line
    from alice_reading import _trim_taste
    from llm_client import robust_llm_client, thinking_disabled_extra_body
    try:
        from config import MEMORY_CURATOR_MODEL as model
    except Exception:
        model = None

    lines: list[str] = []
    for j in judgments or []:
        if not isinstance(j, dict):
            continue
        try:
            idx = int(j.get("i") or 0)
        except Exception:
            continue
        if not (1 <= idx <= len(candidates)):
            continue
        c = candidates[idx - 1]
        title = str(c.get("title") or "")
        cat = str(c.get("category") or "")
        why = str(j.get("why") or "").strip()
        if has_toxic_or_bottomless_exposure(f"{title}\n{why}"):
            continue
        act_str = "点开看" if j.get("open") else "跳过"
        cat_str = f"[{cat}]" if cat else ""
        lines.append(f"- {act_str} {cat_str}《{title}》：{why}")

    if chosen_topic or chosen_view:
        lines.append(f"- 读完正文后的偏好印证（{chosen_topic}）：{chosen_view}")

    if not lines:
        return existing_taste

    plat_zh = "起点中文网" if platform == "qidian" else "番茄小说"
    user_prompt = (
        f"平台：{plat_zh}\n"
        f"已有口味描述：{existing_taste or '（还没有）'}\n\n"
        f"今天的选书与排雷取舍：\n" + "\n".join(lines)
    )
    merged = ""
    try:
        raw = await robust_llm_client.chat_completion(
            [
                {"role": "system", "content": NOVEL_MERGE_TASTE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            temperature=0.5,
            max_tokens=220,
            extra_body=thinking_disabled_extra_body(model),
        )
        merged = _trim_taste(str(raw or "").strip(), 135)
    except Exception as e:
        print(f"[novel] merge_novel_taste llm warn: {e}", flush=True)

    if not merged or len(merged) < 8 or not passes_persona_bottom_line(merged, facet=f"media-taste-{platform}"):
        return existing_taste

    saved = await save_novel_taste(
        l2,
        shared_uid,
        platform,
        merged,
        source_conv_id=source_conv_id,
    )
    return saved or existing_taste


def _record_novel_interaction(row: dict[str, Any]) -> None:
    try:
        _INTERACT_LOG.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": datetime.now(timezone.utc).isoformat(), **row}
        with open(_INTERACT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def has_fresh_novel_item(l2: Any, user_id: str, hours: float | None = None) -> bool:
    """检查最近是否有网文阅读沉淀。"""
    from agent.life_line import detect_item_platform

    if hours is None:
        try:
            from config import ALICE_NOVEL_MIN_GAP_HOURS
            hours = float(ALICE_NOVEL_MIN_GAP_HOURS)
        except Exception:
            hours = 4.0
    try:
        items = await l2.list_life_items(user_id, limit=40)
        now = datetime.now(timezone.utc)
        for it in items:
            if detect_item_platform(it) in ("qidian", "fanqie"):
                created_str = str(it.get("created_at") or "")
                if created_str:
                    try:
                        ts = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if (now - ts).total_seconds() < hours * 3600:
                            return True
                    except Exception:
                        pass
        return False
    except Exception:
        return False


async def run_novel_reading_round(
    l2: Any,
    shared_uid: str,
    platform: str = "auto",
    *,
    force: bool = False,
) -> dict[str, Any]:
    """执行一次完整的二阶段网文阅读轮（书架/榜单翻牌 -> 点进章节读正文与本章说 -> 真实加书架/投推荐票 -> 存记忆与口味）。"""
    if not force and not enabled():
        return {"ok": False, "stage": "disabled"}

    raw_cookies = load_novel_cookies()
    if not raw_cookies:
        return {"ok": False, "stage": "no_cookies"}

    if not force and await has_fresh_novel_item(l2, shared_uid):
        return {"ok": False, "stage": "skip_recent"}

    qd_cookies = _get_qidian_playwright_cookies(raw_cookies)
    fq_cookies = _get_fanqie_playwright_cookies(raw_cookies)

    chosen_plat = platform
    if chosen_plat not in ("qidian", "fanqie"):
        if qd_cookies and fq_cookies:
            chosen_plat = "qidian" if random.random() < 0.5 else "fanqie"
        elif qd_cookies:
            chosen_plat = "qidian"
        elif fq_cookies:
            chosen_plat = "fanqie"
        else:
            return {"ok": False, "stage": "no_valid_cookies"}

    if chosen_plat == "qidian":
        candidates = await asyncio.to_thread(fetch_qidian_books, qd_cookies, 8)
    else:
        candidates = await asyncio.to_thread(fetch_fanqie_books, fq_cookies, 8)

    if not candidates:
        return {"ok": False, "stage": "no_candidates", "platform": chosen_plat}

    existing_taste = ""
    existing_fact_rows: list[dict[str, Any]] = []
    try:
        existing_fact_rows = await l2.list_companion_persona_facts(shared_uid, limit=80) or []
        for f in existing_fact_rows:
            fk = str(f.get("facet_key") or "")
            if fk == f"media_taste:{chosen_plat}":
                existing_taste = str(f.get("value_text") or f.get("value") or "")
                break
    except Exception:
        existing_taste = ""

    chosen_result = await choose_novel_round(candidates, taste=existing_taste)
    initial_judgments = list(chosen_result.get("judgments") or [])
    pick_idx = chosen_result.get("pick")
    if pick_idx is None or not (1 <= int(pick_idx) <= len(candidates)):
        return {"ok": True, "stage": "skipped_all", "platform": chosen_plat, "why": chosen_result.get("why_pick")}

    chosen_book = dict(candidates[int(pick_idx) - 1])
    action = str(chosen_result.get("action") or "track").strip().lower()

    # 二阶段：点进选中的书，读简介 + 真实章节正文 + 本章说高热段落，并按权限执行真实互动
    deep_info: dict[str, Any] = {}
    body = ""
    try:
        if chosen_plat == "qidian":
            deep_info = await asyncio.to_thread(
                fetch_qidian_book_deep,
                qd_cookies,
                chosen_book,
                action=action,
                do_interact=interact_enabled(),
            )
        else:
            deep_info = await asyncio.to_thread(
                fetch_fanqie_book_deep,
                fq_cookies,
                chosen_book,
                action=action,
                do_interact=interact_enabled(),
            )
    except Exception as e:
        print(f"[novel] deep fetch warn: {e}", flush=True)
        deep_info = {}

    if isinstance(deep_info, dict) and deep_info:
        for k in ("title", "author", "category", "intro", "url"):
            if deep_info.get(k):
                chosen_book[k] = deep_info[k]
        body = format_novel_deep_body(deep_info)

    if body:
        chosen_result = await write_novel_opinion(
            chosen_plat,
            chosen_book,
            body,
            fallback_decision=chosen_result,
        )

    # 三层底线防火墙拦截
    from persona_growth import (
        has_toxic_or_bottomless_exposure,
        is_hard_blocked_external_post,
        passes_persona_bottom_line,
    )
    content_to_check = (
        f"{chosen_book.get('title', '')}\n"
        f"{body}\n"
        f"{chosen_result.get('view', '')}\n"
        f"{chosen_result.get('detail', '')}"
    )
    blocked, block_reason = is_hard_blocked_external_post(content_to_check)
    v = str(chosen_result.get("view") or "").strip()
    r = str(chosen_result.get("recall") or "").strip()
    if blocked or (v and not passes_persona_bottom_line(v)) or (r and not passes_persona_bottom_line(r)):
        return {
            "ok": False,
            "stage": "blocked_by_bottom_line",
            "platform": chosen_plat,
            "book": chosen_book.get("title"),
            "reason": block_reason,
        }

    exec_acts = (deep_info.get("executed_actions") if isinstance(deep_info, dict) else None) or {}
    life_item = build_novel_life_item(
        chosen_plat,
        chosen_book,
        chosen_result,
        executed_actions=exec_acts,
    )
    await l2.append_life_item(shared_uid, life_item, days=30)

    if exec_acts:
        _record_novel_interaction({
            "user_id": shared_uid,
            "platform": chosen_plat,
            "book": chosen_book.get("title"),
            "action": action,
            "executed": exec_acts,
        })

    # 更新她在该小说平台的选书与排雷品味（合并本轮 8 本书的取舍判断，不重复单书具体剧情）
    topic = str(chosen_result.get("topic") or chosen_book.get("category") or "网文").strip()
    view = str(chosen_result.get("view") or "").strip()
    saved_taste = await merge_novel_taste(
        l2,
        shared_uid,
        chosen_plat,
        candidates,
        initial_judgments,
        existing_taste=existing_taste,
        chosen_topic=topic,
        chosen_view=view,
        source_conv_id=life_item["item_id"],
    )

    # 独立人格成长沉淀（有毒/无底线内容隔离）
    growth_out = []
    toxic_exposure = has_toxic_or_bottomless_exposure(content_to_check)
    if body and not toxic_exposure:
        try:
            from persona_growth import judge_growth_from_reading, save_growth

            existing_vals = [
                str(f.get("value_text") or f.get("value") or "")
                for f in existing_fact_rows
                if str(f.get("value_text") or f.get("value") or "").strip()
            ]
            growth = await judge_growth_from_reading(
                title=f"《{chosen_book.get('title') or ''}》",
                body=body,
                her_view=view,
                existing=existing_vals,
                source_kind="reading",
                source_id=str(chosen_book.get("url") or life_item["item_id"]),
            )
            if growth:
                await save_growth(l2, shared_uid, growth)
                growth_out = [{"dim": g["dim_zh"], "value": g["value"]} for g in growth]
        except Exception as e:
            print(f"[novel->growth] warn: {e}", flush=True)

    return {
        "ok": True,
        "stage": "saved",
        "platform": chosen_plat,
        "chosen": chosen_book.get("title"),
        "book": chosen_book.get("title"),
        "author": chosen_book.get("author"),
        "action": action,
        "executed_actions": exec_acts,
        "body_chars": len(body),
        "hot_paragraphs_count": len(deep_info.get("hot_paragraphs") or []) if isinstance(deep_info, dict) else 0,
        "taste": saved_taste,
        "growth": growth_out,
        "item_id": life_item["item_id"],
    }
