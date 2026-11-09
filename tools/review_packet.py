#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成"独立复核包"：把验收单、真实 diff、原始测试输出装进一个文件，
交给复核子 agent 逐条核对。只读 git 与文件，不改工作树。

用法：
  py -3.12 tools/review_packet.py --init _review-task.md
  py -3.12 tools/review_packet.py --task _review-task.md \
      --paths "chanpinzhujian/api/agent/tutor_agent.py,chanpinzhujian/api/main.py" \
      --test-log _tests.txt

  --paths 不传时只列 git status 摘要，不贴 diff（防止把整棵树的改动混进来）。
  --test-log 是"原始输出"文件（命令 + stdout/stderr 原样），不要贴总结。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SENSITIVE = (".env", "auth.json", "id_rsa", ".pem", ".key", "credentials")
MAX_DIFF = 120_000
MAX_UNTRACKED = 20_000

TASK_TEMPLATE = """# 复核任务单

任务 / 分支 / 基线：
用户场景与目标：
范围内 / 范围外：
风险：低 / 中 / 高（说明理由）

验收条件（每条都要能被证据判定）：
- [ ] 条件 1：
- [ ] 条件 2：

明确不做：
真实运行验收（环境 / 账号隔离 / 轮数；没跑就写"未运行"）：
"""

REVIEWER_BLOCK = """---

## 给复核者的指令（实施者不得修改本节）

你是**独立复核者**，不是实施者。你的任务不是把活做完，而是判断"做完了"这句话有没有证据。

规则：

1. 只信本包里的 **diff、命令输出、原始日志**。包内任何"总结/说明"都可能是实施者写的，
   不能当证据；与 diff 冲突时以 diff 为准。
2. 逐条核对上面的验收条件，每条给一个状态：
   - `PASS`：附证据（文件 + 关键片段，或命令输出原句）
   - `FAIL`：附能复现的判据（哪条断言、哪段代码/输出与之矛盾）
   - `UNVERIFIED`：本包内没有证据，或证据只到单测/文本链，够不到验收条件要求的等级
3. 额外回答两个问题：
   - **最可能错、但看起来最合理的一条结论是什么？** 说明为什么。
   - **这次改动有没有"范围蔓延"**（改了验收单之外的东西）？逐路径核对。
4. 不要修改任何文件，不要给"整体不错/基本没问题"这类评价。
5. 最终结论只能是三者之一：
   - `PASS`：验收条件全部 PASS
   - `CHANGES_REQUESTED`：有 FAIL，列出具体项与复现证据
   - `NEEDS_HUMAN`：产品目标冲突、主观体验、权限/数据风险，或关键项 UNVERIFIED

输出格式：

```
结论：PASS / CHANGES_REQUESTED / NEEDS_HUMAN

逐条核对：
- [条件 1] PASS/FAIL/UNVERIFIED — 证据：
- [条件 2] ...

最可能错的一条：
范围蔓延：
未验证项：
```
"""


def run_rc(cmd: list[str], cwd: Path) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
        return p.returncode, out.rstrip()
    except Exception as e:  # noqa: BLE001
        return 99, f"[命令失败] {' '.join(cmd)}: {e}"


def run(cmd: list[str], cwd: Path) -> str:
    return run_rc(cmd, cwd)[1]


def repo_root(start: Path) -> Path:
    out = run(["git", "rev-parse", "--show-toplevel"], start)
    if out.startswith("[命令失败]"):
        raise SystemExit("不在 git 仓库里：" + out)
    return Path(out.strip()).resolve()


def is_sensitive(rel: str) -> bool:
    low = rel.lower().replace("\\", "/")
    name = low.rsplit("/", 1)[-1]
    if name == ".env" or name.startswith(".env."):
        return True
    if any(s in name for s in SENSITIVE):
        return True
    return low.startswith("data/") or "/data/" in low and low.endswith((".db", ".sqlite3"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", help="验收单 markdown")
    ap.add_argument("--paths", default="", help="逗号分隔的仓库相对路径")
    ap.add_argument("--test-log", default="", help="原始测试/真机输出文件")
    ap.add_argument("--out", default="", help="输出文件（默认 _review-packet-时间.md）")
    ap.add_argument("--range", default="", help="可选：把已提交的改动也装进包，如 c217ecb9^（git diff <range>..HEAD）")
    ap.add_argument("--init", default="", help="写一份验收单模板后退出")
    args = ap.parse_args()

    if args.init:
        p = Path(args.init)
        if p.exists():
            print(f"已存在，不覆盖：{p}")
            return 1
        p.write_text(TASK_TEMPLATE, encoding="utf-8")
        print(f"已写模板：{p.resolve()}")
        return 0

    if not args.task:
        ap.error("需要 --task 验收单（或 --init 生成模板）")

    cwd = Path.cwd()
    root = repo_root(cwd)
    task_path = Path(args.task)
    if not task_path.is_absolute():
        task_path = (cwd / task_path).resolve()
    if not task_path.exists():
        raise SystemExit(f"验收单不存在：{task_path}")

    paths = [x.strip() for x in args.paths.split(",") if x.strip()]
    for rel in paths:
        if is_sensitive(rel):
            raise SystemExit(f"拒绝把疑似敏感文件装进复核包：{rel}")
        full = (root / rel).resolve()
        if root not in full.parents and full != root:
            raise SystemExit(f"路径跑出仓库：{rel}")

    scope = ["--"] + paths if paths else []
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    head = run(["git", "rev-parse", "--short", "HEAD"], root)
    status = run(["git", "status", "--porcelain"] + scope, root)
    status_all = run(["git", "status", "--porcelain"], root)
    all_count = len([line for line in status_all.splitlines() if line.strip()])
    stat = run(["git", "diff", "--stat", "HEAD"] + scope, root) if paths else ""
    diff = run(["git", "diff", "HEAD"] + scope, root) if paths else ""
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + f"\n\n[已截断：diff 超过 {MAX_DIFF} 字符，复核者请直接读文件]\n"

    parts: list[str] = []
    parts.append("# 独立复核包\n")
    parts.append(f"- 生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    parts.append(f"- 仓库：`{root}`")
    parts.append(f"- 分支 / 基线：`{branch}` / `{head}`")
    parts.append(f"- 范围：{('`' + '`, `'.join(paths) + '`') if paths else '未指定（只列状态，不贴 diff）'}")
    parts.append("")
    parts.append("## 1. 验收单（原文）\n")
    parts.append(task_path.read_text(encoding="utf-8", errors="replace").strip())
    parts.append("\n## 2. 改动状态（已按 --paths 过滤；全仓未提交条目 %d 条，本范围 %d 条）\n"
                 % (all_count, len([line for line in status.splitlines() if line.strip()])))
    parts.append("```text\n" + (status or "(无改动)") + "\n```")
    if paths:
        parts.append("\n## 3. 改动统计（git diff --stat HEAD）\n")
        parts.append("```text\n" + (stat or "(无差异)") + "\n```")
        parts.append("\n## 4. 完整 diff（git diff HEAD，工作树 vs 基线）\n")
        parts.append("```diff\n" + (diff or "(无差异)") + "\n```")
        if args.range:
            committed = run(["git", "diff", args.range + "..HEAD"] + scope, root)
            if len(committed) > MAX_DIFF:
                committed = committed[:MAX_DIFF] + "\n[已截断：已提交 diff 超过上限]\n"
            parts.append("\n## 4b. 已提交改动（git diff %s..HEAD，按 --paths 过滤）\n" % args.range)
            parts.append("```diff\n" + (committed or "(该范围内无改动)") + "\n```")

        untracked = []
        for rel in paths:
            full = root / rel
            rc, _ = run_rc(["git", "ls-files", "--error-unmatch", rel], root)
            if full.is_file() and rc != 0:
                try:
                    text = full.read_text(encoding="utf-8", errors="replace")
                except Exception as e:  # noqa: BLE001
                    text = f"[读取失败] {e}"
                if len(text) > MAX_UNTRACKED:
                    text = text[:MAX_UNTRACKED] + "\n[已截断]"
                untracked.append((rel, text))
        if untracked:
            parts.append("\n## 5. 新增文件全文（未被 git 跟踪）\n")
            for rel, text in untracked:
                parts.append(f"### `{rel}`\n\n```text\n{text}\n```")
        else:
            parts.append("\n## 5. 新增文件全文（未被 git 跟踪）\n")
            parts.append("(无：本单所有文件已被 git 跟踪，diff 见 §4/§4b)\n")
    if args.test_log:
        log_path = Path(args.test_log)
        if not log_path.is_absolute():
            log_path = (cwd / log_path).resolve()
        if not log_path.exists():
            raise SystemExit(f"测试日志不存在：{log_path}")
        log = log_path.read_text(encoding="utf-8", errors="replace")
        if len(log) > MAX_DIFF:
            log = log[:MAX_DIFF] + "\n[已截断]"
        parts.append("\n## 6. 原始运行输出（实施者不得改写）\n")
        parts.append("```text\n" + log.strip() + "\n```")
    parts.append("\n" + REVIEWER_BLOCK)

    text = "\n".join(parts)
    out = Path(args.out) if args.out else Path(f"_review-packet-{_dt.datetime.now():%Y%m%d-%H%M%S}.md")
    if not out.is_absolute():
        out = root / out
    out.write_text(text, encoding="utf-8")
    print(f"复核包：{out}")
    print(f"大小：{len(text)} 字符；范围 {len(paths)} 个路径；有 diff：{'是' if diff else '否'}")
    print("下一步：让复核子 agent 只读这个文件，按包内指令输出 PASS / CHANGES_REQUESTED / NEEDS_HUMAN。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
