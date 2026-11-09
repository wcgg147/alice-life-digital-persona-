"""Source-bound value appraisal of idle questions, separate from execution.

No question must be adopted. Explicit defer/dismiss choices survive in the
question pool; a selected question is executed through the normal goal gate.
"""
import json

from .idle_attention import is_idle
from .noticing import checked_question
from .self_model import retrieve_related_knowledge

SYSTEM='''你是Alice当前的自主目标选择过程，不负责回答问题。
从自己实际选择的观察、当时想了解什么、现在仍不懂的地方和已有偏好中，判断哪些疑问现在还想追究。
可以全部暂缓或放弃；未知不自动等于值得研究。已有目标完成，并不要求新疑问与它有关。只评价当下兴趣与取证机会，不编造既往经历或把评论当事实。
把“是否还想了解”和“当前文本是否已经给出答案”分开。寻找外部资料本来就是搜索的用途；不能因为当前文本没答案，就直接判定现有公开文本搜索无用。
若提供 related_prior_knowledge，那是你此前在真实原文中已核实的阶段性认识（仅在原证据范围内有效）：可用于判断候选疑问是否已有部分解答、识别仍未解决的具体缺口，并在选择 pursue 时生成更聚焦的 search query，避免重复查证已知事实。
逐项给stance=pursue/defer/dismiss和具体理由（不超过80字）；pursue表示现在想查证，defer表示暂放，dismiss表示放弃。可全部defer/dismiss。
若现在有想追究的问题，最多选择一项并自行给出公开搜索词与可用provider；否则selected_id=null、query/provider为空，选择休息。selected_id=null时不把任何一项标为pursue。
取证目标不能把评论中的价格或判断当已知事实，原文数字只能作为待核实线索。搜索只是在找候选来源，不表示找到答案。
只输出JSON {"evaluations":[{"id":"候选疑问id","stance":"pursue/defer/dismiss","reason":"当下理由"}],"selected_id":"一个疑问id或null","reason":"本次选择理由，不超过180字","provider":"可用入口或空","query":"围绕所选疑问的搜索词或空"}。'''


def candidates(context, config):
    if config.get('cognition_idle_goal_choice_enabled') is not True or not is_idle(context):return []
    if not context.get('exploration',{}).get('can_search'):return []
    recent=context.get('episodes',[])
    if recent and recent[0].get('decision',{}).get('action')=='inspect':return []
    sources={s['id']:s for s in context.get('inspection_sources',[])}
    result=[]
    for q in context.get('open_questions',[]):
        s=sources.get(q['source_episode_id'])
        if q['status']!='open' or not s or s['receipt']!=q['source_receipt']:continue
        checked=checked_question(q,s)
        if any(checked[k]!=q.get(k) for k in checked):continue
        result.append(q)
    return result[:6]


def messages(context,questions):
    source_ids={q['source_episode_id'] for q in questions}
    prior=retrieve_related_knowledge(context,[f"{q.get('question','')} {q.get('quote','')} {q.get('reason','')}" for q in questions])
    if not prior:prior=context.get('related_prior_knowledge',[])
    data={'questions':questions,'actual_observations':[s for s in context['inspection_sources'] if s['id'] in source_ids],
          'persona_facts':context['persona_facts'],'capabilities':context['exploration']}
    if prior:data['related_prior_knowledge']=prior
    return [{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(data,ensure_ascii=False)}]


def decision(raw,context,questions):
    data=json.loads(raw) if isinstance(raw,str) else raw
    if not isinstance(data,dict) or not isinstance(data.get('evaluations'),list):raise ValueError('invalid_goal_appraisal')
    offered={q['id']:q for q in questions};evaluations={}
    for item in data['evaluations']:
        if (not isinstance(item,dict) or item.get('id') not in offered or item['id'] in evaluations
                or item.get('stance') not in {'pursue','defer','dismiss'}
                or not isinstance(item.get('reason'),str) or not 1<=len(item['reason'].strip())<=240):
            raise ValueError('invalid_question_evaluation')
        evaluations[item['id']]=item
    if set(evaluations)!=set(offered):raise ValueError('goal_appraisal_requires_visible_questions')
    selected=data.get('selected_id');reason=data.get('reason')
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=240:raise ValueError('invalid_goal_choice_reason')
    if selected is not None and (not isinstance(selected,str) or selected not in offered or evaluations[selected]['stance']!='pursue'):
        raise ValueError('invalid_selected_question')
    if selected is None and any(e['stance']=='pursue' for e in evaluations.values()):raise ValueError('rest_cannot_claim_adopted_question')
    sources={s['id']:s for s in context['inspection_sources']};dispositions=[]
    for qid,item in evaluations.items():
        q=offered[qid]
        dispositions.append(checked_question({**q,'status':{'pursue':'open','defer':'deferred','dismiss':'dismissed'}[item['stance']],
                                               'reason':item['reason'].strip()},sources[q['source_episode_id']]))
    d={'action':'search' if selected else 'rest','resource_id':'','goal':None,'inquiry':None,
       'reason':reason.strip(),'predict_success':selected is not None,'next_check_seconds':600,
       'question_appraisal':dispositions}
    if selected:
        q=offered[selected]
        d.update(goal={'id':'','question':q['question'],'need':'text','origin':q['source_episode_id'],'status':'active'},
                 search_query=data.get('query'),search_provider=data.get('provider'))
    elif data.get('query') or data.get('provider'):raise ValueError('rest_cannot_search')
    return d


def recheck(questions,current):
    visible={q['id']:q for q in current.get('open_questions',[])}
    if any(visible.get(q['id'])!=q for q in questions):raise ValueError('goal_appraisal_sources_or_dispositions_changed')
