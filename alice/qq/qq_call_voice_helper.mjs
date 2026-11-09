// Streaming QQ call experiment. Reuses the project's configured voice transport.
// JSONL only; credentials stay in the child environment, never in IPC/logs.
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { createInterface } from 'node:readline';

export const endCallTool = { type: 'function', function: {
  name: 'qq_end_voice_call',
  description: 'QQ通话里的挂断按钮：执行后双方断开，声音停止。口头告别、说“挂了”或停止说话都不会操作这个按钮。如果你决定结束这通电话，调用此函数落实挂断；想继续保持连接则不调用。是否结束、直接挂还是说完再挂由你自己决定，不需要预先告知或征求同意。reason只作内部简短记录，不会播报。',
  parameters: { type: 'object', properties: {
    reason: { type: 'string', description: '这次决定结束通话的简短依据，不是详细推理。', minLength: 1, maxLength: 160 },
    timing: { type: 'string', enum: ['now', 'after_current_speech'], description: 'now立即结束；after_current_speech等当前语音播放完再结束。' }
  }, required: ['reason'], additionalProperties: false }
} };

export function parseEndCall(m) {
  if (m.name !== endCallTool.function.name || typeof m.call_id !== 'string' || !m.call_id || m.call_id.length > 128 ||
      typeof m.arguments !== 'string' || m.arguments.length > 2048) throw Error('invalid_call_tool');
  const args = JSON.parse(m.arguments);
  if (!args || Array.isArray(args) || typeof args.reason !== 'string' || !args.reason.trim() || args.reason.length > 160 ||
      Object.keys(args).some(k => !['reason', 'timing'].includes(k)) ||
      (args.timing !== undefined && !['now', 'after_current_speech'].includes(args.timing))) throw Error('invalid_call_tool_arguments');
  return { call_id: m.call_id, reason: args.reason.trim(), timing: args.timing || 'now' };
}

export function providerFailure(code, message) {
  return (String(code).toLowerCase() === 'arrearage' || /account is in good standing/i.test(String(message)))
    ? 'voice_account_unavailable' : 'voice_provider_error';
}

export class CallSession {
  constructor(socket, emit, { instructions, voice, maxResponses = 4 }) {
    this.socket = socket; this.emit = emit; this.instructions = instructions;
    this.voice = voice; this.maxResponses = maxResponses;
    this.credits = 0; this.responses = 0; this.active = ''; this.cancelled = new Set();
    this.ready = false; this.closed = false;
    this.toolsSent = false; this.toolCalls = new Set(); this.pendingEnd = null;
  }
  open() {
    this.send({ type: 'session.create', session: { id: 'qq-call-' + Date.now(),
      instructions: this.instructions, extension: { direct_e2e: true,
        direct_e2e_vad_ms: 900, tts: { speaker: this.voice } } } });
  }
  send(value) { if (!this.closed) this.socket.send(JSON.stringify(value)); }
  command(m) {
    if (m.type === 'credit' && this.credits === 0) this.credits = 1;
    else if (m.type === 'audio' && this.ready && this.credits > 0 && !this.pendingEnd) {
      if (typeof m.data !== 'string' || m.data.length > 32000) throw Error('invalid_audio_frame');
      this.send({ type: 'input_audio_buffer.append', audio: m.data });
    } else if (m.type === 'stop') this.close();
    else if (m.type === 'tool_result' && this.pendingEnd?.call_id === m.call_id && typeof m.ok === 'boolean') {
      if (this.pendingReceipt) return;
      this.pendingReceipt = {call_id:m.call_id,ok:m.ok};
      this.send({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: m.call_id,
        output: JSON.stringify({ ok: m.ok, ended: m.ended === true, local_window_released: m.local_window_released === true,
          error: m.ok ? undefined : 'native_end_not_confirmed' }) } });
    }
    else if (m.type === 'no_credit') { this.exhausted = true; if (!this.active) this.close(); }
  }
  receive(m) {
    if (this.closed) return;
    if (m.type === 'error') {
      this.emit({ type: 'error', error: providerFailure(m.error?.code, m.error?.message) }); this.close(); return;
    }
    if (m.type === 'session.created') {
      // The existing facade maps session.updated to session.created. Register tools
      // separately so the shared product transport remains unchanged.
      if (!this.toolsSent) { this.toolsSent = true; this.send({ type: 'session.update', session: { tools: [endCallTool] } }); }
      else if (!this.ready) { this.ready = true; this.emit({ type: 'ready', native_end_tool: true }); }
      return;
    }
    if (m.type === 'conversation.item.created' && m.item?.type === 'function_call_output' &&
        m.item.call_id === this.pendingReceipt?.call_id) {
      const result = this.pendingReceipt; this.pendingReceipt = null; this.pendingEnd = null;
      this.emit({ type: 'tool_result_written', ...result, evidence: 'provider_item_created' });
      // There is nobody on the ended call to hear another response.
      if (result.ok) this.close();
      else if (this.credits > 0) this.send({ type: 'response.create' });
      return;
    }
    if (m.type === 'input_audio_buffer.speech_started') {
      if (this.active) { this.cancelled.add(this.active); this.send({ type: 'response.cancel' }); }
      this.emit({ type: 'barge_in', response_id: this.active }); this.active = '';
    }
    if (m.type === 'conversation.item.input_audio_transcription.completed')
      this.emit({ type: 'heard', item_id: m.item_id, text: m.transcript || '' });
    if (m.type === 'response.created') {
      if (this.credits < 1 || this.responses >= this.maxResponses) { this.send({ type: 'response.cancel' }); this.close(); return; }
      this.credits--; this.responses++; this.active = m.response?.id || '';
      if (!this.active) throw Error('missing_response_id');
      this.emit({ type: 'response', response_id: this.active, number: this.responses,
        need_credit: this.responses < this.maxResponses });
    }
    const id = m.response_id || m.response?.id || this.active;
    if (!id || this.cancelled.has(id)) return;
    if (m.type === 'response.function_call_arguments.done') {
      if (this.toolCalls.has(m.call_id) || this.pendingEnd) return;
      let choice;
      try { choice = parseEndCall(m); } catch { this.emit({ type: 'tool_rejected', error: 'invalid_call_tool' }); return; }
      this.toolCalls.add(choice.call_id); this.pendingEnd = choice;
      this.emit({ type: 'end_decision', name: endCallTool.function.name, response_id: id, ...choice });
    }
    if (m.type === 'response.output_audio.delta' && m.delta)
      this.emit({ type: 'audio', response_id: id, data: m.delta, sample_rate: 24000 });
    if (m.type === 'response.output_text.done')
      this.emit({ type: 'said', response_id: id, text: m.text || m.transcript || '' });
    if (m.type === 'response.done') {
      this.emit({ type: 'done', response_id: id, status: m.response?.status || '',
        output_types: (m.response?.output || []).map(x => ({type:x.type,name:x.name,call_id:x.call_id})) });
      if (this.active === id) this.active = '';
      if (this.exhausted || this.responses >= this.maxResponses) this.emit({ type: 'drain_then_stop' });
    }
  }
  close() { if (!this.closed) { this.closed = true; this.socket.close(); } }
}

async function main() {
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  let session, timer;
  const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
  for await (const line of lines) {
    if (line.length > 100000) throw Error('oversize_command');
    const m = JSON.parse(line);
    if (!session) {
      if (m.type !== 'start' || typeof m.instructions !== 'string' || m.instructions.length > 60000)
        throw Error('invalid_start');
      const { createDashscopeVoiceSocket } = await import(pathToFileURL(resolve(process.argv[2], 'deployment/voice-transport-dashscope.mjs')));
      const ws = createDashscopeVoiceSocket({ apiKey: process.env.DASHSCOPE_API_KEY,
        voice: process.env.ALI_VOICE || 'longanqian', log: () => {} });
      session = new CallSession(ws, emit, { ...m, voice: process.env.ALI_VOICE || 'longanqian' });
      ws.on('open', () => session.open());
      ws.on('message', raw => { try { session.receive(JSON.parse(String(raw))); }
        catch { emit({ type: 'error', error: 'voice_protocol_error' }); session.close(); } });
      ws.on('error', () => { emit({ type: 'error', error: 'voice_connection_error' }); session.close(); });
      ws.on('close', (code, reason) => {
        if (!session.closed) emit({ type: 'error', error: providerFailure(code, reason) });
        clearTimeout(timer); emit({ type: 'closed' }); lines.close(); process.stdin.destroy();
      });
      timer = setTimeout(() => session.close(), Math.min(1800, Math.max(10, Number(m.seconds) || 120)) * 1000);
    } else session.command(m);
  }
  clearTimeout(timer); session?.close();
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href)
  main().catch(() => { process.stderr.write('call_voice_helper_failed\n'); process.exitCode = 1; });
