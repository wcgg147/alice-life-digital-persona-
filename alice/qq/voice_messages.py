"""One bounded synthesis using the already configured project voice provider."""
import asyncio
import base64
import json
import os
import io
import wave
import time
import subprocess
from datetime import datetime,timedelta,timezone
from pathlib import Path


def reserve_call(store,config,kind,source_key):
    store.db.execute('CREATE TABLE IF NOT EXISTS qq_voice_calls(id INTEGER PRIMARY KEY,at REAL,day TEXT,kind TEXT,source_key TEXT,status TEXT)')
    day=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    count=store.db.execute('SELECT COUNT(*) FROM qq_voice_calls WHERE day=?',(day,)).fetchone()[0]
    if count>=int(config.get('voice_calls_per_day',300)):raise RuntimeError('voice_daily_cap')
    store.reserve(config.get('daily_call_cap',300))
    cur=store.db.execute('INSERT INTO qq_voice_calls(at,day,kind,source_key,status) VALUES (?,?,?,?,?)',(time.time(),day,kind,source_key,'reserved'))
    store.db.commit();return cur.lastrowid


def finish_call(store,call_id,success):
    store.db.execute('UPDATE qq_voice_calls SET status=? WHERE id=?',('success' if success else 'failed',call_id));store.db.commit()


async def _request(payload,config):
    root=Path(config['product_root']).resolve()
    from dotenv import dotenv_values
    values=dotenv_values(root/'deployment/.env')
    env=os.environ.copy()
    for key in ('DASHSCOPE_API_KEY','ALI_REALTIME_MODEL','ALI_VOICE','ALI_TTS_TRANSPORT','ALI_TTS_MODEL','ALI_TTS_VOICE'):
        if values.get(key):env[key]=values[key]
    if not env.get('DASHSCOPE_API_KEY'):raise RuntimeError('project_voice_not_configured')
    node=Path(config['voice_node_path'])
    if not node.is_file():raise RuntimeError('voice_node_missing')
    process=await asyncio.create_subprocess_exec(str(node),str(Path(__file__).with_name('qq_voice_helper.mjs')),str(root),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        stdout,stderr=await asyncio.wait_for(process.communicate(json.dumps(payload,ensure_ascii=False).encode()),40)
    except BaseException:
        if process.returncode is None:process.kill();await process.wait()
        raise
    if process.returncode or len(stdout)>10*1024*1024:raise RuntimeError('voice_synthesis_failed')
    return json.loads(stdout)


async def synthesize(text,config):
    from tool_protocol import contains_tool_protocol
    text=str(text).strip()
    if not text or len(text)>240 or contains_tool_protocol(text):raise ValueError('invalid_voice_text')
    result=await _request({'text':text},config)
    audio=base64.b64decode(result.pop('audio_base64',''),validate=True)
    if not audio or len(audio)>6*1024*1024 or not (audio.startswith(b'RIFF') or audio.startswith(b'OggS')):
        raise ValueError('invalid_generated_voice')
    return {'audio':audio,**result}


def pcm_for_asr(data):
    import audioop
    if len(data)>6*1024*1024:raise ValueError('voice_over_6MiB')
    with wave.open(io.BytesIO(data),'rb') as f:
        channels,width,rate=f.getnchannels(),f.getsampwidth(),f.getframerate()
        duration=f.getnframes()/rate
        if channels not in (1,2) or duration>60 or f.getcomptype()!='NONE':raise ValueError('unsupported_voice_format_or_over_60_seconds')
        pcm=f.readframes(f.getnframes())
    if width!=2:pcm=audioop.lin2lin(pcm,width,2)
    if channels==2:pcm=audioop.tomono(pcm,2,.5,.5)
    if rate!=16000:pcm,_=audioop.ratecv(pcm,2,1,rate,16000,None)
    if not pcm or audioop.rms(pcm,2)<10:raise ValueError('silent_voice')
    return pcm,duration


async def transcribe_wav(data,config):
    pcm,duration=pcm_for_asr(data)
    result=await _request({'mode':'transcribe','pcm_base64':base64.b64encode(pcm).decode()},config)
    text=str(result.get('transcript') or '').strip()
    if not text or len(text)>4000:raise ValueError('invalid_voice_transcript')
    return {'text':text,'duration':duration,'model':result.get('model'),'source':'project_voice_asr'}
