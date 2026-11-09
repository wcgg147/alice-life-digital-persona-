import re

_PATTERNS = [
    r"你(?:讲话|说话|回复|回答|聊天|语气|口吻|风格|态度)",
    r"你.{0,6}(?:肉麻|油腻|尴尬|奇怪|烦|吵|啰嗦|敷衍|冷漠|阴阳|自恋|自我感觉)",
    r"(?:表情包|语音|图片|消息|回复|引用|代码).{0,8}(?:没|不|又|错|乱|坏|问题|声音)",
    r"(?:发成|变成|弄成|搞成|当成).{0,6}(?:代码|表情|图片|语音|文字)",
    r"能不能别|可不可以别|别再|不要每|别每",
    r"(?:你|Alice).{0,10}(?:帮我|帮忙|写|做|实现|生成|改|修|弄)",
    r"(?:帮我|帮忙).{0,10}(?:写|做|实现|生成|改|修|弄)",
    r"(?:你|Alice).{0,8}(?:会不会|能不能|可不可以|能否)",
]

_COMPILED = [re.compile(p) for p in _PATTERNS]

_GREETING = re.compile(
    r"^(?:你好|您好|hi|hello|嗨|哈喽|在吗|早上好|晚上好|早安|晚安|谢谢|多谢|好的|ok|嗯|哦|哈哈+|233+|草|笑死)[\s!！。.~～?？]*$",
    re.IGNORECASE,
)

_CODE = re.compile(
    r"(?:^|\n)\s*(?:def |class |import |from |#include|function |const |let |var |public |private |print\(|console\.log|\{|\}|;\s*$)",
    re.MULTILINE,
)


def is_feedback_candidate(text) -> bool:
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    s = s[:2000]
    if _GREETING.match(s):
        return False
    if _CODE.search(s) and not re.search(r"你|Alice", s):
        return False
    return any(p.search(s) for p in _COMPILED)
