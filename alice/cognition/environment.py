"""Read-only adapters. No persona writes, QQ sends, likes or comments."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from itertools import zip_longest
from pathlib import Path
import sys
import time
from uuid import UUID
from urllib.parse import urlsplit


def load_runtime(root):
    from dotenv import load_dotenv
    root = Path(root).resolve()
    for name in [".env.local", ".env"]:
        load_dotenv(root / "alice/config" / name, override=False)
    cfg = json.loads((root / ".runtime/qq-social/config.json").read_text(encoding="utf-8-sig"))
    return cfg


class LiveModel:
    requires_inquiry = True

    def __init__(self, cfg):
        from openai import AsyncOpenAI
        from llm_client import _is_opencode_url, thinking_disabled_extra_body
        self.name = cfg["model"]
        url = cfg.get("model_base_url") or os.environ.get("OPENCODE_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL")
        key = cfg.get("model_api_key") or os.environ.get("OPENCODE_API_KEY")
        if not url or not key:
            raise RuntimeError("model_not_configured")
        options = cfg.get('cognition_inference', {})
        if not isinstance(options, dict) or options.keys() - {'thinking', 'thinking_budget', 'max_tokens', 'timeout_seconds'}:
            raise ValueError('invalid_cognition_inference_options')
        self.max_tokens = options.get('max_tokens', 900)
        self.timeout_seconds = options.get('timeout_seconds', 45)
        if (type(self.max_tokens) is not int or self.max_tokens <= 0
                or (self.name.casefold() == 'longcat-2.5' and self.max_tokens > 262144)):
            raise ValueError('invalid_cognition_output_budget')
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0):
            raise ValueError('invalid_cognition_timeout')
        self.extra_body = thinking_disabled_extra_body(self.name, url)
        mode = options.get('thinking')
        if 'thinking' in options:
            if self.name.casefold() != 'longcat-2.5' or not isinstance(mode, str) or mode not in {'enabled', 'disabled'}:
                raise ValueError('unsupported_cognition_thinking_mode')
            self.extra_body = {'thinking': {'type': mode}}
        budget = options.get('thinking_budget')
        if mode == 'enabled' or 'thinking_budget' in options:
            # Request room for a final decision; provider enforcement must be
            # verified from usage, not assumed from accepting this option.
            if mode != 'enabled' or type(budget) is not int or not 0 < budget < self.max_tokens:
                raise ValueError('invalid_cognition_thinking_budget')
            self.extra_body['thinking_budget'] = budget
        self.inference = {'max_tokens': self.max_tokens, 'timeout_seconds': self.timeout_seconds,
                          'thinking': (self.extra_body or {}).get('thinking', {}).get('type'),
                          'thinking_budget': budget}
        client_kwargs = {'base_url': url, 'api_key': key, 'timeout': self.timeout_seconds, 'max_retries': 0}
        if _is_opencode_url(url):
            session_id = os.environ.get('OPENCODE_SESSION_ID') or ('ses_' + UUID(bytes=os.urandom(16), version=4).hex)
            client_kwargs['default_headers'] = {'x-opencode-session': session_id}
        self.client = AsyncOpenAI(**client_kwargs)
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        self.last_response = {'finish_reason': None, 'usage': None, 'inference': dict(self.inference)}
        started = time.monotonic()
        try:
            # A total deadline also bounds transports that keep resetting their
            # per-read timeout. Cancellation propagates; no automatic retries.
            async with asyncio.timeout(self.timeout_seconds):
                response = await self.client.chat.completions.create(
                    model=self.name, messages=messages, temperature=.3, max_tokens=self.max_tokens,
                    extra_body=self.extra_body, response_format={"type": "json_object"})
            self.last_response.update(finish_reason=str(response.choices[0].finish_reason),
                                      usage=response.usage.model_dump() if response.usage else None)
            return str(response.choices[0].message.content or "")
        except (Exception, asyncio.CancelledError) as exc:
            self.last_response['error_type'] = type(exc).__name__
            raise
        finally:
            self.last_response['elapsed_seconds'] = round(time.monotonic() - started, 3)

    async def close(self):
        await self.client.close()


class ReadingEnvironment:
    def __init__(self, root, cfg, ledger):
        self.root, self.cfg, self.ledger = Path(root), cfg, ledger
        self.persona_id = cfg['persona_user_id']
        self.payloads = {}
        self.catalog_errors = []
        from .exploration import Explorer
        self.explorer = Explorer(ledger, self.current_config)

    def exploration_capabilities(self):
        return self.explorer.capabilities()

    async def search(self, query, goal, *, provider=None, fresh=False):
        return await self.explorer.search(query, goal, provider=provider, fresh=fresh)

    def current_config(self):
        latest = json.loads((self.root/'.runtime/qq-social/config.json').read_text(encoding='utf-8-sig'))
        if latest.get('persona_user_id') != self.persona_id:
            raise RuntimeError('cognitive_identity_changed_restart_required')
        self.cfg = latest
        return latest

    def text_feed_urls(self):
        from alice_reading import _AI_FEEDS
        configured = self.current_config().get('cognition_text_feed_urls', [])
        if not isinstance(configured, list):
            return ()
        return tuple(dict.fromkeys(url for url in configured if isinstance(url, str) and url in _AI_FEEDS))[:2]

    async def text_feed_cards(self):
        """Configured publisher titles only; reading remains an explicit action."""
        import alice_reading
        feeds = self.text_feed_urls()
        cached = getattr(self, '_text_feed_cache', None)
        if cached and cached[0] == feeds and time.monotonic() - cached[1] < 600:
            resources = cached[2]
        else:
            resources = []
            catalogs = []
            for feed in feeds:
                catalog = []
                catalogs.append(catalog)
                try:
                    # Keep a bounded title reservoir. Idle attention removes
                    # consumed sources before choosing its six visible options;
                    # truncating to two here starved it after two inspections.
                    cards = await asyncio.wait_for(asyncio.to_thread(alice_reading._fetch_rss, feed, 12), timeout=12)
                    if not cards:
                        self.catalog_errors.append({'adapter':'text_feed','source_ref':feed,'status':'empty_or_unavailable'})
                    publisher = (urlsplit(feed).hostname or '').removeprefix('www.')
                    for card in cards[:12]:
                        ref = str(card.get('url') or '')
                        parts = urlsplit(ref)
                        host = parts.hostname or ''
                        if (parts.scheme not in {'http','https'} or parts.username or parts.password
                                or not (host == publisher or host.endswith('.' + publisher))):
                            continue
                        rid = 'article:' + hashlib.sha256(ref.encode()).hexdigest()[:24]
                        if any(r['id'] == rid for r in resources):
                            continue
                        resources.append({'id':rid,'family':'public_article_text',
                            'title':str(card.get('title') or '')[:160], 'source_ref':ref, 'feed_source':feed,
                            'description':'网页文章候选；尚未读取正文，打开后仅取得有限正文，不代表标题已被证实'})
                        catalog.append(resources[-1])
                except Exception as exc:
                    self.catalog_errors.append({'adapter':'text_feed','source_ref':feed,'error_type':type(exc).__name__})
            # One publisher must not consume the whole attention window.
            resources = [r for row in zip_longest(*catalogs) for r in row if r is not None]
            self._text_feed_cache = (feeds, time.monotonic(), resources)
        # observe clears payloads each time, including when catalog metadata is cached.
        allowed = self.text_feed_urls()
        resources = [r for r in resources if r['feed_source'] in allowed]
        for resource in resources:
            self.payloads[resource['id']] = ('text_feed', resource)
        return resources

    async def persona(self):
        """Read actual rows without constructing a store that initializes schema."""
        import asyncpg
        url = os.environ.get("ALICE_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("persona_database_not_configured")
        connection = await asyncpg.connect(url, timeout=8)
        try:
            async with connection.transaction(readonly=True):
                if await connection.fetchval("SELECT current_database()") != "alice_persona":
                    raise RuntimeError("wrong_persona_database")
                rows = await connection.fetch("""SELECT fact_id,kind,facet_key,value_text,evidence_quote,source_kind
                  FROM companion_persona_facts WHERE user_id=$1 AND status='active'
                  ORDER BY CASE WHEN kind='identity' THEN 0 ELSE 1 END,recorded_at DESC LIMIT 8""",
                                              UUID(self.cfg["persona_user_id"]))
                # Skip oversized expressions whole, retaining negations/qualifiers.
                return [dict(r) for r in rows if len(str(r["value_text"] or "")) <= 600
                        and len(str(r["evidence_quote"] or "")) <= 600]
        finally:
            await connection.close()

    async def observe(self):
        self.payloads, self.catalog_errors = {}, []
        resources = []
        # Only the pure perception functions of the existing reader are used.
        import alice_reading
        alice_reading._COOKIE_PATH = str(self.root / ".runtime/bili_cookies.json")
        try:
            cards = await asyncio.wait_for(asyncio.to_thread(alice_reading.fetch_bilibili, limit=6), timeout=25)
            if not cards:
                self.catalog_errors.append({"adapter": "bilibili", "status": "empty_catalog"})
            for card in cards:
                ref = str(card.get("url") or "")
                if not ref.startswith("https://www.bilibili.com/video/"):
                    continue
                rid = "bili:" + str(card.get("bvid") or ref.rsplit("/", 1)[-1])
                resources.append({"id": rid, "family": "bilibili_metadata_comments", "title": str(card["title"])[:160],
                                  "source_ref": ref, "description": "视频标题、简介、热门评论；这个入口未传输视频音轨"})
                self.payloads[rid] = ("bili", ref)
        except Exception as exc:
            self.catalog_errors.append({"adapter": "bilibili", "error_type": type(exc).__name__})
        resources.extend(await self.text_feed_cards())
        for path in sorted((self.root / ".runtime/books").glob("*-chunks.json"))[:4]:
            book = json.loads(path.read_text(encoding="utf-8-sig"))
            slug, chunks = book["book"], book["chunks"]
            old_path = path.with_name(slug + "-state.json")
            old = json.loads(old_path.read_text(encoding="utf-8-sig")) if old_path.exists() else {}
            cursor = int(old.get("cursor", 0))
            for row in self.ledger.db.execute("SELECT result_json,decision_json FROM episodes WHERE owner=? AND status='observed' AND revoked=0", (self.ledger.owner,)):
                if json.loads(row[1]).get('action')!='read':continue
                result = json.loads(row[0])
                if result.get("book") == slug:
                    cursor = max(cursor, int(result["chunk"]) + 1)
            if cursor >= len(chunks):
                continue
            rid = f"book:{slug}:{cursor}"
            resources.append({"id": rid, "family": "book_original_text", "title": f"{slug} 第{cursor + 1}/{len(chunks)}段",
                              "source_ref": rid, "description": "本地已授权书籍的下一段原文；还没读到的部分不当作经历"})
            self.payloads[rid] = ("book", {"text": chunks[cursor]["text"], "book": slug, "chunk": cursor})
        facts = await self.persona()
        for discovered in self.explorer.resources():
            item = {k: v for k, v in discovered.items() if k != '_entry'}
            resources.append(item)
            self.payloads[item['id']] = ('discovered', item)
        return resources, facts

    async def inspect(self,resource):
        """One bounded preview using the existing authorized read adapter."""
        kind,payload=self.payloads[resource['id']]
        if kind=='bili':
            import alice_reading
            text=await asyncio.to_thread(lambda:asyncio.run(alice_reading.fetch_article(payload,comment_pages=1,comment_limit=3)))
            return {'status':'ok' if text else 'empty','source_ref':payload,'text':text[:1200],
                    'primary_observed':False,'audio_observed':False}
        result=await self.read(resource)
        text=str(result.get('text',''))
        return {**result,'text':text[:1200],'text_truncated':bool(result.get('text_truncated')) or len(text)>1200}

    async def read(self, resource):
        kind, payload = self.payloads[resource["id"]]
        if kind == 'text_feed':
            from agent.public_page_reader import read_public_page
            ref = payload['source_ref']
            if payload['feed_source'] not in self.text_feed_urls():
                return {'status':'error','code':'feed_disabled','source_ref':ref}
            cfg = self.current_config()
            resolver = cfg.get('cognition_page_resolver', os.getenv('PUBLIC_PAGE_READER_RESOLVER', 'system'))
            result = await read_public_page(ref, timeout=8, resolver=resolver)
            if payload['feed_source'] not in self.text_feed_urls():
                return {'status':'error','code':'feed_disabled','source_ref':ref}
            text = str(result.get('text') or '')
            return {**result,'source_ref':ref,'text':text[:5000],
                    'text_truncated':bool(result.get('text_truncated')) or len(text)>5000,
                    'body_origin':'public_https_reader',
                    'primary_observed':False,'audio_observed':False}
        if kind == 'discovered':
            return await self.explorer.read(payload)
        if kind == "book":
            return {"status": "ok", "source_ref": resource["source_ref"], **payload,
                    "primary_observed": True, "audio_observed": False}
        import alice_reading
        text = await asyncio.to_thread(lambda: asyncio.run(alice_reading.fetch_article(payload, comment_pages=1, comment_limit=12)))
        return {"status": "ok" if text else "empty", "source_ref": payload, "text": text,
                "primary_observed": False, "audio_observed": False}
