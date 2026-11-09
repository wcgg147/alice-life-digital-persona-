"""Install the version-pinned owner-call bridge into the current QQ main process.

Uses QQ's Node callback thread and its own Electron/native interfaces. No UI input.
The private loopback token is never printed or given to the conversation model.
"""
import argparse,asyncio,hashlib,json,re,secrets,sqlite3,sys,time
from pathlib import Path
import websockets
import httpx
from run import config_from

BUILD='9.9.35-52892'
AVSDK_SHA256='b9b2f9d61c7205b9f446aa92bb5784319121efd07fe2889ddf8b9a04b719e6b5'

async def verify_account(cfg):
    async with websockets.connect(cfg['onebot_url'],additional_headers={'Authorization':'Bearer '+cfg['onebot_token']}) as ws:
        await ws.send(json.dumps({'action':'get_login_info','params':{},'echo':'native-call-account-check'}))
        while True:
            response=json.loads(await asyncio.wait_for(ws.recv(),10))
            if response.get('echo')=='native-call-account-check':
                if response.get('status')!='ok' or str((response.get('data') or {}).get('user_id'))!=cfg['expected_qq']:raise RuntimeError('native_call_account_not_verified')
                return

def inject(pid,expression,analysis_dir):
    sys.path.insert(0,str(analysis_dir));import frida
    session=frida.attach(pid);result=[]
    code=r'''
const wrapper=Process.getModuleByName('wrapper.node');
if(!wrapper.path.includes('9.9.35-52892'))throw Error('unsupported_loaded_QQ_build');
const qq=Process.getModuleByName('QQNT.dll');
const make=new NativeFunction(qq.getExportByName('napi_create_string_utf8'),'int',['pointer','pointer','uint64','pointer']);
const run=new NativeFunction(qq.getExportByName('napi_run_script'),'int',['pointer','pointer','pointer']);
const utf8=new NativeFunction(qq.getExportByName('napi_get_value_string_utf8'),'int',['pointer','pointer','pointer','uint64','pointer']);
const clear=new NativeFunction(qq.getExportByName('napi_get_and_clear_last_exception'),'int',['pointer','pointer']);
const code=__EXPRESSION__,data=Memory.allocUtf8String(code);let done=false,inside=false;
Interceptor.attach(qq.getExportByName('napi_get_cb_info'),{
 onEnter(args){if(!done&&!inside)this.env=args[0];},
 onLeave(ret){
  if(!this.env||done||inside||ret.toInt32()!==0)return;done=true;inside=true;
  try{
   const str=Memory.alloc(8),value=Memory.alloc(8),buffer=Memory.alloc(16384),size=Memory.alloc(8);
   const a=make(this.env,data,__BYTES__,str),b=a===0?run(this.env,str.readPointer(),value):-1;
   if(a===0&&b===0&&utf8(this.env,value.readPointer(),buffer,16384,size)===0)send({ok:true,result:JSON.parse(buffer.readUtf8String(size.readU64().toNumber()))});
   else{clear(this.env,value);send({ok:false,error:'native_install_failed',createStatus:a,runStatus:b});}
  }catch{try{clear(this.env,Memory.alloc(8));}catch{}send({ok:false,error:'native_install_error'});}
  finally{inside=false;}
 }
});
'''.replace('__EXPRESSION__',json.dumps(expression)).replace('__BYTES__',str(len(expression.encode('utf-8'))))
    script=session.create_script(code)
    def receive(message,data):
        if message.get('type')=='send':result.append(message['payload'])
        elif message.get('type')=='error':result.append({'ok':False,'error':'native_inspection_guard_failed'})
    script.on('message',receive)
    try:
        script.load();deadline=time.monotonic()+35
        while not result and time.monotonic()<deadline:time.sleep(.1)
    finally:session.detach()
    if not result or not result[0].get('ok'):raise RuntimeError((result or [{}])[0].get('error','qq_native_callback_timeout'))
    return result[0]['result']

def main(args):
    cfg=config_from(args.config);runtime=Path(args.config).resolve().parent;source=Path(__file__).resolve().parent
    asyncio.run(verify_account(cfg))
    library=Path(args.qq_app)/'avsdk/AVSDKPlugin.dll'
    if hashlib.sha256(library.read_bytes()).hexdigest()!=AVSDK_SHA256:raise RuntimeError('unsupported_qq_avsdk_library')
    identity=runtime/'snowluma-2.2.1/data'/cfg['expected_qq']/'snowluma_identity.db'
    db=sqlite3.connect(identity.as_uri()+'?mode=ro',uri=True)
    try:rows={str(uin):uid for uin,uid in db.execute('SELECT uin,uid FROM users WHERE uin IN (?,?)',(int(cfg['account_owner_qq']),int(cfg['expected_qq'])))}
    finally:db.close()
    if any(not re.fullmatch(r'u_[A-Za-z0-9_-]{22}',rows.get(k,'')) for k in (cfg['account_owner_qq'],cfg['expected_qq'])):raise RuntimeError('qq_native_identities_missing')
    private=runtime/'native-call-bridge.json'
    old=json.loads(private.read_text(encoding='utf-8')) if private.exists() else {}
    if old.get('token'):
        try:
            response=httpx.get('http://127.0.0.1:'+str(old['port'])+'/status',headers={'Authorization':'Bearer '+old['token']},timeout=3)
            response.raise_for_status()
            if any(w.get('ownerVerified') and not w.get('destroyed') and w.get('phase') in ('waiting','accepting','connected','ending') for w in response.json()['windows']):
                raise RuntimeError('finish_current_owner_call_before_reinstall')
        except (httpx.ConnectError,httpx.ReadTimeout):pass
    bridge={'ownerUid':rows[cfg['account_owner_qq']],'selfUid':rows[cfg['expected_qq']],
        'owner_qq':cfg['account_owner_qq'],'expected_qq':cfg['expected_qq'],'port':13111,
        'token':old.get('token') or secrets.token_hex(32),'testHangupMs':args.test_hangup_seconds*1000,
        'qq_version':BUILD,'avsdk_sha256':AVSDK_SHA256,'qq_pid':args.pid}
    private.write_text(json.dumps(bridge,ensure_ascii=False,indent=2),encoding='utf-8')
    renderer=(source/'native_call_control.mjs').read_text(encoding='utf-8').replace('export class ','class ').replace('export function ','function ')
    bootstrap=(source/'native_call_bootstrap.mjs').read_text(encoding='utf-8').replace('export function ','function ')
    expression='JSON.stringify((()=>{globalThis.__aliceQQCallObserver?.stop();globalThis.__aliceNativeQQCalls?.stop();'+bootstrap+'\nreturn bootstrapNativeQQCalls('+json.dumps(bridge)+','+json.dumps(renderer)+');})())'
    result=inject(args.pid,expression,runtime/'native-analysis-libs')
    print(json.dumps({**result,'qq_build':BUILD,'model_calls':0,'test_hangup_seconds':args.test_hangup_seconds}),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--pid',type=int,required=True)
    p.add_argument('--qq-app',default='F:/versions/'+BUILD+'/resources/app');p.add_argument('--test-hangup-seconds',type=int,default=0,choices=(0,15))
    main(p.parse_args())
