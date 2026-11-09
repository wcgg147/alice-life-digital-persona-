// SPDX-License-Identifier: GPL-3.0-only
// AVSDK command mappings: oldhan13/napcat-qq-voice-call-win,
// bridge/qq/napcat-plugin/controller.mjs, commit 4f1bfeba20bcd7b8d335af03b54f38e743f1aa6d.
// Runs against the real QQ plugin; never simulates mouse or keyboard input.
export class NativeOwnerCall {
  constructor({ ownerUid, selfUid, send, emit, now = () => Date.now(), later = (fn, ms) => globalThis.setTimeout(fn, ms), cancel = id => globalThis.clearTimeout(id), testHangupMs = 0 }) {
    Object.assign(this, { ownerUid, selfUid, send, emit, now, later, cancel, testHangupMs });
    this.state = { phase: 'idle', routeVerified: false }; this.leaseUntil = 0; this.sequence = 170000000;
    this.timers = []; this.devices = null; this.pending = null; this.fingerprint = '';
  }
  publish(extra = {}) { this.emit({ ...this.state, ...extra }); }
  command(cmd, param) { const id = ++this.sequence; this.send({ cmd, id, param }); return id; }
  setLease(until) { this.leaseUntil = Math.min(this.now() + 12000, Number(until) || 0); this.tryAccept(); }
  setDevices(report) {
    if (!report || !Array.isArray(report.audioDevices)) return;
    const pick = (type, name) => report.audioDevices.filter(d => d.deviceType === type && d.deviceName === name && Number.isInteger(d.deviceId) && d.deviceId > 0);
    const mic = pick(0, 'CABLE Output (VB-Audio Virtual Cable)'), speaker = pick(1, 'Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)');
    if (mic.length !== 1 || speaker.length !== 1) { this.devices = null; this.state.routeVerified = false; this.publish({ reason: 'explicit_devices_missing' }); return; }
    this.devices = { input: mic[0].deviceId, output: speaker[0].deviceId };
    this.state.routeVerified = report.selectedMicAudioDevice?.deviceId === this.devices.input && report.selectedSpeakAudioDevice?.deviceId === this.devices.output;
    this.publish({ devices: this.devices }); this.tryAccept();
  }
  receive(raw) {
    let m; try { m = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return; }
    if (!m || !Number.isInteger(m.cmd)) return;
    const p = m.value ?? m.param ?? m.params;
    if (m.cmd === 20006) {
      if (!Array.isArray(p) || p.length < 12 || p[0] !== 1 || p[1] !== this.ownerUid || !Number.isInteger(p[3]) || typeof p[4] !== 'string' || !/^\d{1,20}$/.test(p[4]) || typeof p[11] !== 'string') {
        this.publish({ observation: 'unaccepted_invite', reason: 'not_verified_owner_private_invite' }); return;
      }
      const fp = JSON.stringify([p[0], p[1], p[3], p[4]]);
      if (fp === this.fingerprint && !['idle', 'ended'].includes(this.state.phase)) return;
      this.pending = { params: p, at: this.now(), fingerprint: fp };
      this.state = { phase: 'waiting', sceneId: 1, callKey: 'qq-call-' + this.now() + '-' + (++this.sequence), routeVerified: false, ownerVerified: true };
      this.publish(); this.tryAccept();
    } else if (m.cmd === 10 && m.id === this.closeRequestId && this.state.phase === 'ending' && Array.isArray(p) && Number.isInteger(p[0])) {
      this.state.closeAcknowledged = p[0] === 0; this.state.nativeCloseResult = p[0]; this.publish();
    } else if (m.cmd === 20004 && Array.isArray(p) && Number.isInteger(p[0]) && this.state.ownerVerified && this.state.phase === 'accepting') {
      if (p[0] === 0) { this.state.phase = 'connected'; this.state.routeVerified = false; this.publish(); this.applyDevices(); }
      else this.hangup('native_connect_failed');
    } else if (m.cmd === 20008 && Array.isArray(p) && p[0] === this.ownerUid && ['accepting', 'connected'].includes(this.state.phase)) {
      const first = this.state.phase !== 'connected';
      this.state.phase = 'connected'; if (first) this.state.routeVerified = false; this.publish(); if (first) this.applyDevices();
    } else if (m.cmd === 20010 && Array.isArray(p) && typeof p[0] === 'string' && this.state.ownerVerified) {
      try { const room = JSON.parse(p[0]);
        if (room.scene_id === 1 && room.user_list?.some(u => u.uid === this.ownerUid) && room.user_list?.some(u => u.uid === this.selfUid)) {
          const first = this.state.phase !== 'connected';
          this.state.phase = 'connected'; if (first) this.state.routeVerified = false; this.publish(); if (first) this.applyDevices();
        }
      } catch {}
    } else if (m.cmd === 20009 && Array.isArray(p) && p[1] === this.ownerUid && this.state.ownerVerified) {
      this.hangup('peer_left');
    } else if ((m.cmd === 20011 || m.cmd === 20018 || m.cmd === 20022 || (m.cmd === 20019 && this.state.phase !== 'connected')) && this.state.ownerVerified) {
      this.finish('native_call_ended');
    }
  }
  tryAccept() {
    if (!this.pending || this.state.phase !== 'waiting' || !this.devices || this.leaseUntil <= this.now()) return;
    if (this.now() - this.pending.at > 45000) { this.finish('invite_expired'); return; }
    const p = this.pending.params; this.fingerprint = this.pending.fingerprint;
    this.state.phase = 'accepting'; this.publish();
    this.applyDevices();
    this.command(5, [p[0], this.selfUid, [p[1]], p[3], p[4], Boolean(p[10]), p[11]]);
    for (const ms of [100, 600, 1800]) this.timers.push(this.later(() => this.applyDevices(), ms));
    if (this.testHangupMs > 0) this.timers.push(this.later(() => this.hangup('owner_control_test'), this.testHangupMs));
  }
  applyDevices() {
    if (!this.devices || !['accepting', 'connected'].includes(this.state.phase)) return;
    this.command(102, [this.devices.input, this.devices.output]); this.command(103, []);
  }
  tick() {
    if (this.state.ownerVerified && ['accepting', 'connected'].includes(this.state.phase) && this.leaseUntil <= this.now()) this.hangup('audio_backend_unavailable');
  }
  hangup(reason) {
    if (!this.state.ownerVerified || !['accepting', 'connected'].includes(this.state.phase)) return false;
    this.state.phase = 'ending'; this.state.reason = reason; this.state.closeRequestedAt = this.now(); this.publish();
    this.closeRequestId = this.command(10, [1, this.ownerUid, 0]); return true;
  }
  finish(reason) {
    for (const timer of this.timers) this.cancel(timer); this.timers = []; this.pending = null;
    this.state.phase = 'ended'; this.state.reason = reason; this.state.routeVerified = false; this.publish();
  }
}

export function attachNativeOwnerCall(config) {
  if (globalThis.__aliceNativeOwnerCall?.version === 5) return { installed: true, reused: true };
  globalThis.__aliceNativeOwnerCall?.destroy();
  const bridge = { version: 5, controller: null, plugin: null, pendingDevices: null, leaseUntil: 0, state: { phase: 'idle', routeVerified: false } };
  bridge.setLease = until => { bridge.leaseUntil = until; bridge.controller?.setLease(until); };
  bridge.setDevices = report => { bridge.pendingDevices = report; bridge.controller?.setDevices(report); };
  bridge.hangup = (source = 'operator_requested') =>
    ['operator_requested','persona_decision','audio_backend_ended'].includes(source) && bridge.controller?.hangup(source) || false;
  const install = p => {
    if (bridge.controller || p.type !== 'application/x-ppapi-avSDK') return;
    const emit = state => { bridge.state = state; };
    const controller = new NativeOwnerCall({ ...config, send: value => p.postMessage(value), emit });
    bridge.controller = controller; bridge.plugin = p;
    const receive = event => {
      try { const data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
        const value = data?.value ?? data?.param;
        bridge.lastNative = { cmd: data?.cmd, keys: data && typeof data === 'object' ? Object.keys(data).slice(0,8) : [],
          paramTypes: Array.isArray(value) ? value.slice(0,12).map(v=>Array.isArray(v)?'array':typeof v) : [] };
        bridge.nativeHistory = [...(bridge.nativeHistory || []),bridge.lastNative].slice(-20);
        controller.receive(data);
      } catch (error) {
        bridge.lastError = { name: error.name, message: String(error.message).slice(0,120) };
        try { controller.hangup('native_controller_error'); } catch {}
      }
    };
    p.addEventListener('message', receive); bridge.removePluginListener = () => p.removeEventListener('message', receive);
    if (bridge.pendingDevices) controller.setDevices(bridge.pendingDevices);
    controller.setLease(bridge.leaseUntil);
  };
  const scan = () => {
    if (bridge.plugin && !bridge.plugin.isConnected) {
      bridge.controller?.finish('native_plugin_released'); bridge.state.localAudioReleased = true;
      bridge.removePluginListener?.(); bridge.controller = null; bridge.plugin = null; bridge.pendingDevices = null;
    }
    document.querySelectorAll('embed,object').forEach(install);
  };
  const observer = new MutationObserver(scan); observer.observe(document.documentElement, { subtree: true, childList: true }); scan();
  const ticker = setInterval(() => { scan(); bridge.controller?.tick(); }, 250);
  bridge.destroy = () => { bridge.controller?.hangup('operator_stopped'); clearInterval(ticker); observer.disconnect(); bridge.removePluginListener?.(); delete globalThis.__aliceNativeOwnerCall; };
  globalThis.addEventListener('unload', () => { clearInterval(ticker); observer.disconnect(); }, { once: true });
  globalThis.__aliceNativeOwnerCall = bridge;
  return { installed: true };
}
