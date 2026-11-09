"""Bounded optional bridge; never starts a model or sends a QQ message."""
import asyncio
import hashlib
import json
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit


def request(config, path, body):
    settings_path = Path(config.get("product_root", ".")) / ".runtime/cognition-bridge.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        if not settings.get("enabled"):
            return {}
        endpoint = urlsplit(settings["url"])
        if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.path not in {"", "/"}:
            return {}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(settings["url"].rstrip("/") + path, data=data,
                                     headers={"Authorization": "Bearer " + settings["token"], "Content-Type": "application/json"})
        # No retries, provider fallback or model work in the foreground path.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=.4) as response:
            raw = response.read(32769)
        return json.loads(raw) if len(raw) <= 32768 else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


async def context(config, work):
    work.pop('_cognition_basis', None)
    return await asyncio.to_thread(request, config, "/v1/context", {"scope": work["group"], "actor": work["actor"]})


def source_event(config, event):
    if event.get("post_type") != "message" or str(event.get("self_id")) != str(config.get("expected_qq")):
        return None
    actor = str(event.get("user_id", ""))
    if not actor.isdigit() or actor in {str(config.get("expected_qq")), *[str(x) for x in config.get("bot_qq_ids", [])]}:
        return None
    if event.get("message_type") == "private":
        if actor not in {str(x) for x in config.get("allowed_private_users", [])}:
            return None
        scope = "private:" + actor
    elif event.get("message_type") == "group":
        scope = str(event.get("group_id", ""))
        if scope not in {str(x) for x in config.get("allowed_groups", [])}:
            return None
    else:
        return None
    if not config.get("enabled"):
        return None
    segments = event.get("message")
    if not isinstance(segments, list) or any(not isinstance(s, dict) or not isinstance(s.get('data', {}), dict) for s in segments):
        return None
    if any(isinstance(s, dict) and s.get("type") == "record" for s in segments) and event.get("voice_input", {}).get("status") != "transcribed":
        return None
    text = "".join(str(s.get("data", {}).get("text", "")) for s in segments if isinstance(s, dict) and s.get("type") == "text")
    if not 1 <= len(text.strip()) <= 4000 or not event.get("message_id"):
        return None
    return {"scope": scope, "actor": actor, "role": "person", "text": text.strip(),
            "message_id": str(event["message_id"]), "at": event.get("time", time.time()),
            "voice_source": event.get("voice_input")}


class Observer:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.wake = asyncio.Event()
        store.db.execute('''CREATE TABLE IF NOT EXISTS cognition_outbox(
          key TEXT PRIMARY KEY,payload TEXT,status TEXT,attempts INTEGER DEFAULT 0,next_at REAL DEFAULT 0)''')
        store.db.commit()
        self.recover_recent()

    def recover_recent(self):
        if not (Path(self.config.get('product_root','.'))/'.runtime/cognition-bridge.json').is_file():
            return
        self.store.db.execute('CREATE TABLE IF NOT EXISTS cognition_ingest_state(id INTEGER PRIMARY KEY,last_row INTEGER)')
        row=self.store.db.execute('SELECT last_row FROM cognition_ingest_state WHERE id=1').fetchone()
        last=row[0] if row else 0
        rows=self.store.db.execute("SELECT id,conv,group_id,actor,role,content,message_id,at FROM messages WHERE delivered=1 AND id>? AND at>=? ORDER BY id DESC LIMIT 60",(last,time.time()-48*3600)).fetchall()
        for rid,conv,scope,actor,role,text,mid,at in reversed(rows):
            scope,actor=str(scope),str(actor)
            allowed=self.config.get('enabled') and (actor in {str(x) for x in self.config.get('allowed_private_users',[])} if scope.startswith('private:') else scope in {str(x) for x in self.config.get('allowed_groups',[])})
            if not allowed or role not in {'user','assistant'} or not isinstance(text,str) or not 1<=len(text)<=4000 or not mid:
                continue
            material={'scope':scope,'actor':actor,'role':'person' if role=='user' else 'alice','text':text,'message_id':str(mid),'at':at,
                      'source_record':{'kind':'saved_qq_message','row_id':rid,'format':'stored_display_text'}}
            if role=='assistant':
                source=self.store.db.execute("SELECT message_id FROM messages WHERE conv=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1",(conv,rid)).fetchone()
                source_mid=str(source[0]) if source else ''
                material.update(receipts=[str(mid)],reply_to=source_mid,basis=self._reply_basis(source_mid))
            self.put(material)
        if rows:
            with self.store.db:self.store.db.execute('INSERT INTO cognition_ingest_state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET last_row=excluded.last_row',(max(r[0] for r in rows),))

    def _reply_basis(self, source_mid, base=None):
        basis = dict(base or {})
        if source_mid and "context_refs" not in basis:
            for col in ("payload", "detail"):
                try:
                    row = self.store.db.execute(
                        f"SELECT {col} FROM metrics WHERE kind='qq_reply_basis' AND {col} LIKE ? ORDER BY at DESC LIMIT 1",
                        (f"%{source_mid}%",),
                    ).fetchone()
                    if row:
                        detail = json.loads(row[0])
                        if str(detail.get("source_message_id")) == str(source_mid) and detail.get("context_refs"):
                            basis["context_refs"] = detail["context_refs"]
                    break
                except Exception:
                    continue
        return basis

    def put(self, material):
        if not (Path(self.config.get("product_root", ".")) / ".runtime/cognition-bridge.json").is_file():
            return
        key = hashlib.sha256(json.dumps([material['scope'],material['role'],material['message_id']]).encode()).hexdigest()
        with self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO cognition_outbox(key,payload,status) VALUES (?,?,'pending')",
                                  (key,json.dumps(material,ensure_ascii=False)))
        self.wake.set()

    def offer(self, event):
        material = source_event(self.config, event)
        if material:
            self.put(material)

    def offer_delivery(self, work, sent):
        if sent:
            source_mid = str(work["event"]["message_id"])
            self.put({"scope":work['group'],"actor":work['actor'],"role":"alice",
                      "text":"\n\n".join(p[1] for p in sent)[:4000],"message_id":str(sent[0][0]),
                      "receipts":[str(p[0]) for p in sent],"reply_to":source_mid,
                      "at":time.time(),"basis":self._reply_basis(source_mid, work.get('_cognition_basis',{}))})

    async def run(self):
        while True:
            self.wake.clear()
            row = self.store.db.execute("SELECT key,payload,attempts FROM cognition_outbox WHERE status='pending' AND next_at<=? ORDER BY rowid LIMIT 1",(time.time(),)).fetchone()
            if not row:
                try: await asyncio.wait_for(self.wake.wait(),2)
                except asyncio.TimeoutError: pass
                continue
            key, payload, attempts = row
            material = json.loads(payload)
            allowed = self.config.get('enabled') and (
                material['actor'] in {str(x) for x in self.config.get('allowed_private_users',[])} if material['scope'].startswith('private:')
                else material['scope'] in {str(x) for x in self.config.get('allowed_groups',[])})
            if not allowed:
                with self.store.db:self.store.db.execute("UPDATE cognition_outbox SET status='cancelled' WHERE key=?",(key,))
                continue
            result = await asyncio.to_thread(request, self.config, "/v1/event", material)
            accepted = bool(result.get('event_id'))
            with self.store.db:
                self.store.db.execute("UPDATE cognition_outbox SET status=?,attempts=attempts+1,next_at=? WHERE key=?",
                                      ('delivered' if accepted else 'pending',time.time()+min(60,2**min(attempts+1,6)),key))
            if accepted or not attempts:self.store.metric('cognition_event_transfer',{'accepted':accepted,'attempts':attempts+1})
