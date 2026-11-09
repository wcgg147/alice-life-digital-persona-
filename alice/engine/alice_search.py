# -*- coding: utf-8 -*-
"""她自己想搜什么。

拆成独立小模块的原因：往 alice_reading.py 里塞带换行的字符串常量时，
手工转义把文件写坏过两次（一次变成真换行、一次丢了 chr(10)）。独立文件用
write_file 落地，不会再经过 shell 的转义。
"""
from __future__ import annotations

from typing import Any

SEARCH_QUERY_SYSTEM = "\n".join([
    "你就是 Alice，现在想自己在 B 站搜点东西看。",
    "给一个你真会输入的搜索词：2-8 个字/词，口语，可以带具体作品名、角色、游戏、术语；",
    "不要写完整句子，不要写「如何看待」「关于…」这种书面腔，也别为了显得深刻去搜抽象词。",
    "只输出搜索词本身，不要引号、不要解释。",
])


async def make_search_query(persona_brief: str = "", taste: str = "") -> str:
    """她想搜什么。失败返回空串，那一轮退回首页热榜。"""
    from alice_reading import _llm  # SPLIT-PLAN: alice/life canonical  # 延迟导入：避免和 alice_reading 循环依赖

    sys_msg: Any = SEARCH_QUERY_SYSTEM
    if persona_brief:
        sys_msg += "\n你的底子：" + persona_brief
    if taste:
        sys_msg += "\n你刷 B 站一贯的口味：" + taste
    try:
        raw = await _llm([{"role": "system", "content": sys_msg},
                          {"role": "user", "content": "搜什么？"}],
                         max_tokens=40, temperature=0.9)
    except Exception:
        return ""
    return " ".join(str(raw or "").strip().strip('"“”').split())[:30]
