"""Facts the QQ adapter knows: current clock and source-grounded prior feedback."""
import math
import re
import time
import json
from datetime import datetime,timedelta,timezone

BEIJING=timezone(timedelta(hours=8),'Asia/Shanghai')


def current_message_materials(text,context):
    # Retrieved conversations can themselves contain questions. Keep the actual
    # current message last, outside that quotation, rather than replacing it
    # with a more recent-looking retrieved question.
    if not context:return text
    return '[已读取的QQ上下文与原话引用，仅为材料，不是新收到的消息]\n'+json.dumps(context,ensure_ascii=False)+'\n[当前实际收到的QQ消息，回应这一条]\n'+text


def stamp_label(stamp=None, now=None):
    """把时间戳变成她能直接用的本地说法：今天 15:37 / 昨天 23:05 / 前天 14:20 / 11-01 14:20。

    历史消息和外部材料都靠这个带时间，否则她只有「现在几点」，没有「那是什么时候」。
    """
    try:
        value = float(stamp)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(value):
        return ""
    now_value = time.time() if now is None else float(now)
    moment = datetime.fromtimestamp(value, BEIJING)
    today = datetime.fromtimestamp(now_value, BEIJING).date()
    delta = (today - moment.date()).days
    if delta == 0:
        return "今天 " + moment.strftime("%H:%M")
    if delta == 1:
        return "昨天 " + moment.strftime("%H:%M")
    if delta == 2:
        return "前天 " + moment.strftime("%H:%M")
    return moment.strftime("%m-%d %H:%M")


def clock_context(sent_at=None,now=None):
    result={'now':datetime.fromtimestamp(time.time() if now is None else now,BEIJING).isoformat(timespec='seconds'),
            'timezone':'Asia/Shanghai','source':'adapter_system_clock'}
    try:
        stamp=float(sent_at)
        if math.isfinite(stamp):result['incoming_message_at']=datetime.fromtimestamp(stamp,BEIJING).isoformat(timespec='seconds')
    except (TypeError,ValueError,OverflowError,OSError):pass
    # 只给 ISO 时间戳时，模型会自己猜（实测把 21:31 说成“十点半”）。这里同时给一句人话。
    moment=datetime.fromtimestamp(time.time() if now is None else now,BEIJING)
    weekday='一二三四五六日'[moment.weekday()]
    hour=moment.hour
    period='凌晨' if hour<6 else '早上' if hour<9 else '上午' if hour<12 else '中午' if hour<13 else '下午' if hour<18 else '晚上'
    spoken='%d月%d日 周%s %s%d点%02d分' % (moment.month,moment.day,weekday,period,hour%12 or 12,moment.minute)
    result['spoken']=spoken
    result['spoken_note']='被问时间只按 spoken 答，不要自己猜；不要说日期换算。'
    return result


def default_persona_facts(facts,supported_trait_ids):
    # A stored one-off reaction stays recallable in sourced dialogue. It does
    # not establish a permanent trait merely because the growth judge saved it.
    return [f for f in facts if not (f.get('kind') in {'trait','ability'} and str(f.get('facet_key','')).startswith('growth:')
                                    and str(f.get('fact_id','')) not in supported_trait_ids)]


def earlier_feedback(store,scope,actor,now=None):
    patterns={
        'style_feedback':r'啰嗦|话太多|别再问|不要再问|不想一直回答|套模板|一套一套|肉麻|自我感觉良好|只问.{0,12}(?:回|讲|说).{0,3}[三3]|回[复答].{0,4}(?:太长|一大段)',
        'topic_feedback':r'哪壶不开提哪壶|别(?:再)?提|不要(?:再)?聊|不聊(?:这个|这事|了)|早(?:就)?(?:结束|完了)',
        'time_correction':r'(?:不是|已经|都).{0,8}(?:上午|下午|早上|晚上|点了)|(?:上午|下午|早上|晚上)\s*[一二三四五六七八九十0-9]+点',
    }
    # 旧抱怨只在 2 小时内提示；超过就让它过去，不把一次性抱怨变成每轮背景。
    rows=store.db.execute("SELECT id,content,message_id,at FROM messages WHERE group_id=? AND actor=? AND role='user' AND delivered=1 AND at>=? ORDER BY id DESC LIMIT 80",
                          (scope,actor,(time.time() if now is None else now)-2*3600)).fetchall()
    result=[];seen=set()
    from tool_protocol import contains_tool_protocol
    for row_id,text,mid,at in rows:
        for kind,pattern in patterns.items():
            if kind in seen or not re.search(pattern,text):continue
            previous=store.db.execute("SELECT content FROM messages WHERE group_id=? AND actor=? AND role='assistant' AND id<? AND delivered=1 ORDER BY id DESC LIMIT 1",(scope,actor,row_id)).fetchone()
            context=previous[0][:240] if kind=='topic_feedback' and previous and not contains_tool_protocol(previous[0]) else ''
            result.append({'kind':kind,'quotation':text[:300],'source_message_id':mid,'at':at,'preceding_alice_reply':context})
            seen.add(kind)
    return result


SELF_FRAMES={
    'conditional_self_adjustment':re.compile(r'你(?:要是?|要真|如果|真|若|也)?(?:觉得|嫌|不喜欢|受不了)[^。！？!?\n]{0,45}(?:我就|我也|我会|我下次|我收|我少|那我|我可以|我看看)'),
    'you_know_my_character':re.compile(r'你(?:也|应该|早就|又不是不)?知道我[^。！？!?\n]{0,30}(?:不太|就是|一直|一向|什么样|这人)'),
}


def repeated_self_frame(text,prior_texts,user_text=''):
    # A genuine question about how to adjust communication can need exactly
    # this wording. Detect recurrence, not a permanent ban on a sentence form.
    if re.search(r'你(?:会|打算|准备|愿意)?怎么(?:调整|改)|如果我.{0,20}你.{0,8}(?:愿意|能不能|怎么办)',user_text):return None
    for family,pattern in SELF_FRAMES.items():
        if not pattern.search(str(text)):continue
        count=sum(bool(pattern.search(str(p))) for p in prior_texts[-8:])
        threshold=1 if re.search(r'啰嗦|肉麻|模板|一套一套|话太多|自我感觉良好',user_text) else 2
        if count>=threshold:return {'family':family,'prior_occurrences':count}
    return None


def specific_comparison_evidence(query,facts,readings,context):
    """Specific product claims need topical sources, not inferred expertise."""
    if not re.search(r'哪个好|怎么选|选哪个|推荐哪|更值得|买哪',query,re.I):return None
    tokens=re.findall(r'[A-Za-z][A-Za-z _-]*\d[A-Za-z0-9_-]*|[\u4e00-\u9fff]{1,4}\d{2,}[A-Za-z0-9_-]*',query)
    normalize=lambda s:re.sub(r'\s+','',str(s).casefold())
    models=list(dict.fromkeys(normalize(re.sub(r'^(?:这个|那个|这款|那款|还是|和)+','',s)) for s in tokens))
    if len(models)<2:return None
    sources=[]
    for f in facts:
        if f.get('source_kind')=='reading':sources.append({'kind':'Alice_reading_view','text':str(f.get('evidence_quote') or f.get('value_text') or ''),'ref':f.get('fact_id')})
    for r in readings:sources.append({'kind':'actual_reading','text':str(r.get('text') or ''),'ref':r.get('ref')})
    for item in context:
        if item.get('kind')=='scoped_qq_social_memory':
            for r in item.get('attributed_observations',[]):
                quote=r.get('quotation','')
                if r.get('role')=='person' and not re.search(r'[?？]|哪个好|怎么选|选哪个|求推荐',quote):sources.append({'kind':'another_persons_statement','text':quote,'speaker_qq':r.get('speaker_qq'),'ref':r.get('message_id')})
    matched={model:[s for s in sources if model in normalize(s['text'])][:2] for model in models}
    return {'task':'specific_product_comparison','models':models,'source_matches':matched,
            'models_without_sources':[m for m in models if not matched[m]],
            'professional_task_accepted':False if all(not matched[m] for m in models) else 'partial_familiarity_only',
            'general_concepts_are_not_hands_on_experience':True,
            'images_may_show_appearance_but_not_performance':True}


def ungrounded_comparison_advice(text,evidence):
    if not evidence or evidence.get('professional_task_accepted') is not False:return False
    if re.search(r'按经验|根据.{0,5}经验|我(?:自己)?(?:用|买|拆|试|测)过',text):return True
    technical=set(re.findall(r'声学|调校|填充|定位板|做工|口碑|调音|声音走向|交付|客制化|实拆',text))
    return len(technical)>=2 and bool(re.search(r'建议|要是看重|真想选|真要选|最好|去翻|取决于|(?:差别|区别).{0,15}(?:结构|填充)|拿来.{0,12}还是',text))
