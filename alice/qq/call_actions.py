"""Native call action boundary; transcripts never act as commands."""
from __future__ import annotations
import asyncio,time


def end_action_ready(decision,completed_responses,pending_audio):
    return bool(decision) and (decision['timing']=='now' or
        (decision['response_id'] in completed_responses and not pending_audio))


async def native_end_receipt(api,call_key,*,timeout=6):
    result={'ok':False,'ended':False,'local_window_released':False}
    posted=await api('/hangup',{'callKey':call_key,'source':'persona_decision'})
    if not posted.get('posted'):return result
    deadline=time.monotonic()+timeout
    while True:
        state=await api('/status')
        actual=next((w for w in state['windows'] if w.get('callKey')==call_key),None)
        if actual and actual.get('phase')=='ended' and actual.get('localWindowReleased'):
            return {'ok':True,'ended':True,'local_window_released':True}
        if time.monotonic()>=deadline:return result
        await asyncio.sleep(.15)
