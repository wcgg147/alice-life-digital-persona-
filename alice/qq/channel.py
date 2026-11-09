"""QQ transport and local admission. Importing this module never calls a model."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import math
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS budget_day_overrides (day TEXT PRIMARY KEY, cap INTEGER, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, conv TEXT, group_id TEXT, actor TEXT,
          role TEXT, content TEXT, message_id TEXT, at REAL, delivered INTEGER,
          consolidated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS metrics (at REAL, kind TEXT, payload TEXT);
        CREATE INDEX IF NOT EXISTS messages_reply_lookup ON messages(group_id,message_id,role);
        CREATE TABLE IF NOT EXISTS sent_message_receipts (
          group_id TEXT, message_id TEXT, conv TEXT, actor TEXT, at REAL,
          PRIMARY KEY(group_id,message_id));
        CREATE TABLE IF NOT EXISTS qq_user_terms (
          scope TEXT, term TEXT, meaning TEXT, source_message_id TEXT,
          status TEXT NOT NULL DEFAULT 'active', created_at REAL,
          PRIMARY KEY(scope,term));
        """)
        self.db.commit()

    def first_seen(self, key, now):
        cur = self.db.execute('INSERT OR IGNORE INTO seen VALUES (?,?)', (key, now))
        self.db.execute('DELETE FROM seen WHERE at < ?', (now - 7 * 86400,))
        self.db.commit()
        return cur.rowcount == 1

    def reserve(self, cap, now=None):
        day = datetime.fromtimestamp(now or time.time(), timezone(timedelta(hours=8))).date().isoformat()
        with self.db:
            override=self.db.execute('SELECT cap FROM budget_day_overrides WHERE day=?',(day,)).fetchone()
            effective=override[0] if override else cap
            self.db.execute('INSERT OR IGNORE INTO budget VALUES (?,0)', (day,))
            if effective is None:
                cur=self.db.execute('UPDATE budget SET calls=calls+1 WHERE day=?',(day,))
            else:
                cur = self.db.execute('UPDATE budget SET calls=calls+1 WHERE day=? AND calls < ?', (day, effective))
        if not cur.rowcount:
            raise RuntimeError('daily_model_call_cap')

    def metric(self, kind, payload):
        self.db.execute('INSERT INTO metrics VALUES (?,?,?)', (time.time(), kind, json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    def message(self, conv, event, role, text, message_id, delivered):
        self.db.execute('INSERT INTO messages(conv,group_id,actor,role,content,message_id,at,delivered) VALUES (?,?,?,?,?,?,?,?)',
                        (conv, str(event['group_id']), str(event['user_id']), role, text, str(message_id), time.time(), int(delivered)))
        self.db.commit()

    def history(self, group, actor, *, since=0):
        rows = self.db.execute('SELECT role,content,at FROM messages WHERE group_id=? AND actor=? AND delivered=1 AND at>=? ORDER BY id DESC LIMIT 12',
                               (str(group), str(actor), since)).fetchall()
        from tool_protocol import contains_tool_protocol
        return [{'role': r[0], 'content': r[1][:800], 'at': r[2]} for r in reversed(rows)
                if r[0] != 'assistant' or not contains_tool_protocol(r[1])]

    def user_terms(self, scope):
        """这个人在这个会话里对某些词的说法；只做词义提示，不做意图判断。"""
        rows = self.db.execute(
            "SELECT term,meaning FROM qq_user_terms WHERE scope=? AND status='active' "
            "ORDER BY created_at DESC LIMIT 12", (str(scope),)).fetchall()
        return [{'term': str(r[0]), 'meaning': str(r[1])} for r in rows if str(r[1] or '').strip()]

    def mark_reply_quality(self, scope, actor, verdict, reason, trigger):
        """把「上一条回复」按对方的抱怨标成负样本；不需要对方额外打标，正常说话即可。"""
        row = self.db.execute("SELECT message_id, content FROM messages WHERE group_id=? AND actor=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                              (str(scope), str(actor))).fetchone()
        if not row:
            return None
        content = str(row[1] or '')
        self.db.execute("CREATE TABLE IF NOT EXISTS reply_quality("
                        "scope TEXT, actor TEXT, reply_message_id TEXT, reply_content TEXT, reply_chars INTEGER,"
                        "reply_bubbles INTEGER, verdict TEXT, reason TEXT, trigger_text TEXT, at REAL,"
                        "PRIMARY KEY(scope, actor, reply_message_id))")
        self.db.execute("INSERT OR IGNORE INTO reply_quality(scope,actor,reply_message_id,reply_content,reply_chars,reply_bubbles,verdict,reason,trigger_text,at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (str(scope), str(actor), str(row[0]), content, len(content), content.count("\n\n") + 1,
                         verdict, reason, str(trigger)[:120], time.time()))
        self.db.commit()
        return row[0]

    def history_message_ids(self,group,actor,*,since=0):
        if str(group).startswith('private:'):
            rows=self.db.execute('SELECT message_id,at FROM messages WHERE group_id=? AND actor=? AND delivered=1 AND at>=? ORDER BY id DESC LIMIT 12',
                                 (str(group),str(actor),since)).fetchall()
            ids={str(r[0]) for r in rows}
            if rows:
                ids.update(str(r[0]) for r in self.db.execute('SELECT message_id FROM sent_message_receipts WHERE group_id=? AND actor=? AND at>=?',
                                                            (str(group),str(actor),min(r[1] for r in rows))))
            return ids
        else:
            rows=self.db.execute('SELECT message_id,at FROM messages WHERE group_id=? AND delivered=1 AND at>=? ORDER BY id DESC LIMIT 12',
                                 (str(group),since)).fetchall()
            ids={str(r[0]) for r in rows}
            if rows:
                ids.update(str(r[0]) for r in self.db.execute('SELECT message_id FROM sent_message_receipts WHERE group_id=? AND at>=?',
                                                            (str(group),min(r[1] for r in rows))))
            return ids

    def is_own_message(self, group, message_id):
        return self.db.execute("SELECT 1 FROM messages WHERE group_id=? AND message_id=? AND role='assistant' AND delivered=1 UNION ALL SELECT 1 FROM sent_message_receipts WHERE group_id=? AND message_id=? LIMIT 1",
                               (str(group),str(message_id),str(group),str(message_id))).fetchone() is not None

    def sent_receipt(self, group, actor, conv, message_id):
        self.db.execute('INSERT OR IGNORE INTO sent_message_receipts VALUES (?,?,?,?,?)',
                        (str(group),str(message_id),conv,str(actor),time.time()))
        self.db.commit()

    def recall(self, group, query,*,exclude_message_ids=()):
        """Bounded local recall with original speaker/time, never a web query."""
        clean = str(query or '').strip(' 。！？!?~～….,，')
        # Short generic conversational reactions ("不感兴趣", "好无聊", "真没意思") refer to
        # the current turn in history, not an arbitrary old message sharing bigrams.
        if len(clean) <= 6 and re.fullmatch(r'(?:真|太|好|有点|完全|实在|根本)?(?:不感兴趣|没兴趣|不怎么感兴趣|没意思|无聊|不想听|不想聊|不想看|算了|随便)(?:吧|啦|了|呢|啊|呀)?', clean):
            return []
        def terms(text):
            result = set(re.findall(r'[a-zA-Z]{3,}', text.casefold()))
            for run in re.findall(r'[\u4e00-\u9fff]{2,}', text):
                result.update(run[i:i+2] for i in range(min(len(run)-1,80)))
            return result
        wanted = terms(query)
        if not wanted:
            return []
        rows = self.db.execute('SELECT id,actor,role,content,message_id,at FROM messages WHERE group_id=? AND delivered=1 ORDER BY id DESC LIMIT 200', (str(group),)).fetchall()
        from tool_protocol import contains_tool_protocol
        rows=[r for r in rows if str(r[4]) not in exclude_message_ids and (r[2]!='assistant' or not contains_tool_protocol(r[3]))]
        scored = [(len(wanted & terms(row[3])), row) for row in rows]
        scored = sorted((pair for pair in scored if pair[0] >= 2), key=lambda pair:(pair[0], pair[1][0]), reverse=True)[:3]
        return [{'role': row[2], 'speaker': row[1] if row[2]=='user' else 'Alice', 'text': row[3][:400],
                 'message_id': row[4], 'at': row[5]} for score,row in scored]


THREAD_SECONDS = 120      # 一段群对话的静默窗口：有人接着说话就续上
THREAD_TURNS = 10         # 一条线最多进模型的句数，防拉长
THREAD_MAX_SECONDS = 600  # 一条线从起头到散场的上限


class Gate:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.active = {}
        self.stopped = {}
        self.group_active = {}

    def permits(self, group):
        if str(group).startswith('private:'):
            return bool(self.config.get('enabled')) and str(group)[8:] in {str(u) for u in self.config.get('allowed_private_users', [])}
        return bool(self.config.get('enabled')) and str(group) in {str(g) for g in self.config.get('allowed_groups', [])}

    def admit(self, e, now=None):
        now = time.time() if now is None else now
        if not self.config.get('enabled') or e.get('post_type') != 'message' or e.get('message_type') not in {'group', 'private'}:
            return None
        private = e.get('message_type') == 'private'
        actor = str(e.get('user_id', ''))
        group = 'private:' + actor if private else str(e.get('group_id', ''))
        if not self.permits(group):
            return None
        if not actor or actor == str(e.get('self_id')) or not e.get('message_id'):
            return None
        try:
            sent_at = float(e.get('time', 0))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(sent_at) or abs(now - sent_at) > 30:
            return None
        segments = e.get('message')
        if not isinstance(segments, list):  # Never parse CQ strings as trusted segments.
            return None
        if len(segments) > 100 or not all(isinstance(s, dict) and isinstance(s.get('data', {}), dict) for s in segments):
            return None
        mentions = {str(s.get('data', {}).get('qq')) for s in segments if s.get('type') == 'at'}
        addressed = str(e.get('self_id')) in mentions
        quoted_self = any(s.get('type') == 'reply' and self.store.is_own_message(group, s.get('data', {}).get('id')) for s in segments)
        text = ''.join(str(s.get('data', {}).get('text', '')) for s in segments if s.get('type') == 'text').strip()
        if not text and any(s.get('type') == 'image' for s in segments):
            text = '（发来一张图片）'
        if not text and any(s.get('type')=='record' for s in segments):text='（发来一段语音）'
        if not text and addressed:text='（@了你）'
        key = (group, actor)
        expiry, turns = self.active.get(key, (0, 0))
        reply_targets = {str(s.get('data', {}).get('id')) for s in segments if s.get('type') == 'reply'}
        if not text or len(text) > 2000 or (mentions and not addressed):
            return None
        stop = text.casefold().strip('。.!！ ') in {
            '先不聊了', '不用回复', '停止回复', '暂停', '/stop', 'stop',
            '你可以退下了', '可以退下了', '退下吧', '你退下吧', '别回了',
            '别说话了', '不用再回复了', '你先别说话了', '先闭嘴',
        }
        # 群里的「这段对话」不是一对一，而是她参与过的一条线：
        # 同一个人在窗口内接着说、或有人用 QQ「回复」接上线里任何一条，都算续聊；
        # 第三个人随便聊不打断这条线，也不会进模型。
        thread = None if private else self.group_active.get(group)
        thread_live = bool(thread and now <= thread.get('expiry', 0) and thread.get('turns', 0) < THREAD_TURNS)
        same_actor = bool(thread_live and actor in thread.get('actors', set()))
        thread_reply = bool(thread_live and reply_targets & thread.get('ids', set()))
        continuation = thread_reply or same_actor
        can_stop = stop and (now <= expiry or thread_live)
        if not private and not addressed and not quoted_self and not continuation and not can_stop:
            return None
        event_key = ':'.join((str(e.get('self_id')), group, actor, str(e['message_id'])))
        if not self.store.first_seen(event_key, now):
            return None
        if stop:
            if not private:
                self.group_active.pop(group, None)
            self.active.pop(key, None)
            self.stopped[key] = now
            return None
        self.active[key] = (now + 120, 1 if addressed else turns + 1)
        if not private:
            if not thread_live:
                thread = {'actors': set(), 'ids': set(), 'started': now, 'turns': 0, 'expiry': now + THREAD_SECONDS}
            thread.setdefault('actors', set()).add(actor)
            thread.setdefault('ids', set()).add(str(e['message_id']))
            thread['turns'] = int(thread.get('turns', 0)) + 1
            thread['expiry'] = now + THREAD_SECONDS
            if thread['turns'] > THREAD_TURNS or now - float(thread.get('started', now)) > THREAD_MAX_SECONDS:
                self.group_active.pop(group, None)
            else:
                self.group_active[group] = thread
        if private:
            e = {**e, 'group_id': group}  # Local conversation scope, not a QQ group.
        return {'event': e, 'text': text, 'key': event_key, 'actor': actor, 'group': group, 'received_at': now,
                'trigger': 'private' if private else 'mention' if addressed else 'conversation_continuation' if continuation else 'reply_to_alice'}


class OneBot:
    """One reader demultiplexes API receipts and messages; bounded work queue."""
    def __init__(self, config, store, brain):
        self.config, self.store, self.brain = config, store, brain
        self.gate = Gate(config, store)
        self.queue = asyncio.Queue(maxsize=8)
        self.pending = {}
        self.ws = None
        self.sequence = 0
        self.account = None
        self.foreground_work = None
        from social_memory import SocialMemory
        self.social_memory=SocialMemory(config,store)
        if hasattr(brain,'bind_social_memory'):
            brain.bind_social_memory(self.social_memory)
        if hasattr(brain, 'bind_transport'):
            brain.bind_transport(self.api, self.work_permitted)
            from sticker_observer import StickerObserver
            self.observer = StickerObserver(config,store,brain,self.api,self.work_permitted)
        else:
            self.observer = None
        from proactive_chat import ProactiveChat
        self.proactive = ProactiveChat(self)
        from cognition_bridge import Observer
        self.cognition = Observer(config, store)

    def work_permitted(self, work):
        ordinary = self.ws is not None and self.gate.permits(work['group']) and self.gate.stopped.get((work['group'],work['actor']),0) < work['received_at']
        if work.get('trigger') == 'proactive':
            from proactive_chat import enabled
            return ordinary and enabled(self.config,work['group']) and time.time()-work['received_at']<=90
        return ordinary

    async def api(self, action, params):
        self.sequence += 1
        echo = str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send(json.dumps({'action': action, 'params': params, 'echo': echo}))
            result = await asyncio.wait_for(future, 15)
            if result.get('status') != 'ok' or result.get('retcode', 0) != 0:
                raise RuntimeError('onebot_action_failed')
            return result.get('data') or {}
        finally:
            self.pending.pop(echo, None)

    async def reader(self):
        async for raw in self.ws:
            try:
                e = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(e, dict):
                continue
            if 'echo' in e:
                future = self.pending.get(str(e['echo']))
                if future and not future.done():
                    future.set_result(e)
                continue
            if self.account is None or str(e.get('self_id')) != self.account:
                continue
            self.social_memory.observe(e)
            self.cognition.offer(e)
            if self.observer:
                self.observer.offer(e)
            self.enqueue_event(e)
            self.proactive.offer(e)

    def enqueue_event(self, event):
        work = self.gate.admit(event)
        if work:
            try:
                self.queue.put_nowait(work)
            except asyncio.QueueFull:
                self.store.metric('queue_full', {})

    async def poll_private_once(self):
        # The installed connector cannot report private events. Its native
        # friend-history API works; share the same gate, persistent dedup and queue.
        if not self.config.get('enabled') or self.account is None:
            return
        for user in list(self.config.get('allowed_private_users', [])):
            if not self.gate.permits('private:' + str(user)):
                continue
            recent = await self.api('get_friend_msg_history', {'user_id': int(user), 'count': 10})
            rows = recent.get('messages', [])
            for message in sorted(rows, key=lambda m: float(m.get('time') or 0)):
                if (message.get('group_id') in (None,0,'0','') and str(message.get('user_id')) in {str(user),str(self.account)}
                        and float(message.get('time') or 0)>=float(self.config.get('private_conversation_started_at',0))):
                    self.social_memory.observe({**message,'message_type':'private','self_id':self.account},
                        scope='private:'+str(user),source_kind='native_private_history')
                if str(message.get('user_id')) != str(user) or message.get('group_id') not in (None, 0, '0', ''):
                    continue
                self.enqueue_event({**message, 'post_type': 'message', 'message_type': 'private', 'self_id': self.account})

    async def poll_private(self):
        while True:
            delay = 3
            try:
                await self.poll_private_once()
            except Exception as exc:
                self.store.metric('private_history_failed', {'error_type': type(exc).__name__})
                delay = 30
            await asyncio.sleep(delay)

    async def worker(self):
        while True:
            work = await self.queue.get()
            self.foreground_work = work
            lease = None
            try:
                from activity import acquire
                lease = await acquire(self.store,'text')
                if time.time() - work['received_at'] > int(self.config.get('queued_turn_ttl_seconds',180)) or self.ws is None:
                    continue
                if not self.gate.permits(work['group']):
                    continue
                if self.gate.stopped.get((work['group'], work['actor']), 0) >= work['received_at']:
                    continue
                e, text = work['event'], work['text']
                from qq_reply import reply_complaint
                verdict, reason = reply_complaint(text)
                if verdict:
                    self.store.mark_reply_quality(work['group'], work['actor'], verdict, reason, text)
                if any(s.get('type')=='record' for s in e.get('message',[])):
                    work=await self.prepare_voice_input(work);e,text=work['event'],work['text']
                    if not self.work_permitted(work):continue
                history = self.store.history(work['group'], work['actor'], since=self.config.get('private_conversation_started_at', 0)
                                             if e.get('message_type') == 'private' else 0)
                work={**work,'history_message_ids':self.store.history_message_ids(work['group'],work['actor'],since=self.config.get('private_conversation_started_at',0) if e.get('message_type')=='private' else 0)}
                reply, conv = await self.brain.reply(work, history)
                if isinstance(reply, dict) and reply.get('silent'):
                    # 她看了这条决定不接（只在她不是被点名时允许）。不发、不记账。
                    self.store.metric('qq_silent_turn', {'group': work['group'], 'actor': work['actor'],
                        'trigger': work.get('trigger'), 'source_message_id': str(e.get('message_id')),
                        'reason': str(reply.get('reason') or '')[:160]})
                    continue
                from qq_reply import performative_repair
                reply_text = reply if isinstance(reply, str) else str(reply.get('text') or '')
                if verdict and performative_repair(reply_text):
                    self.store.metric('qq_repair_pattern', {
                        'group': work['group'], 'actor': work['actor'],
                        'source_message_id': str(e.get('message_id')),
                        'verdict': verdict, 'reason': reason,
                        'reply_chars': len(reply_text), 'reply': reply_text[:200]})
                await self.send_reply(work,reply,conv)
            except Exception as exc:
                # No auto resend: a lost receipt may have followed a successful send.
                self.store.metric('turn_failed', {'error_type': type(exc).__name__})
                print('QQ turn failed:', type(exc).__name__, flush=True)
            finally:
                if lease:await lease.close()
                self.foreground_work = None
                self.queue.task_done()

    async def prepare_voice_input(self,work):
        event=work['event'];transcript='';source='';error=''
        try:
            if not self.config.get('voice_messages_enabled',False):raise RuntimeError('voice_messages_disabled')
            try:
                result=await self.api('fetch_ptt_text',{'message_id':event['message_id']})
                transcript=str(result.get('text') or '').strip();source='qq_native_transcription'
            except Exception:pass
            if not transcript:
                import base64
                from voice_messages import transcribe_wav,pcm_for_asr,reserve_call,finish_call
                segment=next(s for s in event['message'] if s.get('type')=='record')
                file=str(segment.get('data',{}).get('file') or '')
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}',file):raise ValueError('non_native_voice_file')
                audio=await self.api('get_record',{'file':file,'out_format':'wav'})
                encoded=str(audio.get('base64') or '')
                if len(encoded)>8*1024*1024:raise ValueError('voice_over_size_limit')
                data=base64.b64decode(encoded,validate=True)
                pcm_for_asr(data)  # Reject silence/unsupported inputs before a paid reservation.
                if not self.work_permitted(work):raise RuntimeError('turn_cancelled')
                call_id=reserve_call(self.store,self.config,'transcribe',work.get('key',''))
                try:
                    result=await transcribe_wav(data,self.config);finish_call(self.store,call_id,True)
                except BaseException:
                    finish_call(self.store,call_id,False);raise
                transcript=result['text'];source=result['source']
            if not transcript or len(transcript)>4000:raise ValueError('invalid_voice_transcript')
        except Exception as exc:error=type(exc).__name__
        status='transcribed' if transcript else 'unavailable'
        voice_input={'status':status,'source':source or 'unavailable','human_verified':False}
        if error:voice_input['error_type']=error
        updated={**event,'voice_input':voice_input}
        text=work['text']
        if transcript:
            caption=''.join(str(s.get('data',{}).get('text','')) for s in event.get('message',[]) if s.get('type')=='text').strip()
            text=(caption+'\n' if caption else '')+transcript
            updated['message']=[*event['message'],{'type':'text','data':{'text':transcript}}]
        else:text='（发来一段语音，但目前未取得转写）'
        self.store.metric('qq_voice_input',{'source_key':work.get('key'),'source_message_id':str(event['message_id']),**voice_input})
        self.cognition.offer(updated)
        return {**work,'text':text,'event':updated}

    async def send_reply(self,work,reply,conv):
        from qq_reply import reply_parts
        parts=reply_parts(reply)
        if not parts:
            raise RuntimeError('empty_reply_parts')
        if not self.work_permitted(work):
            return
        event=work['event']
        private=event.get('message_type')=='private'
        quote=bool(isinstance(reply,dict) and reply.get('quote_current'))
        quote_id=str(reply.get('quote_target') or event['message_id']) if isinstance(reply,dict) else str(event['message_id'])
        sent=[]
        source_text=work['text']
        if event.get('voice_input',{}).get('status')=='transcribed':
            source_text='[QQ语音自动转写，未人工核验]\n'+source_text
        self.store.message(conv,event,'user',source_text,event['message_id'],True)
        try:
            for index,part in enumerate(parts):
                if not self.work_permitted(work):
                    break
                receipt=await self.api('send_private_msg' if private else 'send_group_msg',{
                    'user_id' if private else 'group_id':int(work['actor']) if private else event['group_id'],
                    'message':([{'type':'reply','data':{'id':quote_id}}] if quote and index==0 else [])+part['segments']})
                if not receipt.get('message_id'):
                    raise RuntimeError('send_receipt_missing')
                sent.append((receipt['message_id'],part['spoken']))
                self.store.sent_receipt(work['group'],work['actor'],conv,receipt['message_id'])
        finally:
            if sent:
                # Keep a single logical turn, but retain every real QQ receipt.
                # A partial failure records only the bubbles actually acknowledged.
                self.store.message(conv,event,'assistant','\n\n'.join(p[1] for p in sent),sent[0][0],True)
                self.social_memory.remember_delivery(work,sent)
                self.cognition.offer_delivery(work, sent)
                if isinstance(reply,dict):self.social_memory.remember_visual_reply(work,reply.get('visual_evidence',[]),sent)
                self.store.metric('reply_delivery',{'group':work['group'],'actor':work['actor'],
                    'trigger':work.get('trigger','directed'),
                    'source_key':work.get('key'),'source_message_id':str(event['message_id']),
                    'message_ids':[str(p[0]) for p in sent],'parts_sent':len(sent),'parts_planned':len(parts),
                    'text_placement':reply.get('text_placement','together') if isinstance(reply,dict) else None})
                print('QQ reply delivered',work['group'],work['actor'],'bubbles=',len(sent),flush=True)
                if work.get('trigger')=='proactive':
                    self.gate.active[(work['group'],work['actor'])]=(time.time()+120,1)
                if not str(work['group']).startswith('private:'):
                    # 她发的每条也进这条线，别人用 QQ「回复」接她的话才能续上。
                    g = self.gate.group_active.get(work['group'])
                    if g:
                        g.setdefault('ids', set()).update(str(p[0]) for p in sent)
                        g['expiry'] = time.time() + THREAD_SECONDS
        return {'parts_sent':len(sent),'parts_planned':len(parts)}

    async def bootstrap_social_contexts(self):
        self.social_memory.seed_delivered_dialogues()
        try:
            data=await self.api('get_group_list',{})
            groups=data if isinstance(data,list) else data.get('groups',[])
            for group in groups:
                scope=str(group.get('group_id',''))
                if self.social_memory.allowed(scope):
                    self.social_memory.context_name(scope,group.get('group_name',''))
                    recent=await self.api('get_group_msg_history',{'group_id':int(scope),'count':20})
                    for message in recent.get('messages',[]):
                        self.social_memory.observe({**message,'message_type':'group','group_id':scope,'self_id':self.account},
                            source_kind='native_group_history')
            for user in self.config.get('allowed_private_users',[]):
                recent=await self.api('get_friend_msg_history',{'user_id':int(user),'count':40})
                for message in recent.get('messages',[]):
                    if str(message.get('user_id')) in {str(user),str(self.account)} and float(message.get('time') or 0)>=float(self.config.get('private_conversation_started_at',0)):
                        self.social_memory.observe({**message,'message_type':'private','self_id':self.account},
                            scope='private:'+str(user),source_kind='native_private_history')
        except Exception as exc:
            self.store.metric('qq_context_bootstrap_failed',{'error_type':type(exc).__name__})

    async def keep_social_contexts(self):
        await self.bootstrap_social_contexts()
        # A completed bootstrap must not look like a disconnected reader.
        await asyncio.Event().wait()

    async def run(self):
        import websockets
        from websockets.exceptions import ConnectionClosed
        while True:
            tasks = []
            try:
                async with websockets.connect(self.config['onebot_url'], additional_headers={
                    'Authorization': 'Bearer ' + self.config['onebot_token']
                }, max_size=8 * 1024 * 1024) as ws:
                    self.ws = ws
                    tasks.append(asyncio.create_task(self.reader()))
                    login = await self.api('get_login_info', {})
                    self.account = str(login['user_id'])
                    expected = str(self.config.get('expected_qq') or '')
                    if not expected or expected != self.account:
                        raise RuntimeError('unexpected_qq_account')
                    print('QQ connected; release=v14-source-aware-replies; policy=mention_or_reply; quote_default=off; search=', bool(self.config.get('search_enabled',False)), '; sticker_search=', bool(self.config.get('sticker_search_enabled',False)), '; ambient_stickers=', bool(self.config.get('ambient_stickers_enabled',True)), '; enabled groups:', len(self.config.get('allowed_groups', [])), '; private users:', len(self.config.get('allowed_private_users', [])), flush=True)
                    tasks.append(asyncio.create_task(self.worker()))
                    tasks.append(asyncio.create_task(self.poll_private()))
                    tasks.append(asyncio.create_task(self.keep_social_contexts()))
                    if self.observer:
                        tasks.append(asyncio.create_task(self.observer.run()))
                    tasks.append(asyncio.create_task(self.proactive.run()))
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
            except (OSError, ConnectionClosed, asyncio.TimeoutError):
                print('QQ disconnected; reconnect in 5s', flush=True)
            finally:
                self.ws, self.account = None, None
                self.gate.active.clear()
                for future in self.pending.values():
                    if not future.done():
                        future.cancel()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                while not self.queue.empty():
                    self.queue.get_nowait()
                    self.queue.task_done()
            await asyncio.sleep(5)
