// Main-process adapter for QQ's existing native AVSDK plugin. Loopback and token protected.
export function canReleaseNativeCallWindow(meta) {
  try {
    const url = new URL(meta.url);
    return meta.ownerVerified === true && meta.sceneId === 1 && ['ending','ended'].includes(meta.phase) &&
      /^qq-call-\d+-\d+$/.test(meta.callKey || '') && meta.currentCallKey === meta.callKey &&
      meta.type === 'window' && url.protocol === 'app:' && url.pathname === '/renderer/index.html' && url.hash.split('?')[0] === '#/av';
  } catch { return false; }
}

export function bootstrapNativeQQCalls(config, rendererSource) {
  const req = process.mainModule.require.bind(process.mainModule);
  const e = req('electron'), http = req('http'), crypto = req('crypto');
  if (globalThis.__aliceNativeQQCalls) return { installed: true, reused: true, port: config.port };
  let leaseUntil = 0, closed = false;
  const windows = new Map(), ipcHooks = new Map();
  const safeConfig = { ownerUid: config.ownerUid, selfUid: config.selfUid, testHangupMs: config.testHangupMs || 0 };
  const source = '(()=>{' + rendererSource + '\nreturn attachNativeOwnerCall(' + JSON.stringify(safeConfig) + ');})()';
  const execute = async (w, code) => {
    if (!w || w.isDestroyed()) throw Error('call_window_closed');
    return await Promise.race([w.executeJavaScript(code), new Promise((_, reject) => setTimeout(() => reject(Error('native_window_timeout')), 1500))]);
  };
  async function install(w) {
    if (closed || w.isDestroyed() || !w.getURL().startsWith('app://./renderer/index.html')) return;
    try {
      await execute(w, source);
      const old = windows.get(w.id); windows.set(w.id, { w, state: old?.state || { phase: 'idle' }, updatedAt: Date.now() });
      await execute(w, 'globalThis.__aliceNativeOwnerCall?.setLease(' + leaseUntil + ')');
    } catch {}
  }
  function watch(w) {
    w.on('did-finish-load', () => install(w)); install(w);
    w.once('destroyed', () => {
      const row = windows.get(w.id);
      if (row) { row.state = { ...row.state, phase: 'ended', routeVerified: false, reason: 'native_window_destroyed' }; row.localWindowReleased = true; row.updatedAt = Date.now(); row.w = null; }
    });
  }
  function hook(channel) {
    if (typeof channel !== 'string' || !channel.startsWith('RM_IPCFROM_RENDERER') || ipcHooks.has(channel)) return;
    const receive = (event, header, body) => {
      if (header?.eventName !== 'AVApi' || body?.cmdName !== 'refreshAudioDevices') return;
      const report = body.payload?.[0];
      if (!report || !Array.isArray(report.audioDevices) || report.audioDevices.length > 64) return;
      const w = event.sender;
      if (!w || w.isDestroyed() || !w.getURL().startsWith('app://')) return;
      execute(w, 'globalThis.__aliceNativeOwnerCall?.setDevices(' + JSON.stringify(report) + ')').catch(() => {});
    };
    ipcHooks.set(channel, receive); e.ipcMain.prependListener(channel, receive);
  }
  const added = channel => { if (typeof channel === 'string' && channel.startsWith('RM_IPCFROM_RENDERER')) setImmediate(() => hook(channel)); };
  const created = (_event, w) => watch(w);
  e.ipcMain.eventNames().forEach(hook); e.ipcMain.on('newListener', added);
  e.webContents.getAllWebContents().forEach(watch); e.app.on('web-contents-created', created);
  async function refresh() {
    await Promise.all(Array.from(windows.values()).filter(row => row.w && !row.w.isDestroyed()).map(async row => {
      try {
        const result = await execute(row.w, 'JSON.stringify({version:globalThis.__aliceNativeOwnerCall?.version,state:globalThis.__aliceNativeOwnerCall?.state,lastNative:globalThis.__aliceNativeOwnerCall?.lastNative,lastError:globalThis.__aliceNativeOwnerCall?.lastError,nativeHistory:globalThis.__aliceNativeOwnerCall?.nativeHistory,nativeAvailable:!!globalThis.__aliceNativeOwnerCall?.plugin?.isConnected})');
        const data = JSON.parse(result); row.state = data.state || { phase: 'idle' }; row.lastNative = data.lastNative;
        row.nativeAvailable = data.nativeAvailable; row.updatedAt = Date.now();
        row.lastError = data.lastError; row.nativeHistory = data.nativeHistory; row.nativeVersion = data.version;
        if (row.state.ownerVerified && ['ending','ended'].includes(row.state.phase) && !row.windowCloseRequested &&
            (row.state.phase === 'ended' || row.state.closeAcknowledged || Date.now() - (row.state.closeRequestedAt || Date.now()) > 1200)) {
          void releaseCallWindow(row);
        }
      } catch { row.unresponsive = true; }
    }));
    return Array.from(windows.entries()).map(([id, row]) => ({ windowId: id, ...row.state, nativeAvailable: row.nativeAvailable,
      lastNative: row.lastNative, lastError: row.lastError, nativeHistory: row.nativeHistory, nativeVersion: row.nativeVersion, localWindowReleased: !!row.localWindowReleased, updatedAt: row.updatedAt, destroyed: !row.w, unresponsive: !!row.unresponsive }));
  }
  async function releaseCallWindow(row) {
    const w = row.w, key = row.state.callKey;
    if (!w || w.isDestroyed() || row.windowCloseRequested) return;
    try {
      const current = JSON.parse(await execute(w, 'JSON.stringify(globalThis.__aliceNativeOwnerCall?.state)'));
      const meta = { ...row.state, currentCallKey: current?.callKey, type: w.getType(), url: w.getURL() };
      if (!canReleaseNativeCallWindow(meta) || !['ending','ended'].includes(current?.phase)) return;
      const window = e.BrowserWindow.fromWebContents(w);
      if (!window || window.webContents.id !== w.id) return;
      row.windowCloseRequested = true; window.close();
      setTimeout(async () => {
        if (w.isDestroyed() || window.isDestroyed()) return;
        try {
          const state = JSON.parse(await execute(w, 'JSON.stringify(globalThis.__aliceNativeOwnerCall?.state)'));
          if (canReleaseNativeCallWindow({ ...state, callKey: key, currentCallKey: state?.callKey, type: w.getType(), url: w.getURL() })) window.destroy();
        } catch {}
      }, 500);
    } catch {}
  }
  async function setLease(canAccept) {
    leaseUntil = canAccept ? Date.now() + 8000 : 0;
    await Promise.all(Array.from(windows.values()).filter(r => r.w && !r.w.isDestroyed()).map(r => execute(r.w,
      'globalThis.__aliceNativeOwnerCall?.setLease(' + leaseUntil + ')').catch(() => {})));
  }
  const authorized = incoming => {
    const got = Buffer.from(String(incoming || '')), expected = Buffer.from('Bearer ' + config.token);
    return got.length === expected.length && crypto.timingSafeEqual(got, expected);
  };
  const server = http.createServer(async (request, response) => {
    const reply = (status, data) => { response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); response.end(JSON.stringify(data)); };
    if (!authorized(request.headers.authorization)) { reply(403, { error: 'unauthorized' }); return; }
    try {
      if (request.method === 'GET' && request.url === '/status') { reply(200, { windows: await refresh(), leaseUntil }); return; }
      let text = ''; for await (const part of request) { text += part; if (text.length > 2048) throw Error('oversize_body'); }
      const body = text ? JSON.parse(text) : {};
      if (request.method === 'POST' && request.url === '/lease' && typeof body.canAccept === 'boolean' && Object.keys(body).length === 1) {
        await setLease(body.canAccept); reply(200, { ok: true, leaseUntil }); return;
      }
      if (request.method === 'POST' && request.url === '/hangup' && typeof body.callKey === 'string' &&
          Object.keys(body).every(k => ['callKey','source'].includes(k)) &&
          (body.source === undefined || ['operator_requested','persona_decision','audio_backend_ended'].includes(body.source))) {
        await refresh();
        const row = Array.from(windows.values()).find(r => r.w && r.state.ownerVerified && r.state.callKey === body.callKey && ['accepting', 'connected'].includes(r.state.phase));
        if (!row) { reply(409, { error: 'no_matching_owner_call' }); return; }
        const posted = await execute(row.w, 'globalThis.__aliceNativeOwnerCall.hangup(' + JSON.stringify(body.source || 'operator_requested') + ')');
        reply(200, { posted: !!posted }); return;
      }
      reply(400, { error: 'unsupported_operation' });
    } catch { if (!response.headersSent) reply(500, { error: 'native_call_operation_failed' }); }
  });
  server.on('error', () => { closed = true; leaseUntil = 0; });
  server.listen(config.port, '127.0.0.1');
  const stop = () => {
    closed = true; leaseUntil = 0;
    for (const [channel, listener] of ipcHooks) e.ipcMain.removeListener(channel, listener);
    e.ipcMain.removeListener('newListener', added); e.app.removeListener('web-contents-created', created);
    for (const row of windows.values()) if (row.w && !row.w.isDestroyed()) execute(row.w, 'globalThis.__aliceNativeOwnerCall?.destroy()').catch(() => {});
    server.close(); delete globalThis.__aliceNativeQQCalls;
  };
  globalThis.__aliceNativeQQCalls = { stop };
  return { installed: true, port: config.port, ownerOnly: true, automaticAnswerRequiresBackendLease: true };
}
