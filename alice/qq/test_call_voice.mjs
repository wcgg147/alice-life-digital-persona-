import test from 'node:test';
import assert from 'node:assert/strict';
import { CallSession, parseEndCall, endCallTool, providerFailure } from './qq_call_voice_helper.mjs';

test('barge-in clears the current response and rejects its late PCM', () => {
  const sent=[],events=[]; const ws={send:x=>sent.push(JSON.parse(x)),close:()=>{}};
  const s=new CallSession(ws,x=>events.push(x),{instructions:'test'});
  s.receive({type:'session.created'});s.command({type:'credit'});
  s.receive({type:'response.created',response:{id:'a'}});s.command({type:'credit'});
  s.receive({type:'response.output_audio.delta',response_id:'a',delta:'AAAA'});
  s.receive({type:'input_audio_buffer.speech_started'});
  s.receive({type:'response.output_audio.delta',response_id:'a',delta:'LATE'});
  s.receive({type:'response.created',response:{id:'b'}});
  s.receive({type:'response.output_audio.delta',response_id:'b',delta:'BBBB'});
  assert.deepEqual(events.filter(x=>x.type==='audio').map(x=>x.data),['AAAA','BBBB']);
  assert.equal(events.filter(x=>x.type==='barge_in').length,1);
  assert(sent.some(x=>x.type==='response.cancel'));
});

test('no audio is forwarded before a prepaid response credit or after closure', () => {
  const sent=[];const s=new CallSession({send:x=>sent.push(JSON.parse(x)),close:()=>{}},()=>{},{instructions:'test'});
  s.receive({type:'session.created'});s.command({type:'audio',data:'AAAA'});
  assert.equal(sent.filter(x=>x.type==='input_audio_buffer.append').length,0);
  s.command({type:'credit'});s.command({type:'audio',data:'AAAA'});
  assert.equal(sent.filter(x=>x.type==='input_audio_buffer.append').length,0);
  s.receive({type:'session.created'});s.command({type:'audio',data:'AAAA'});
  assert.equal(sent.filter(x=>x.type==='input_audio_buffer.append').length,1);
  s.close();s.command({type:'audio',data:'AAAA'});
  assert.equal(sent.filter(x=>x.type==='input_audio_buffer.append').length,1);
});

test('tools use acknowledged setup, with no forced choice or enabled search', () => {
  const sent=[],events=[];const s=new CallSession({send:x=>sent.push(JSON.parse(x)),close:()=>{}},x=>events.push(x),{});
  s.receive({type:'session.created'});
  assert.deepEqual(sent,[{type:'session.update',session:{tools:[endCallTool]}}]);
  assert.equal(s.ready,false);s.receive({type:'session.created'});assert.equal(s.ready,true);
  assert.equal(events.filter(x=>x.type==='ready').length,1);
});

test('farewell transcript has no end action; model function can end without speech', () => {
  const sent=[],events=[];let closed=false;
  const s=new CallSession({send:x=>sent.push(JSON.parse(x)),close:()=>{closed=true;}},x=>events.push(x),{});
  s.command({type:'credit'});s.receive({type:'response.created',response:{id:'r1'}});
  s.receive({type:'conversation.item.input_audio_transcription.completed',transcript:'先挂了'});
  assert.equal(events.some(x=>x.type==='end_decision'),false);
  const call={type:'response.function_call_arguments.done',response_id:'r1',call_id:'c1',name:'qq_end_voice_call',
    arguments:JSON.stringify({reason:'今天不想继续聊了',timing:'now'})};
  s.receive(call);s.receive(call);
  assert.equal(events.filter(x=>x.type==='end_decision').length,1);
  assert.equal(closed,false); // A model decision is not a successful native receipt.
  s.command({type:'tool_result',call_id:'wrong',ok:true});assert.equal(closed,false);
  s.command({type:'tool_result',call_id:'c1',ok:true,ended:true,local_window_released:true});
  assert.equal(closed,false);
  s.receive({type:'conversation.item.created',item:{type:'function_call_output',call_id:'c1'}});
  assert.equal(closed,true);
  assert.equal(sent.filter(x=>x.type==='conversation.item.create').length,1);
  assert.equal(sent.some(x=>x.type==='response.create'),false);
});

test('stale cancelled tool calls and malformed arguments cannot act', () => {
  const events=[];const s=new CallSession({send:()=>{},close:()=>{}},x=>events.push(x),{});
  s.command({type:'credit'});s.receive({type:'response.created',response:{id:'r1'}});
  s.receive({type:'input_audio_buffer.speech_started'});
  s.receive({type:'response.function_call_arguments.done',response_id:'r1',call_id:'old',name:'qq_end_voice_call',arguments:'{"reason":"old"}'});
  assert.equal(events.some(x=>x.type==='end_decision'),false);
  for(const args of ['{}','{"reason":""}','{"reason":"x","timing":"ask_user"}','{"reason":"x","script":"evil"}'])
    assert.throws(()=>parseEndCall({name:'qq_end_voice_call',call_id:'c',arguments:args}));
  assert.throws(()=>parseEndCall({name:'different_tool',call_id:'c',arguments:'{"reason":"x"}'}));
  assert.equal(parseEndCall({name:'qq_end_voice_call',call_id:'c',arguments:'{"reason":"不想说了","timing":"after_current_speech"}'}).timing,'after_current_speech');
});

test('native failure returns a failed receipt and lets the persona continue', () => {
  const sent=[];let closed=false;const s=new CallSession({send:x=>sent.push(JSON.parse(x)),close:()=>{closed=true;}},()=>{},{});
  s.command({type:'credit'});s.receive({type:'response.created',response:{id:'r'}});s.command({type:'credit'});
  s.receive({type:'response.function_call_arguments.done',response_id:'r',call_id:'c',name:'qq_end_voice_call',arguments:'{"reason":"结束了"}'});
  s.command({type:'tool_result',call_id:'c',ok:false,ended:false,local_window_released:false});
  s.receive({type:'conversation.item.created',item:{type:'function_call_output',call_id:'c'}});
  assert.equal(closed,false);assert.equal(s.pendingEnd,null);
  assert.equal(JSON.parse(sent.find(x=>x.type==='conversation.item.create').item.output).ok,false);
  assert.equal(sent.filter(x=>x.type==='response.create').length,1);
});

test('provider errors during initial setup are surfaced', () => {
  const events=[];const s=new CallSession({close:()=>{}},x=>events.push(x),{instructions:'test'});
  s.receive({type:'error',error:{message:'secret not logged'}});
  assert.deepEqual(events,[{type:'error',error:'voice_provider_error'}]);
});

test('account rejection is identifiable without retaining arbitrary server messages', () => {
  assert.equal(providerFailure(1007,'Access denied, please make sure your account is in good standing.'),'voice_account_unavailable');
  assert.equal(providerFailure('Arrearage',''),'voice_account_unavailable');
  assert.equal(providerFailure(1006,'other failure'),'voice_provider_error');
});
