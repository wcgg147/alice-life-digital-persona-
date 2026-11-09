"""QQ presentation choices; no sentence chopping or extra model request."""
import re


def repeats_spoken_text(paragraph,voice_text):
    """Remove a duplicated transcript; distinct captions remain Alice's choice."""
    from difflib import SequenceMatcher
    clean=lambda s:''.join(re.findall(r'\w',str(s).casefold()))
    text,spoken=clean(paragraph),clean(voice_text)
    if not text or not spoken:return False
    if text in spoken:return True
    if len(text)<8:return False
    pairs={text[i:i+2] for i in range(len(text)-1)}
    voice_pairs={spoken[i:i+2] for i in range(len(spoken)-1)}
    coverage=sum(m.size for m in SequenceMatcher(None,text,spoken,autojunk=False).get_matching_blocks())/len(text)
    return len(pairs & voice_pairs)/len(pairs)>=.5 and coverage>=.7


def expression_invitation(text):
    text = str(text or '').strip()
    # 「图/图片/图包」也算要图：主人说「分享分享你的新图」时不能只换来一段文字描述。
    if not re.search(r'表情|动图|gif|笑脸|图(?:片|包)?',text,re.I):
        return False
    if re.search(r'地图|图书馆|蓝图|企图|意图|试图|拼图',text):
        return False
    # Questions about meanings/creation are not invitations to send an image.
    if re.search(r'什么意思|什么含义|为什么|怎么(?:做|制作|画|搜)|解释|描述|介绍',text):
        return False
    if re.search(r'(?:别|不要|不用|不必)(?:再)?(?:给我)?(?:发|来|给|用).*?(?:表情|动图|gif)',text,re.I):
        return False
    return bool(re.search(r'(?:发|来|给|分享|晒|甩|挑|选|用|看|换).{0,6}?(?:表情|动图|gif|笑脸|图(?:片|包)?)',text,re.I)
                or re.search(r'(?:表情|动图|gif).*?(?:发|来|给|分享)',text,re.I)
                or re.search(r'(?:表情|动图|gif).*?(?:表达|代表|对应|适合|搭配).*?(?:心情|状态|感受|情绪|反应)',text,re.I)
                or re.search(r'(?:心情|状态|感受|情绪).*?(?:哪|什么|啥|适合|用).*?(?:表情|动图|gif)',text,re.I))


def claims_attachment(text):
    """文本里是否声称已经把图/语音发过去了 —— 用来对照这一轮到底有没有附件。"""
    text = str(text or '')
    if re.search(r'没发|不发|发不出|还没发|没法发|没收到|发不出去', text):
        return False
    return bool(re.search(r'(?:发|给|送)(?:你|您)(?:了|一张|个)?|发过去了|发出去了|这就发|已经发了', text))


def reply_parts(reply):
    from tool_protocol import contains_tool_protocol
    text = str(reply.get('text') or '') if isinstance(reply,dict) else str(reply)
    text = text.strip()[:1800]
    if contains_tool_protocol(text):
        raise RuntimeError('internal_tool_protocol_not_chat_text')
    # Blank lines are the model's own turn composition. Never split by length
    # or punctuation: one thought stays intact, and short replies stay short.
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n',text) if p.strip()]
    if isinstance(reply,dict) and reply.get('voice_text'):
        paragraphs=[p for p in paragraphs if not repeats_spoken_text(p,reply['voice_text'])]
    if len(paragraphs)>3:
        paragraphs = paragraphs[:2] + ['\n\n'.join(paragraphs[2:])]
    # 真人一条消息说完就完了：短回复（合计 ≤110 字）不拆成两三条完整句。
    # 拆条会把「解释+补充+建议」的结构暴露出来，那是最明显的 AI 味。
    if len(paragraphs) >= 2 and sum(len(p) for p in paragraphs) <= 110 and not (isinstance(reply, dict) and reply.get('segments')):
        paragraphs = ['，'.join(p.rstrip('。！？!?.,，') for p in paragraphs) + '。']
    parts = [{'segments':[{'type':'text','data':{'text':p}}],'spoken':p} for p in paragraphs]
    if isinstance(reply,dict):
        for index,segment in enumerate(reply.get('segments',[])[:2]):
            displays=reply.get('attachment_displays') or []
            spoken=str(displays[index] if index<len(displays) else reply.get('display') or '[QQ附件]')
            if parts and segment.get('type')!='record' and not any(s.get('type')=='record' for s in parts[-1]['segments']) and reply.get('text_placement','together')=='together':
                parts[-1]['segments'].append(segment)
                parts[-1]['spoken']+='\n'+spoken
            else:
                parts.append({'segments':[segment],'spoken':spoken})
    return parts


def reply_complaint(text):
    """对方是不是在抱怨她上一轮的回复（用于自动标负样本，不需要额外打标动作）。

    返回 (verdict, reason)：
      ('bad', 理由)  明确针对回复的抱怨——太长、AI 味、又在解释、又是这句
      ('weak', 理由) 只有“不喜欢/不行/受不了”这类，配合很短的消息才算弱负样本
      (None, '')     没在抱怨她的回复
    """
    value = str(text or '').strip()
    if not value or len(value) > 60:
        return None, ""
    strong = {
        "too_long": r"太长|啰嗦|话太多|写这么多|一大段|看不下去|看不完",
        "ai_voice": r"AI\s*味|ai\s*味|像\s*AI|机器味|客服|像机器人|不像人|没人味|人味|说人话|假惺惺|不自然",
        "explaining": r"别解释|不用解释|别总结|别复述|别分析|说重点|别绕",
        "again": r"又这样|又是这句|又来了|你怎么老|又开始|复读",
    }
    for reason, pattern in strong.items():
        if re.search(pattern, value, re.I):
            return "bad", reason
    if re.search(r"不喜欢|不行|受不了|无语|什么玩意|没意思|不好", value):
        return "weak", "dislike"
    return None, ""


def declines_expression(text):
    return bool(re.search(r'(?:不想|不愿|不打算|这次不|先不)(?:再)?(?:发|用).*?(?:表情|动图|gif)|(?:这次|现在)(?:就|只)(?:想)?(?:用文字|说话)',str(text),re.I))

_PERFORMATIVE_REPAIR = re.compile(
    r'我认|我改|我记下了|下次直接|不认了|是我.{0,8}(?:太|错|不对|跳远|接得)'
)


def performative_repair(text):
    """上一轮是不是在表演认错/保证改；只用于历史标注，不用于判用户。"""
    return bool(_PERFORMATIVE_REPAIR.search(str(text or '')))


_TIME_PREFIX = re.compile(
    r'^\s*\[(?:今天|昨天|前天|刚刚|刚才|\d{1,2}:\d{2}(?::\d{2})?|\d{4}-\d{2}-\d{2})[^\]]{0,16}\]\s*'
)


def strip_time_prefix(text):
    """出站兜底：模型偶尔会把「[今天 15:46]」抄进正文，这里剥掉开头那一个。

    只认时间形状的方括号，不碰「[QQ表情：…]」这类占位或她自己的引号。
    """
    value = str(text or '')
    return _TIME_PREFIX.sub('', value, count=1)
