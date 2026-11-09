"""Optional evaluation of a saved observation; no new outside read or data.

Use the original pre-read context so later knowledge cannot manufacture past
learning. Deliberately withdrawn interpretations are never offered for revival.
"""
import json

from .inquiry import receipt_matches,validate_intent

CONTRACT='''
pending_learning 是已保存但尚未取得有效问题评估的阅读，不是新材料。
当最近一次阅读因评估失败/超时导致 workspace.method_trials.status="feedback_missing"（或 workspace.next_steps.suggested_action="evaluate"）时，原文已保存在账本中，无需重复请求外部来源或跳过该段盲目顺读；可选择 {"action":"evaluate","target_episode_id":"其中一条id","goal":null,"resource_id":"","inquiry":null,"predict_success":false,"next_check_seconds":600,"reason":"..."} 用已保存原文与当时的问题及行动前状态补评一次，不把后来的知识倒填成当时收获。也可暂缓、休息或做其他合法行动。
已明确撤销的判断不会出现在此队列。previous_attempts/last_attempt_status 表示补评历史，失败不能当作学习；不要求自动反复重试。
'''


def eligible(row):
    d=json.loads(row['decision_json']);r=json.loads(row['result_json'] or '{}')
    a=r.get('inquiry_assessment') or {}
    if (row['revoked'] or row['status']!='observed' or d.get('action')!='read' or not d.get('inquiry')
            or not r.get('met_need') or r.get('status')!='ok' or not str(r.get('text','')).strip()
            or not receipt_matches(r) or r.get('inquiry_revoked') or a.get('status') in {'assessed','withdrawn'}):
        return False
    try:validate_intent(d['inquiry'])
    except (ValueError,TypeError):return False
    return True


def candidates(ledger,scope,actor):
    trials={}
    rows=ledger.db.execute("select * from episodes where owner=? and (scope='public' or (scope=? and actor=?)) order by started desc,rowid desc",
                           (ledger.owner,scope,actor)).fetchall()
    for row in rows:
        d=json.loads(row['decision_json'])
        if not row['revoked'] and d.get('action')=='evaluate':
            r=json.loads(row['result_json'] or '{}')
            trials.setdefault(d.get('target_episode_id'),[]).append(r.get('assessment_status',row['status']))
    out=[]
    for row in rows:
        if not eligible(row):continue
        d=json.loads(row['decision_json']);r=json.loads(row['result_json']);gid=(d.get('goal') or {}).get('id')
        goal=ledger.db.execute("select status,question,need,origin from goals where owner=? and id=?",(ledger.owner,gid)).fetchone()
        if not goal or goal['status'] not in {'active','paused'}:continue
        if any(goal[k]!=d['goal'].get(k) for k in ['question','need','origin']):continue
        attempts=trials.get(row['id'],[])
        out.append({'id':row['id'],'goal_id':gid,'question':d['goal']['question'],'source_ref':r['source_ref'],
                    'receipt':r['receipt'],'scope':row['scope'],'actor':row['actor'],
                    'why_pending':(r.get('inquiry_assessment') or {}).get('reason','not_evaluated'),
                    'previous_attempts':len(attempts),'last_attempt_status':attempts[0] if attempts else None})
        if len(out)==6:break
    return out
