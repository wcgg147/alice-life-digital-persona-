// Reuse the project's configured realtime voice transport, without starting or
// editing the product server. Input/output is private local JSON over stdio.
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
const root=process.argv[2];
const {createDashscopeVoiceSocket}=await import(pathToFileURL(resolve(root,'deployment/voice-transport-dashscope.mjs')));
let input='';for await(const chunk of process.stdin)input+=chunk;
const {text,mode='synthesize',pcm_base64=''}=JSON.parse(input);
if(mode==='synthesize'&&(typeof text!=='string'||!text.trim()||text.length>240))throw Error('invalid_voice_text');
if(!['synthesize','transcribe'].includes(mode))throw Error('invalid_voice_mode');
const inputPcm=mode==='transcribe'?Buffer.from(pcm_base64,'base64'):null;
if(inputPcm && (!inputPcm.length||inputPcm.length>1920000))throw Error('invalid_voice_input');
function wav(pcm){
 const h=Buffer.alloc(44);h.write('RIFF');h.writeUInt32LE(36+pcm.length,4);h.write('WAVEfmt ',8);
 h.writeUInt32LE(16,16);h.writeUInt16LE(1,20);h.writeUInt16LE(1,22);h.writeUInt32LE(24000,24);
 h.writeUInt32LE(48000,28);h.writeUInt16LE(2,32);h.writeUInt16LE(16,34);h.write('data',36);h.writeUInt32LE(pcm.length,40);
 return Buffer.concat([h,pcm]);
}
await new Promise((resolve,reject)=>{
 let settled=false;const chunks=[];let transcript='';let committed=false;let offset=0;
 const ws=createDashscopeVoiceSocket({apiKey:process.env.DASHSCOPE_API_KEY,log:()=>{}});
 const finish=(err)=>{if(settled)return;settled=true;clearTimeout(timer);try{ws.close();}catch{}
  if(err){reject(Error(err));return;}
  if(mode==='transcribe'){
   if(!transcript.trim()){reject(Error('empty_voice_transcript'));return;}
   process.stdout.write(JSON.stringify({ok:true,transcript,model:process.env.ALI_REALTIME_MODEL||'qwen-audio-3.0-realtime-flash'}));resolve();return;
  }
  const pcm=Buffer.concat(chunks);if(!pcm.length){reject(Error('empty_voice_audio'));return;}
  const normalized=s=>s.replace(/[\p{P}\p{S}\s]/gu,'').toLowerCase();
  if(transcript && normalized(transcript)!==normalized(text)){reject(Error('spoken_text_mismatch'));return;}
  const audio=pcm.toString('ascii',0,4)==='OggS'||pcm.toString('ascii',0,4)==='RIFF'?pcm:wav(pcm);
  process.stdout.write(JSON.stringify({ok:true,audio_base64:audio.toString('base64'),transcript,
   model:process.env.ALI_REALTIME_MODEL||'qwen-audio-3.0-realtime-flash',voice:process.env.ALI_VOICE||'longanqian',
   bytes:audio.length,sample_rate:24000}));resolve();};
 const timer=setTimeout(()=>finish('voice_provider_timeout'),35000);
 const appendPart=()=>{
  const part=inputPcm.subarray(offset,offset+24*32000);offset+=part.length;
  ws.send(JSON.stringify({type:'input_audio_buffer.append',audio:part.toString('base64')}));
  ws.send(JSON.stringify({type:'input_audio_buffer.commit'}));
 };
 ws.on('open',()=>{
  ws.send(JSON.stringify({type:'session.create',session:{id:'qq-voice-'+Date.now(),model:'1.2.6.1',
   instructions:'逐字朗读给定文本，不回答、不添加内容。',extension:{controlled_pcm:true,
    asr:{audio_info:{format:'pcm_s16le',sample_rate:16000,channel:1}},
    tts:{speaker:process.env.ALI_VOICE||'longanqian',audio_info:{format:'pcm_s16le',sample_rate:24000,channel:1}}}}}));
 });
 ws.on('message',raw=>{let m;try{m=JSON.parse(String(raw));}catch{return;}
  if(m.type==='session.created'&&!committed){committed=true;if(mode==='transcribe')appendPart();else ws.send(JSON.stringify({type:'speech_text_buffer.commit',text}));}
  if(mode==='transcribe'&&m.type==='conversation.item.input_audio_transcription.completed'){
   transcript+=(transcript?' ':'')+(m.transcript||m.delta||'');
   if(offset<inputPcm.length)appendPart();else finish();
  }
  if(m.type==='response.output_audio.delta'&&m.delta)chunks.push(Buffer.from(m.delta,'base64'));
  if(m.type==='response.output_text.delta')transcript+=m.delta||'';
  if(m.type==='response.output_text.done'&&(m.text||m.transcript))transcript=m.text||m.transcript;
  if(m.type==='response.done'&&mode==='synthesize')finish();
  if(m.type==='error')finish('voice_provider_error');
 });
 ws.on('error',()=>finish('voice_connection_error'));
});
