"""Owner-only QQ call supervisor. Native call state gates model audio and attention."""
from __future__ import annotations
import argparse,asyncio,json,os,re,subprocess,sys,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
import httpx
from activity import acquire
from channel import Store
from run import config_from
from call_actions import native_end_receipt
from call_health import provider_blocked


def valid_end_request(item,call_key):
    return (item.get('type')=='native_tool_request' and item.get('name')=='qq_end_voice_call'
        and item.get('native_call_key')==call_key and isinstance(item.get('call_id'),str) and 0<len(item['call_id'])<=128
        and isinstance(item.get('response_id'),str) and bool(item['response_id'])
        and isinstance(item.get('reason'),str) and 0<len(item['reason'].strip())<=160
        and item.get('timing') in ('now','after_current_speech')
        and set(item)=={'type','name','native_call_key','call_id','response_id','reason','timing'})


def budget_available(store,default_cap):
    day=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    row=store.db.execute('SELECT cap FROM budget_day_overrides WHERE day=?',(day,)).fetchone()
    cap=row[0] if row else default_cap
    used=store.db.execute('SELECT calls FROM budget WHERE day=?',(day,)).fetchone()
    return cap is None or (used[0] if used else 0)+2<=cap


async def main(args):
    cfg=config_from(args.config);runtime=Path(args.config).resolve().parent
    bridge=json.loads((runtime/'native-call-bridge.json').read_text(encoding='utf-8'))
    if bridge['owner_qq']!=cfg['account_owner_qq'] or bridge['expected_qq']!=cfg['expected_qq']:raise RuntimeError('call_bridge_account_mismatch')
    store=Store(runtime/'channel.sqlite3');url='http://127.0.0.1:'+str(bridge['port'])
    stop=runtime/'native-call.stop';lease=None;audio=None;key='';ready=False;gate_opened=False;reader=None
    gate=None;log=None;last_error='';failed_keys=set();released_keys=set();action_task=None
    flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    def record(kind,**data):
        row={'at':time.time(),'kind':kind,**data}
        with (runtime/'native-call-service.events.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(json.dumps(row,ensure_ascii=False),flush=True)
    def set_gate(active):
        if gate:
            temp=gate.with_suffix('.tmp');temp.write_text(json.dumps({'callKey':key,'active':bool(active)}),encoding='utf-8');temp.replace(gate)
    def health(available):
        path=runtime/'native-call-health.json';temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps({'at':time.time(),'available':available,'account':cfg['expected_qq'],
            'owner':cfg['account_owner_qq']}),encoding='utf-8');temp.replace(path)
    async def stop_audio():
        nonlocal audio,lease,reader,log,ready,key,gate,gate_opened,action_task
        set_gate(False)
        if audio and audio.returncode is None:
            stop.write_text(key,encoding='utf-8')
            try:await asyncio.wait_for(audio.wait(),7)
            except asyncio.TimeoutError:
                if os.name=='nt':
                    killer=await asyncio.create_subprocess_exec('taskkill','/PID',str(audio.pid),'/T','/F',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,**flags);await killer.wait()
                else:audio.kill();await audio.wait()
        if reader:
            reader.cancel();await asyncio.gather(reader,return_exceptions=True)
        if action_task:
            action_task.cancel();await asyncio.gather(action_task,return_exceptions=True);action_task=None
        if log:log.close()
        if lease:await lease.close()
        if stop.exists():stop.unlink()
        if gate and gate.exists():gate.unlink()
        audio=lease=reader=log=gate=None;ready=False;key='';gate_opened=False
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+bridge['token']},timeout=4) as client:
        async def api(path,body=None):
            response=await client.get(url+path) if body is None else await client.post(url+path,json=body)
            response.raise_for_status();return response.json()
        async def execute_end(item,child,call_key):
            result={'native_call_key':call_key,'call_id':item['call_id'],'ok':False,'ended':False,'local_window_released':False}
            record('persona_native_end_requested',native_call_key=call_key,**{k:item[k] for k in ('call_id','response_id','reason','timing')})
            try:
                result.update(await native_end_receipt(api,call_key))
            except (httpx.HTTPError,OSError,ValueError) as exc:result['error_type']=type(exc).__name__
            path=runtime/(call_key+'.action-result.json');temp=path.with_suffix('.tmp')
            temp.write_text(json.dumps(result),encoding='utf-8');temp.replace(path)
            record('persona_native_end_result',**result)
            # Keep the audio child alive long enough to write the real tool receipt
            # back to its model session, even though the QQ window has now closed.
            if result['ok']:
                try:await asyncio.wait_for(child.wait(),5)
                except asyncio.TimeoutError:pass
        async def begin(call):
            nonlocal lease,audio,key,gate,log,reader,ready,gate_opened
            call_key=call['callKey']
            if not re.fullmatch(r'qq-call-\d+-\d+',call_key):raise RuntimeError('invalid_native_call_key')
            lease=await acquire(store,'native-call-reservation:'+call_key,timeout=0)
            key=call_key;gate=runtime/(key+'.gate.json');set_gate(False)
            (runtime/(key+'.action-result.json')).unlink(missing_ok=True)
            if stop.exists():stop.unlink()
            log=(runtime/(key+'.audio.log')).open('ab')
            command=[sys.executable,str(Path(__file__).with_name('call_audio.py')),'--config',str(Path(args.config).resolve()),
                '--audio-python',args.audio_python,'--owner-call-test','--native-call-key',key,'--attention-owner',lease.owner,
                '--gate-file',str(gate),'--seconds','1800','--max-responses','300']
            audio=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=log,**flags)
            child=audio
            async def read_ready():
                nonlocal ready,action_task
                seen=set()
                while line:=await child.stdout.readline():
                    log.write(line);log.flush()
                    try:
                        item=json.loads(line)
                        if item.get('ready'):ready=True;record('audio_backend_ready',native_call_key=key)
                        elif valid_end_request(item,call_key) and item['call_id'] not in seen and (action_task is None or action_task.done()):
                            seen.add(item['call_id']);action_task=asyncio.create_task(execute_end(item,child,call_key))
                    except ValueError:pass
            reader=asyncio.create_task(read_ready());record('owner_call_preparing',native_call_key=key)
        record('service_started',owner_only=True,automatic_device_selection=True)
        try:
            while True:
                try:
                    status=await api('/status');last_error='';health(not provider_blocked(cfg,runtime))
                    for window in status['windows']:
                        released=window.get('callKey')
                        if released and window.get('localWindowReleased') and released not in released_keys:
                            released_keys.add(released);record('owner_call_local_released',native_call_key=released)
                    active=[w for w in status['windows'] if w.get('ownerVerified') and not w.get('destroyed') and w.get('phase') in ('waiting','accepting','connected','ending')]
                    current=next((w for w in active if w.get('callKey')==key),None)
                    if key and action_task and not action_task.done():
                        set_gate(False);await api('/lease',{'canAccept':False});await asyncio.sleep(.1);continue
                    if key and (not current or current['phase']=='ending'):
                        record('native_call_ended',native_call_key=key);await stop_audio();current=None
                    if not key:
                        pending=next((w for w in active if w['phase']=='waiting'),None)
                        if pending and pending['callKey'] not in failed_keys and not provider_blocked(cfg,runtime) and budget_available(store,cfg['daily_call_cap']):
                            try:await begin(pending)
                            except RuntimeError as exc:
                                if str(exc)!='qq_attention_busy':raise
                    if key:
                        current=next((w for w in active if w.get('callKey')==key),None)
                        if audio.returncode is not None:
                            failed_keys.add(key)
                            record('audio_backend_stopped',native_call_key=key,exit_code=audio.returncode)
                            if current:
                                try:await api('/hangup',{'callKey':key,'source':'audio_backend_ended'})
                                except httpx.HTTPStatusError:pass
                            await stop_audio();await api('/lease',{'canAccept':False})
                        elif current:
                            if gate_opened and not current.get('routeVerified'):
                                record('native_audio_route_lost',native_call_key=key)
                                await api('/hangup',{'callKey':key,'source':'audio_backend_ended'});await stop_audio();await api('/lease',{'canAccept':False})
                            else:
                                verified=current['phase']=='connected' and current.get('routeVerified') is True
                                if verified and not gate_opened:gate_opened=True;record('native_audio_gate_opened',native_call_key=key)
                                set_gate(verified);await api('/lease',{'canAccept':bool(ready)})
                    else:await api('/lease',{'canAccept':False})
                except (httpx.HTTPError,OSError,ValueError) as exc:
                    health(False)
                    if last_error!=type(exc).__name__:record('native_bridge_unavailable',error_type=type(exc).__name__);last_error=type(exc).__name__
                    await stop_audio()
                await asyncio.sleep(.35)
        finally:
            health(False)
            try:await api('/lease',{'canAccept':False})
            except httpx.HTTPError:pass
            await stop_audio();store.db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--audio-python',required=True)
    asyncio.run(main(p.parse_args()))
