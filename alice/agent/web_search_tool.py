"""Bounded web search tool for the conversation director.

The language model may request this function through the OpenAI-compatible
tool-call protocol.  This module, not the model, performs the network request.
Search queries use fixed provider endpoints. Optional known source URLs use
the separate bounded public HTTPS reader, never the material import pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Tuple


class _NoSearchAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('search_provider_redirect_rejected')


class WebSearchTool:
    NAME = "web_search"
    _cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def __init__(
        self, *, provider: str = "bing_rss", api_key: str = "",
        timeout: int = 5, max_results: int = 5, cache_ttl: int = 1800,
    ) -> None:
        self.provider = (provider or "bing_rss").strip().lower()
        self.api_key = (api_key or "").strip()
        self.timeout = max(2, min(15, int(timeout)))
        self.max_results = max(1, min(8, int(max_results)))
        self.cache_ttl = max(60, min(86400, int(cache_ttl)))

    @classmethod
    def definition(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": cls.NAME,
                "description": (
                    "Search the public web only when a current, location-specific, "
                    "brand-specific, policy, regulation, or real-world process fact "
                    "is necessary to plan the next conversation step. Do not use it "
                    "for generic stable role-play flow or ordinary language teaching."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "A concise search query containing the relevant place, "
                                "organization, process, and recency cue."
                            ),
                        },
                        "source_url": {"type": "string", "maxLength": 2048,
                            "description": "Optional exact public HTTPS source URL already known or supplied by the user. Use it to read the actual page; omit if unknown. Never invent a URL."},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def explicit_page_url(user_text: str) -> str:
        if not re.search(r"\b(?:open|read|visit|check)\b|look at|打开|读取|读一下|看看", user_text or "", re.I):
            return ""
        matches = re.findall(r"https?://[^\s<>\"'()，。！？；：、]+|(?<![\w@])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}/[^\s<>\"'()，。！？；：、]*", user_text or "")
        urls = []
        for match in matches:
            value = match.rstrip(".,!?;:，。！？；：")
            if not value.startswith(("https://", "http://")):
                value = "https://" + value
            if value not in urls:
                urls.append(value)
        return urls[0] if len(urls) == 1 else ""

    @staticmethod
    def page_excerpts(text: str, query: str) -> List[Dict[str, Any]]:
        """Select bounded verbatim spans; relevance ranking is not verification."""
        words = set(re.findall(r"[a-z0-9]{3,}", str(query or "").lower())) - {
            "the", "and", "for", "please", "search", "tell", "what", "about", "https", "www", "com", "org", "official", "page"}
        positions = [m.start() for m in re.finditer(r"\b[a-z0-9]{3,}\b", text.lower()) if m.group() in words]
        candidates = {}
        for position in positions:
            start = max(0, position - 80)
            end = min(len(text), start + 280)
            span = text[start:end]
            score = sum(bool(re.search(r"\b" + re.escape(word) + r"\b", span, re.I)) for word in words)
            candidates[start] = (score, end)
        if not candidates and text.strip():
            candidates[0] = (0, min(280, len(text)))
        selected = []
        for start, (_, end) in sorted(candidates.items(), key=lambda row: (-row[1][0], row[0])):
            if any(start < row["source_end"] and end > row["source_start"] for row in selected):
                continue
            selected.append({"snippet": text[start:end], "source_start": start, "source_end": end})
            if len(selected) == 3:
                break
        return selected

    async def read_page(self, source_url: str, query: str) -> Dict[str, Any]:
        from .public_page_reader import read_public_page
        page = await read_public_page(source_url, timeout=self.timeout,
            resolver=os.getenv("PUBLIC_PAGE_READER_RESOLVER", "system"))
        record = {"query": self._normalize_query(query), "provider": "public_page_reader", "cached": False,
                  "status": page.get("status", "error"), "results": []}
        if page.get("status") != "ok":
            record["error_code"] = page.get("error_code", "page_read_failed")
            return record
        common = {key: page.get(key) for key in ("url", "title", "evidence_kind", "fetched_at", "text_truncated", "response_sha256")}
        common["title"] = str(common.get("title") or "")[:180]
        record["results"] = [{**common, "content_status": "fetched", **span}
                             for span in self.page_excerpts(page["text"], query)]
        record["page_metadata"] = {key: value for key, value in page.items() if key != "text"}
        record["page_metadata"]["title"] = common["title"]
        return record

    @staticmethod
    def page_model_content(record: Dict[str, Any]) -> str:
        """Bound page tool context without cutting serialized JSON mid-field."""
        payload = {key: value for key, value in record.items() if key != "page_metadata"}
        payload["results"] = list(record.get("results") or [])[:3]
        content = json.dumps(payload, ensure_ascii=False)
        while len(content) > 8000 and len(payload["results"]) > 1:
            payload["results"].pop()
            content = json.dumps(payload, ensure_ascii=False)
        return content

    async def search(self, query: str) -> Dict[str, Any]:
        normalized = self._normalize_query(query)
        if not normalized:
            return self._error("invalid_query", "The search query is empty.")

        cache_key = f"{self.provider}:{normalized.casefold()}:{self.max_results}"
        if self.provider == 'deepseek_search':
            cache_key += ':' + hashlib.sha256(self.api_key.encode()).hexdigest()[:16]
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            result = dict(cached[1])
            result["cached"] = True
            return result

        try:
            if self.provider == "tavily":
                if not self.api_key:
                    return self._error("missing_api_key", "Tavily API key is not configured.")
                result = await asyncio.to_thread(self._search_tavily, normalized)
            elif self.provider == "bing_rss":
                result = await asyncio.to_thread(self._search_bing_rss, normalized)
            elif self.provider == "crossref":
                result = await asyncio.to_thread(self._search_crossref, normalized)
            elif self.provider == "wikipedia":
                result = await asyncio.to_thread(self._search_wikipedia, normalized)
            elif self.provider == 'deepseek_search':
                if not self.api_key:
                    return self._error('missing_api_key', 'Modelink search key is not configured.')
                if len(normalized)>128:
                    return self._error('invalid_query_length', 'Search query exceeds provider limit.')
                result = await asyncio.to_thread(self._search_deepseek, normalized)
            else:
                return self._error("unsupported_provider", f"Unsupported provider: {self.provider}")
        except Exception as exc:
            result = self._error(type(exc).__name__[:64], "Web search request failed.")

        result["query"] = normalized
        result["provider"] = self.provider
        result["cached"] = False
        if result.get("status") == "ok":
            self._cache[cache_key] = (now + self.cache_ttl, dict(result))
        return result

    def _search_bing_rss(self, query: str) -> Dict[str, Any]:
        params = urllib.parse.urlencode({"q": query, "format": "rss", "setlang": "en-US"})
        body = self._request(
            f"https://www.bing.com/search?{params}",
            headers={"Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8"},
        )
        return {"status": "ok", "results": self._parse_bing_rss(body, self.max_results)}

    def _search_crossref(self, query: str) -> Dict[str, Any]:
        # A research query may retain a Chinese source anchor for grounding.
        # Use its English scientific terms for the international paper index.
        english = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9'-]*", query))
        search_query = english if len(english.split()) >= 2 else query
        params = urllib.parse.urlencode({
            "query.bibliographic": search_query, "rows": self.max_results,
            "filter": "type:journal-article,has-abstract:true",
            "select": "DOI,title,abstract,URL,published,container-title,type",
        })
        for attempt in range(2):
            try:
                data = json.loads(self._request(
                    "https://api.crossref.org/works?" + params,
                    headers={"Accept": "application/json"}))
                break
            except urllib.error.HTTPError as exc:
                if attempt or exc.code not in (500, 502, 503, 504):
                    raise
                time.sleep(0.2)

        results = []
        for item in (data.get("message", {}).get("items") or [])[:self.max_results]:
            title = self._clean_text(" ".join(item.get("title") or []), 180)
            abstract = self._clean_text(item.get("abstract"), 12000)
            doi = str(item.get("DOI") or "").strip()
            if (not doi or not abstract or re.search(
                    r"\b(?:protocol|retraction|withdrawal|correction|erratum)\b", title, re.I)):
                continue
            results.append({
                "title": title, "url": "https://doi.org/" + urllib.parse.quote(doi, safe="/"),
                "snippet": abstract[:500], "body_text": abstract,
                "content_status": "fetched", "evidence_kind": "research_abstract",
                "doi": doi, "published": item.get("published"),
                "journal": " ".join(item.get("container-title") or [])[:180],
            })
        return {"status": "ok", "results": results}

    def _search_deepseek(self, query: str) -> Dict[str, Any]:
        """Modelink's DeepSeek Search; summaries remain discovery candidates."""
        endpoint='https://api.qnaigc.com/v1/tools/search'
        request=urllib.request.Request(endpoint,method='POST',
            data=json.dumps({'query':query,'count':self.max_results,'need_content':False}).encode(),
            headers={'Authorization':'Bearer '+self.api_key,'Content-Type':'application/json','Accept':'application/json'})
        try:
            with urllib.request.build_opener(_NoSearchAuthRedirect).open(request,timeout=self.timeout) as response:
                raw=response.read(512001)
        except urllib.error.HTTPError as exc:
            return self._error('provider_http_'+str(exc.code),'Modelink search request failed.')
        if len(raw)>512000:return self._error('provider_response_too_large','Search response exceeds limit.')
        decoded=json.loads(raw.decode('utf-8'))
        if not isinstance(decoded,dict) or not isinstance(decoded.get('results'),list):
            return self._error('invalid_provider_response','Search response has no result list.')
        results=[]
        for item in decoded['results'][:self.max_results]:
            if not isinstance(item,dict):continue
            url=self._safe_http_url(str(item.get('url') or ''))
            if not url:continue
            results.append({'url':url,'title':self._clean_text(item.get('title'),180),
                'snippet':self._clean_text(item.get('snippet') or item.get('summary'),500)})
        return {'status':'ok','results':results,'source_api_url':endpoint,
                'api_response_sha256':hashlib.sha256(raw).hexdigest()}

    def _search_tavily(self, query: str) -> Dict[str, Any]:
        payload = json.dumps({
            "api_key": self.api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": self.max_results,
            "include_answer": False,
            "include_raw_content": False,
        }).encode("utf-8")
        body = self._request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        decoded = json.loads(body)
        results = []
        for item in (decoded.get("results") or [])[:self.max_results]:
            url = self._safe_http_url(str(item.get("url") or ""))
            if not url:
                continue
            results.append({
                "title": self._clean_text(item.get("title"), 180),
                "url": url,
                "snippet": self._clean_text(item.get("content"), 500),
            })
        return {"status": "ok", "results": results}

    def _search_wikipedia(self, query: str) -> Dict[str, Any]:
        """Search plus actually fetched introductory extracts, not primary facts.

        Fixed public MediaWiki endpoints; no model-generated URL is fetched.
        https://www.mediawiki.org/wiki/API:Search
        https://www.mediawiki.org/wiki/Extension:TextExtracts#API
        """
        language = "zh" if re.search(r"[\u4e00-\u9fff]", query) else "en"
        endpoint = f"https://{language}.wikipedia.org/w/api.php?"
        headers = {"Accept": "application/json"}
        params = {"action":"query","list":"search","srsearch":query,
                  "srlimit":self.max_results,"format":"json","utf8":1}
        data = json.loads(self._request(endpoint + urllib.parse.urlencode(params), headers=headers))
        if data.get('error'):
            return self._error('wikipedia_api_error','Encyclopedia search failed.')
        hits = [h for h in (data.get('query') or {}).get('search',[])[:self.max_results]
                if isinstance(h,dict) and type(h.get('pageid')) is int and h['pageid']>0]
        searches=[{'query':query,'mode':'original','status':'ok'}]
        terms=re.findall(r'[A-Za-z0-9]+',query)
        # One OR expansion supplements sparse plain-English queries, retaining
        # original hits. Quoted phrases, exclusions and field filters stay exact.
        if (len(hits)<self.max_results and 3<len(terms)<=20
                and re.fullmatch(r'[A-Za-z0-9 $]+',query)
                and not {'AND','OR','NOT'}.intersection(terms)):
            relaxed=' OR '.join(terms)
            try:
                extra=json.loads(self._request(endpoint+urllib.parse.urlencode({**params,'srsearch':relaxed}),headers=headers))
                if extra.get('error'):raise ValueError('wikipedia_relaxation_api_error')
                seen={h['pageid'] for h in hits}
                for h in (extra.get('query') or {}).get('search',[])[:self.max_results]:
                    if (isinstance(h,dict) and type(h.get('pageid')) is int and h['pageid']>0 and h['pageid'] not in seen):
                        hits.append(h);seen.add(h['pageid'])
                    if len(hits)>=self.max_results:break
                searches.append({'query':relaxed,'mode':'or_expansion','status':'ok'})
            except Exception as exc:
                searches.append({'query':relaxed,'mode':'or_expansion','status':'error','error_type':type(exc).__name__})
        if not hits:return {"status":"ok","results":[],"search_queries":searches}
        params = {"action":"query","prop":"extracts","pageids":"|".join(str(h['pageid']) for h in hits),
                  "explaintext":1,"exintro":1,"exchars":1200,"format":"json","formatversion":2}
        extracts = json.loads(self._request(endpoint + urllib.parse.urlencode(params), headers=headers))
        if extracts.get('error'):
            return self._error('wikipedia_extract_error','Encyclopedia introductory text could not be fetched.')
        pages = {p['pageid']:p for p in (extracts.get('query') or {}).get('pages',[])
                 if isinstance(p,dict) and type(p.get('pageid')) is int}
        results = []
        for hit in hits:
            text = str(pages.get(hit['pageid'],{}).get('extract') or '').strip()
            if not text:continue  # A search snippet is not retrieved article text.
            results.append({'title':self._clean_text(hit.get('title'),180),
                'url':f"https://{language}.wikipedia.org/?curid={hit['pageid']}",
                'snippet':self._clean_text(hit.get('snippet'),500),'body_text':text[:4000],
                'evidence_kind':'encyclopedia_intro_extract','content_status':'fetched',
                'page_id':hit['pageid'],'language':language,'last_edited':hit.get('timestamp')})
        return {'status':'ok','results':results,'search_queries':searches}

    async def read_wikipedia_article(self, language: str, page_id: int, *, query: str = '') -> Dict[str, Any]:
        if language not in {'en','zh'} or type(page_id) is not int or page_id<=0:
            return {'status':'error','text':'','error_code':'invalid_encyclopedia_page'}
        params={'action':'query','prop':'extracts','pageids':str(page_id),'explaintext':1,
                'format':'json','formatversion':2}
        url=f'https://{language}.wikipedia.org/w/api.php?'+urllib.parse.urlencode(params)
        try:
            raw=await asyncio.to_thread(self._request,url,headers={'Accept':'application/json'})
            data=json.loads(raw)
            page=next((p for p in (data.get('query') or {}).get('pages',[])
                       if p.get('pageid')==page_id),{})
            text=str(page.get('extract') or '').strip()
            if data.get('error') or not text:raise ValueError('missing_article_text')
            start=0
            if query:
                matches=self.page_excerpts(text,query)
                if matches:
                    start=max(0,matches[0]['source_start']-800)
                    boundary=text.rfind('\n',max(0,start-400),start+1)
                    if boundary>=0:start=boundary+1
            excerpt=text[start:start+5000]
            return {'status':'ok','text':excerpt,'text_truncated':len(excerpt)<len(text),
                    'source_text_start':start,'source_text_end':start+len(excerpt),
                    'source_text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    'response_sha256':hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                    'title':str(page.get('title') or '')[:180],
                    'evidence_kind':'encyclopedia_text_excerpt','body_origin':'mediawiki_api_response',
                    'fetched_url':url,'canonical_url':f'https://{language}.wikipedia.org/?curid={page_id}',
                    'source_api_url':url}
        except Exception as exc:
            return {'status':'error','text':'','error_code':'encyclopedia_text_'+type(exc).__name__[:40]}

    def _request(
        self, url: str, *, data: bytes | None = None,
        headers: Dict[str, str] | None = None,
    ) -> str:
        request_headers = {
            "User-Agent": "LanguageTutorDirector/1.0 (+bounded-web-search)",
            **(headers or {}),
        }
        request = urllib.request.Request(
            url, data=data, headers=request_headers,
            method="POST" if data is not None else "GET",
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(512_000)
                    charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt or isinstance(exc, urllib.error.HTTPError):
                    raise

    @classmethod
    def _parse_bing_rss(cls, body: str, max_results: int) -> List[Dict[str, str]]:
        root = ET.fromstring(body)
        results: List[Dict[str, str]] = []
        for item in root.findall(".//item"):
            url = cls._safe_http_url(item.findtext("link") or "")
            if not url:
                continue
            results.append({
                "title": cls._clean_text(item.findtext("title"), 180),
                "url": url,
                "snippet": cls._clean_text(item.findtext("description"), 500),
            })
            if len(results) >= max_results:
                break
        return results

    @staticmethod
    def _normalize_query(query: Any) -> str:
        value = " ".join(str(query or "").split())
        value = re.sub(r"[\x00-\x1f\x7f]", "", value)
        return value[:240]

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str:
        text = html.unescape(str(value or ""))
        text = re.sub(r"<[^>]+>", " ", text)
        return " ".join(text.split())[:limit]

    @staticmethod
    def _safe_http_url(value: str) -> str:
        url = value.strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return url[:1000]

    @staticmethod
    def _error(code: str, message: str) -> Dict[str, Any]:
        return {
            "status": "error", "error_code": code,
            "message": message, "results": [],
        }
