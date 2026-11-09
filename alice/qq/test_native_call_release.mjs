import test from 'node:test';
import assert from 'node:assert/strict';
import { canReleaseNativeCallWindow } from './native_call_bootstrap.mjs';
const call = { ownerVerified:true,sceneId:1,phase:'ending',callKey:'qq-call-123-456',currentCallKey:'qq-call-123-456',type:'window',url:'app://./renderer/index.html#/av' };
test('only the same ending owner call window can be released', () => {
 assert.equal(canReleaseNativeCallWindow(call),true);
 for(const patch of [{ownerVerified:false},{sceneId:3},{phase:'connected'},{currentCallKey:'qq-call-999-999'},{type:'webview'},{url:'app://./renderer/index.html#/main/message'},{url:'app://./renderer/index.html#/chat'}])
  assert.equal(canReleaseNativeCallWindow({...call,...patch}),false);
});
