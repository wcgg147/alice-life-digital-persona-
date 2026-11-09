"""Only advertise a native call capability while the matching local service is live."""
import json,time
from pathlib import Path


def provider_blocked(config,runtime):
    try:
        state=json.loads((Path(runtime)/'voice-provider-unavailable.json').read_text(encoding='utf-8'))
        return str(state['account'])==str(config['expected_qq']) and state['error']=='voice_account_unavailable'
    except (OSError,ValueError,KeyError,TypeError):return False


def call_capability(config,store,work,now=None):
    if work.get('group')!='private:'+str(config.get('account_owner_qq')):return ''
    try:
        runtime=Path(store.db.execute('PRAGMA database_list').fetchone()[2]).parent
        if provider_blocked(config,runtime):return ''
        state=json.loads((runtime/'native-call-health.json').read_text(encoding='utf-8'))
        age=(time.time() if now is None else now)-float(state['at'])
        if (not 0<=age<8 or not state.get('available') or str(state['account'])!=str(config['expected_qq'])
                or str(state['owner'])!=str(config['account_owner_qq'])):return ''
    except (OSError,ValueError,KeyError,TypeError):return ''
    return ('当前这个小号的主人私聊语音来电通道在线，可以接听对方大号的QQ语音通话；'
        '通话时可以听说、打断和自主结束，挂断前不必告知。不能从文字聊天里拨出电话，也不接其他人或群通话。')
