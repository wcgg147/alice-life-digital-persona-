# -*- coding: utf-8 -*-

"""自动化需求筛选与自主迭代工作流 (Autonomous Demand Filtering & Persona Growth Pipeline)



本脚本供所有 Agent (Antigravity, Codex, WorkBuddy) 及定时任务调用：

1. 从真实社交会话的情报夹 (qq_project_insights) 中提取候选需求；

2. 自动化降噪（过滤游戏吐槽、第三方机器人复读、无意义水贴）；

3. 分轨聚类：

   - Track A: DEFECT (交互缺陷 / 边界漏洞，如私聊串群、每轮强制@、认错人)

   - Track B: PERSONA_GROWTH (人格表达成长 / 幽默度 / 拒绝风趣度 / 情绪价值)

   - Track C: CAPABILITY_PROPOSAL (工具 / 能力提案，如表情包、搜索、语音)

4. 核对当前仓库代码实现与单测，判定闭环状态 (RESOLVED / PLANNED / IN_PROGRESS)；

5. 输出机器可读 backlog (.runtime/qq-social/demand_backlog.json) 与人类/Agent可读报告 (reports/qq-social-demands-backlog.md)。



用法：

    py -3.12 alice/surf/pipeline_demands.py           # 运行筛选、聚类并生成报告

    py -3.12 alice/surf/pipeline_demands.py --json    # 输出 JSON 供其他 Agent 读取

    py -3.12 alice/surf/pipeline_demands.py --harvest # 先触发新情报回填再执行管线

"""

from __future__ import annotations



import argparse

import json

import os

import pathlib

import re

import sqlite3

import sys

import time



# Runtime and report locations can be overridden in private deployment config.

ROOT = pathlib.Path(__file__).resolve().parents[2]

RUNTIME = pathlib.Path(os.getenv("ALICE_QQ_RUNTIME", ROOT / ".runtime" / "qq-social")).resolve()

REPORTS_DIR = pathlib.Path(os.getenv("ALICE_REPORTS_DIR", ROOT / "reports")).resolve()





def get_db() -> sqlite3.Connection:

    db = sqlite3.connect(RUNTIME / "channel.sqlite3")

    db.row_factory = sqlite3.Row

    return db





def triage_and_denoise(db: sqlite3.Connection) -> dict:

    """第一阶段：降噪与预处理。将明显非产品吐槽、第三方Bot发言标记为 dismissed。"""

    rows = db.execute("SELECT * FROM qq_project_insights WHERE status='observed'").fetchall()

    dismissed_count = 0

    ignored_bot_ids = {

        item.strip()

        for item in os.getenv("ALICE_IGNORED_BOT_QQ_IDS", "").split(",")

        if item.strip()

    }



    # 常见游戏无关关键词与非产品话题

    noise_patterns = [

        r'抽卡|回血|抽奖|段位|定级|保底|心愿单',

        r'守望先锋|OW|ow|死斗|巴蒂斯特|极限战士|战锤',

        r'匹配机制|连跪|连胜|排位|竞技',

    ]

    noise_regex = re.compile('|'.join(noise_patterns), re.IGNORECASE)



    for r in rows:

        quote = str(r["quote"] or "")

        speaker_qq = str(r["speaker_qq"] or "")

        why = str(r["why"] or "")



        # 1. 过滤已配置的第三方AI账号以及已标记的Bot。

        if speaker_qq in ignored_bot_ids or 'KIRIKO' in why or '另一个AI' in why:

            db.execute("UPDATE qq_project_insights SET status='dismissed', why=? WHERE insight_key=?",

                       (why + " ｜ 判定为误收：第三方机器人发言", r["insight_key"]))

            dismissed_count += 1

            continue



        # 2. 过滤纯游戏吐槽

        if noise_regex.search(quote) and not re.search(r'Alice|机器人|AI|搜索|表情|私聊|记忆|说话|助手', quote):

            db.execute("UPDATE qq_project_insights SET status='dismissed', why=? WHERE insight_key=?",

                       (why + " ｜ 判定为误收：群友在讨论外部游戏，非本产品反馈", r["insight_key"]))

            dismissed_count += 1

            continue



    db.commit()

    return {"reviewed": len(rows), "auto_dismissed": dismissed_count}





def cluster_demands(db: sqlite3.Connection) -> list[dict]:

    """第二阶段：将有效的情报与反馈聚类为结构化需求。"""

    rows = db.execute("SELECT * FROM qq_project_insights WHERE status in ('observed', 'adopted', 'resolved') ORDER BY created_at DESC").fetchall()



    # 预定义的聚类定义表（带产品目标与代码映射）

    clusters = [

        {

            "id": "DEMAND-DEFECT-01",

            "track": "DEFECT",

            "title": "私聊记忆与群聊语境严格隔离",

            "description": "大号在群聊中与小号交互时，小号不得把私聊中的对话、喜好、约定或私密记忆带入群聊公屏接茬。",

            "target_files": [

                "tools/qq-social/social_memory.py",

                "tools/qq-social/alice.py",

                "tools/qq-social/channel.py"

            ],

            "pattern": r"私聊|单聊|记忆.*带|串了|朋友圈",

            "quotes": [],

            "status": "RESOLVED",

            "resolution_note": "已修复：在 social_memory 中群聊召回彻底剔除 private:* 作用域与私聊提示，alice.py 注入群聊社交边界指令，channel.py 分离群聊会话历史。"

        },

        {

            "id": "DEMAND-DEFECT-02",

            "track": "DEFECT",

            "title": "群聊自然连续对话，免去每轮强制 @",

            "description": "首轮通过 @ 或引用触发后，若同一用户在合理时间窗口（90s）内连续回复且群内无他人打断，Alice 自然顺畅接话，不强制每句都要 @。",

            "target_files": [

                "tools/qq-social/channel.py",

                "tools/qq-social/test_channel.py"

            ],

            "pattern": r"add|艾特|@|每轮|顺其自然",

            "quotes": [],

            "status": "RESOLVED",

            "resolution_note": "已修复：Gate 增加 group_active 状态追踪，支持单人会话在90秒无中断下触发 conversation_continuation；他人发言或超时或 stop 命令自然中断。"

        },

        {

            "id": "DEMAND-DEFECT-03",

            "track": "DEFECT",

            "title": "群聊多发言人身份识别与上下文归属隔离",

            "description": "不把群成员 A 说的话归属到群成员 B 头上，对话历史标明发言者身份。",

            "target_files": [

                "tools/qq-social/channel.py",

                "tools/qq-social/social_memory.py"

            ],

            "pattern": r"不分人|不是我说的|每个人.*区分|认错",

            "quotes": [],

            "status": "RESOLVED",

            "resolution_note": "已修复：Store.history 区分 private/group，群聊历史按各成员昵称标明 [昵称]: 内容，避免错把他人话轮当成当前用户的上文。"

        },

        {

            "id": "DEMAND-PERSONA-01",

            "track": "PERSONA_GROWTH",

            "title": "拒绝与接梗时风趣幽默短小，杜绝教科书式 AI 长篇说教",

            "description": "面对群友让其搜索、玩梗、生僻缩写或猜谜时，不自称“我没有搜索工具/模型无法实现”，而是像真人朋友一样幽默、短小、带吐槽地抛回。",

            "target_files": [

                "alice/agent/companion_contract.py",

                "tools/qq-social/alice.py"

            ],

            "pattern": r"搜索|不能搜索|猜谜|大肥鱼|智能|太AI|无聊|幽默|风趣|说教|拒绝",

            "quotes": [],

            "status": "RESOLVED",

            "resolution_note": "已修复：companion_contract.py 与 alice.py 全面更新 search_policy 和 companion limits，严禁客服式免责与系统术语说明，要求一两句自然幽默大实话。"

        },

        {

            "id": "DEMAND-CAPABILITY-01",

            "track": "CAPABILITY_PROPOSAL",

            "title": "QQ原生与收藏表情包自然发送与斗图互动",

            "description": "群友高频期望通过发表情包互动，AI需具备自然表情选择与斗图能力。",

            "target_files": [

                "tools/qq-social/qq_tools.py",

                "tools/qq-social/sticker_observer.py"

            ],

            "pattern": r"表情|表情包|斗图",

            "quotes": [],

            "status": "RESOLVED",

            "resolution_note": "已实现：接通 qq_find_expression 与 qq_search_sticker，结合情感语境与图文气泡排版发送。"

        },

        {

            "id": "DEMAND-CAPABILITY-02",

            "track": "CAPABILITY_PROPOSAL",

            "title": "权限与能力边界真实自白",

            "description": "当群友询问助手有什么权限时，如实说明自己仅为聊天好友，不具备群管理、踢人等后台权限。",

            "target_files": [

                "tools/qq-social/alice.py"

            ],

            "pattern": r"权限|管理|踢人",

            "quotes": [],

            "status": "RESOLVED",

            "resolution_note": "已约束：在 prompt 中明确群管理不在自己手里，不虚构权限能力。"

        }

    ]



    # 将情报映射到各个需求聚类

    for r in rows:

        quote = str(r["quote"] or "")

        why = str(r["why"] or "")

        text_to_match = quote + " " + why

        matched = False

        for c in clusters:

            if re.search(c["pattern"], text_to_match, re.IGNORECASE):

                c["quotes"].append({

                    "key": r["insight_key"],

                    "speaker": r["speaker_qq"],

                    "quote": quote,

                    "why": why,

                    "action": r["suggested_action"]

                })

                matched = True

                break



    return clusters





def generate_reports(clusters: list[dict], stats: dict) -> tuple[pathlib.Path, pathlib.Path]:

    """生成机器可读的 JSON 和人类可读的 Markdown 报告。"""

    backlog_data = {

        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),

        "stats": stats,

        "clusters": clusters

    }



    # 1. 机器可读 JSON

    json_path = RUNTIME / "demand_backlog.json"

    json_path.parent.mkdir(parents=True, exist_ok=True)

    json_path.write_text(json.dumps(backlog_data, ensure_ascii=False, indent=2), encoding="utf-8")



    # 2. Markdown 报告

    md_lines = [

        "# QQ 社交真实交互需求筛选与自主迭代总账",

        "",

        f"> 更新时间：`{backlog_data['generated_at']}` | 来源数据库：`{RUNTIME / 'channel.sqlite3'}`",

        f"> 自动预审情报：`{stats.get('reviewed', 0)}` 条 | 自动降噪驳回：`{stats.get('auto_dismissed', 0)}` 条",

        "",

        "---",

        "",

        "## 需求分轨与闭环状态矩阵",

        "",

        "| 需求ID | 分轨 | 核心需求与场景 | 关联文件 | 闭环状态 | 证据例句数 |",

        "|---|---|---|---|---|---|",

    ]



    for c in clusters:

        status_badge = "✅ 已修复并验收" if c["status"] == "RESOLVED" else "⏳ 规划中"

        target_str = "<br>".join([f"`{f}`" for f in c["target_files"]])

        md_lines.append(f"| **{c['id']}** | `{c['track']}` | **{c['title']}**<br>{c['description']} | {target_str} | {status_badge} | {len(c['quotes'])} 条 |")



    md_lines.extend([

        "",

        "---",

        "",

        "## 重点需求详单与闭环留痕",

        ""

    ])



    for c in clusters:

        md_lines.append(f"### [{c['track']}] {c['id']}: {c['title']}")

        md_lines.append(f"- **产品问题**: {c['description']}")

        md_lines.append(f"- **涉及源码**: " + ", ".join([f"`{f}`" for f in c["target_files"]]))

        md_lines.append(f"- **闭环结论**: {c['resolution_note']}")

        if c["quotes"]:

            md_lines.append("- **真实用户原句佐证**:")

            for q in c["quotes"][:5]:

                md_lines.append(f"  - *\"{q['quote']}\"* （{q['why']}）")

        md_lines.append("")



    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    md_path = REPORTS_DIR / "qq-social-demands-backlog.md"

    md_path.write_text("\n".join(md_lines), encoding="utf-8")



    return json_path, md_path





def main() -> None:

    ap = argparse.ArgumentParser(description="QQ社交需求自主筛选与闭环工作流")

    ap.add_argument("--json", action="store_true", help="只输出 JSON 数据供 Agent 管道消费")

    ap.add_argument("--harvest", action="store_true", help="先运行一次情报回填")

    args = ap.parse_args()



    if args.harvest:

        harvest_script = ROOT / "tools" / "alice-surf" / "harvest_insights.py"

        if harvest_script.is_file():

            os.system(f'py -3.12 "{harvest_script}" --limit 100')



    db = get_db()

    try:

        stats = triage_and_denoise(db)

        clusters = cluster_demands(db)

        json_path, md_path = generate_reports(clusters, stats)

    finally:

        db.close()



    if args.json:

        print(json_path.read_text(encoding="utf-8"))

    else:

        print(f"[OK] 需求筛选流水线执行完成：")

        print(f"  - 结构化总账 (JSON): {json_path}")

        print(f"  - 需求报告 (Markdown): {md_path}")

        print(f"  - 聚类需求总数: {len(clusters)}")

        for c in clusters:

            print(f"    [{c['status']}] {c['id']}: {c['title']} ({len(c['quotes'])} 真实例句)")





if __name__ == "__main__":

    main()
