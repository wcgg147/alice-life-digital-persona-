import unittest
from call_actions import end_action_ready,native_end_receipt
from native_call_service import valid_end_request


class CallActionsTests(unittest.IsolatedAsyncioTestCase):
    def test_current_speech_must_finish_both_generation_and_device_playback(self):
        choice={'timing':'after_current_speech','response_id':'r'}
        self.assertFalse(end_action_ready(choice,set(),{}))
        self.assertFalse(end_action_ready(choice,{'r'},{'pcm':200}))
        self.assertFalse(end_action_ready(choice,{'other'},{}))
        self.assertTrue(end_action_ready(choice,{'r'},{}))
        self.assertTrue(end_action_ready({**choice,'timing':'now'},set(),{'pcm':200}))

    def test_only_current_child_tool_event_can_request_native_action(self):
        event={'type':'native_tool_request','name':'qq_end_voice_call','native_call_key':'k',
            'call_id':'c','response_id':'r','reason':'今天聊够了','timing':'now'}
        self.assertTrue(valid_end_request(event,'k'))
        self.assertFalse(valid_end_request(event,'different-call'))
        self.assertFalse(valid_end_request({**event,'type':'heard'},'k'))
        self.assertFalse(valid_end_request({**event,'text':'先挂了'},'k'))
        self.assertFalse(valid_end_request({**event,'timing':'ask_user'},'k'))

    async def test_native_ack_without_releasing_the_correct_call_is_not_success(self):
        for window in ({'callKey':'k','phase':'ending','closeAcknowledged':True},
                       {'callKey':'k','phase':'ended','localWindowReleased':False},
                       {'callKey':'other','phase':'ended','localWindowReleased':True}):
            async def api(path,body=None):return {'posted':True} if body else {'windows':[window]}
            self.assertFalse((await native_end_receipt(api,'k',timeout=0))['ok'])

    async def test_real_receipt_waits_for_local_release_and_carries_persona_source(self):
        sent=[];observations=iter([
            {'callKey':'k','phase':'ending','closeAcknowledged':True},
            {'callKey':'k','phase':'ended','localWindowReleased':True}])
        async def api(path,body=None):
            if body:sent.append((path,body));return {'posted':True}
            return {'windows':[next(observations)]}
        result=await native_end_receipt(api,'k')
        self.assertEqual(result,{'ok':True,'ended':True,'local_window_released':True})
        self.assertEqual(sent,[('/hangup',{'callKey':'k','source':'persona_decision'})])
