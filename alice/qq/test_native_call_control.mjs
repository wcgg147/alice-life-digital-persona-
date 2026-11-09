import test from 'node:test';
import assert from 'node:assert/strict';
import { NativeOwnerCall } from './native_call_control.mjs';

const report = { audioDevices: [
  { deviceType: 0, deviceId: 3, deviceName: 'CABLE Output (VB-Audio Virtual Cable)' },
  { deviceType: 1, deviceId: 6, deviceName: 'Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)' },
], selectedMicAudioDevice: { deviceId: 3 }, selectedSpeakAudioDevice: { deviceId: 6 } };
const invite = (uid = 'owner', scene = 1) => [scene, uid, '', 1, '0', 0, 0, 0, 0, 0, false, ''];
function fixture() {
  const sent = [], states = [], timers = []; let clock = 1000;
  const c = new NativeOwnerCall({ ownerUid: 'owner', selfUid: 'self', send: x => sent.push(x), emit: x => states.push(x),
    now: () => clock, later: fn => { timers.push(fn); return fn; }, cancel: () => {} });
  return { c, sent, states, timers, advance: n => { clock += n; } };
}
test('foreign and group invitations never produce native Accept', () => {
  const { c, sent } = fixture(); c.setLease(10000); c.setDevices(report);
  c.receive({ cmd: 20006, param: invite('stranger') }); c.receive({ cmd: 20006, param: invite('owner', 3) });
  assert.equal(sent.length, 0);
});
test('owner private invitation waits for devices and an available audio backend', () => {
  const { c, sent } = fixture(); c.receive(JSON.stringify({ cmd: 20006, id: 0, value: invite() }));
  c.setDevices(report); assert.equal(sent.length, 0); c.setLease(10000);
  const accept = sent.find(x => x.cmd === 5);
  assert.deepEqual(accept.param, [1, 'self', ['owner'], 1, '0', false, '']);
  c.receive({ cmd: 20006, param: invite() }); assert.equal(sent.filter(x => x.cmd === 5).length, 1);
});
test('device acknowledgement is distinct from connected and selection evidence', () => {
  const { c } = fixture(); c.setLease(10000); c.setDevices(report); c.receive({ cmd: 20006, param: invite() });
  assert.equal(c.state.routeVerified, false); assert.equal(c.state.phase, 'accepting');
  c.receive({ cmd: 20008, id: 0, value: ['owner', 1, 1] }); assert.equal(c.state.phase, 'connected');
  c.setDevices(report); assert.equal(c.state.routeVerified, true);
});
test('direct private calls connect on the native room result without a MAV roster', () => {
  const { c } = fixture(); c.setLease(10000); c.setDevices(report); c.receive({ cmd: 20006, value: invite() });
  c.receive({ cmd: 20004, id: 0, value: [0] }); assert.equal(c.state.phase, 'connected');
  assert.equal(c.state.routeVerified, false); c.setDevices(report); assert.equal(c.state.routeVerified, true);
});
test('backend loss closes only the current verified owner call', () => {
  const { c, sent, advance } = fixture(); c.setDevices(report); c.setLease(2000); c.receive({ cmd: 20006, param: invite() });
  advance(2000); c.tick(); assert.deepEqual(sent.at(-1).param, [1, 'owner', 0]);
  assert.equal(c.state.phase, 'ending'); c.receive({ cmd: 20011, param: [1, '0', ''] }); assert.equal(c.state.phase, 'ended');
});
