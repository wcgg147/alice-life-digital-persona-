# -*- coding: utf-8 -*-
"""她的一天：到点自己出门逛或读书，不用主人敲命令。

调度只做三件代码该做的事——频率、预算、别撞车；去哪儿、读不读、歇不歇由
`roam.py` / `read_book.py` 里的她自己决定。

    py -3.12 alice/surf/day.py                 # 到点自动选：上午读书，其余时间出门
    py -3.12 alice/surf/day.py --dry-run       # 只打印"打算做什么、会被哪条规矩拦下"
    py -3.12 alice/surf/day.py --action read --book huozhe --force

状态与日志：`.runtime/alice-life/day-state.json`、`day-YYYYMMDD.jsonl`
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, date

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / ".runtime" / "alice-life"
LOCK = STATE_DIR / "day.lock"
STATE = STATE_DIR / "day-state.json"
PY = sys.executable or "py"

MAX_ACTIONS = int(os.getenv("ALICE_DAY_MAX_ACTIONS", "6"))
# 静默时段；可用 ALICE_DAY_QUIET="22-23" 临时改，复核时用来验证这条规矩真的生效。
_quiet = os.getenv("ALICE_DAY_QUIET", "0-8")
try:
    QUIET_START, QUIET_END = (int(x) for x in _quiet.split("-", 1))
except Exception:
    QUIET_START, QUIET_END = 0, 8
MIN_GAP_MINUTES = int(os.getenv("ALICE_DAY_MIN_GAP_MIN", "90"))
# 必须短于 cron 间隔（3 小时）：被硬杀留下的锁不该吃掉一整个周期。
LOCK_TTL = int(os.getenv("ALICE_DAY_LOCK_TTL_SEC", "2700"))


def log_path(day: date) -> pathlib.Path:
    return STATE_DIR / f"day-{day.strftime('%Y%m%d')}.jsonl"


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with log_path(date.today()).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def lock_busy(readonly: bool = False) -> bool:
    """readonly=True 时只判断，不清过期锁——dry-run 不能有副作用。"""
    if not LOCK.exists():
        return False
    try:
        age = time.time() - LOCK.stat().st_mtime
    except OSError:
        return False
    if age > LOCK_TTL:
        if not readonly:
            LOCK.unlink(missing_ok=True)
        return False
    return True


def acquire_lock() -> bool:
    """原子建锁：两个进程同时起，只有一个能拿到。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return True


def release_lock() -> None:
    LOCK.unlink(missing_ok=True)


def _records_today() -> list[dict]:
    path = log_path(date.today())
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def actions_today() -> int:
    """只数真正做了事的；歇着不占预算（否则连歇六次就把一天用光）。"""
    return sum(1 for r in _records_today() if r.get("counted", True))


def chose_reading_today() -> bool:
    return any(r.get("action") == "read" and r.get("counted", True) for r in _records_today())


def failed_twice(action: str) -> bool:
    """同一动作今天连坏两次就别再试它——书坏了不该每 90 分钟撞一次。"""
    rows = [r for r in _records_today() if r.get("action") == action]
    return len(rows) >= 2 and all(r.get("rc") != 0 for r in rows[-2:])


def decide(hour: int) -> str:
    """上午读书，其余时间出门；今天已经读过、或读书连坏两次，就出门。"""
    if hour < 15 and not chose_reading_today() and not failed_twice("read"):
        return "read"
    return "roam"


def blocked_reasons(now: datetime, state: dict) -> list[str]:
    reasons = []
    if QUIET_START <= now.hour < QUIET_END:
        reasons.append("静默时段（%d–%d 点）" % (QUIET_START, QUIET_END))
    if lock_busy(readonly=True):
        reasons.append("上一件事还没跑完（锁在）")
    if actions_today() >= MAX_ACTIONS:
        reasons.append("今天已经做了 %d 件，够了" % MAX_ACTIONS)
    last = float(state.get("last_run_at") or 0)
    if last and (time.time() - last) < MIN_GAP_MINUTES * 60:
        reasons.append("距上次不到 %d 分钟" % MIN_GAP_MINUTES)
    return reasons


def _last_json_object(text: str) -> dict:
    """从子进程输出里找最后一个 JSON 对象。

    roam 打一行，read_book 打多行（indent=1）——只取最后一行会拿到 "}"。
    """
    value = (text or "").strip()
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    lines = value.splitlines()
    for start in range(len(lines) - 1, -1, -1):
        chunk = chr(10).join(lines[start:]).strip()
        if not chunk.endswith("}"):
            continue
        try:
            parsed = json.loads(chunk)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}


def run_action(action: str, book: str, timeout: int = 1200) -> dict:
    if action == "read":
        cmd = [PY, str(ROOT / "alice" / "surf" / "read_book.py"), "--book", book, "--rounds", "1"]
    else:
        cmd = [PY, str(ROOT / "alice" / "surf" / "roam.py"), "--rounds", "1", "--gap-min", "0"]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT / "alice" / "surf"), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        # 超时也要留下子进程已经吐出来的东西，不然不知道卡在哪。
        raw = exc.stdout
        out = raw if isinstance(raw, str) else (raw or b"").decode("utf-8", "replace")
        raw_err = exc.stderr
        err = "timeout after %ss; partial stderr: " % timeout + (
            raw_err if isinstance(raw_err, str) else (raw_err or b"").decode("utf-8", "replace"))
        rc = -9
    parsed = _last_json_object(out)
    choice = str(parsed.get("choice") or "")
    error = str(parsed.get("error") or "")
    # roam 会把模型连接错误写成 {"error": ...} 但退出码仍是 0；这种不能算「做了事」。
    return {"action": action, "rc": rc, "seconds": round(time.time() - started),
            "choice": choice, "error": error,
            "counted": rc == 0 and not error and choice != "rest",
            "stdout_tail": (out or "")[-600:], "stderr_tail": (err or "")[-300:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", choices=["auto", "read", "roam"], default="auto")
    ap.add_argument("--book", default="huozhe")
    ap.add_argument("--force", action="store_true", help="忽略间隔/预算/静默；锁仍然生效")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=1200, help="子进程超时秒数；取证时可调小")
    args = ap.parse_args()

    now = datetime.now()
    state = read_state()
    action = args.action if args.action != "auto" else decide(now.hour)
    reasons = blocked_reasons(now, state)

    if args.dry_run:
        # dry-run 永远给答案：做什么、被哪条规矩拦下。守卫不能把它挡在前面。
        print("打算做：%s（现在 %s，今天已做 %d 件）" % (action, now.strftime("%H:%M"), actions_today()))
        print("会拦下它的规矩：%s" % ("、".join(reasons) if reasons else "无"))
        return 0

    # 锁不给 --force 绕：正在跑的任务不能被人工插队覆盖。
    if lock_busy():
        print("上一件事还没跑完，跳过")
        return 0
    if reasons and not args.force:
        print("；".join(reasons) + "，跳过")
        return 0

    if not acquire_lock():
        print("没抢到锁，跳过")
        return 0
    try:
        record = run_action(action, args.book, args.timeout)
    finally:
        release_lock()

    record["at"] = now.isoformat(timespec="seconds")
    append_log(record)
    state["last_run_at"] = time.time()
    state.setdefault("counts", {})
    state["counts"][str(date.today())] = actions_today()
    write_state(state)

    print("%s rc=%s %ss choice=%s%s" % (action, record["rc"], record["seconds"],
                                        record["choice"] or "-",
                                        (" error=" + record["error"]) if record.get("error") else ""))
    if record["stdout_tail"].strip():
        print(record["stdout_tail"].strip()[-300:])
    if record["rc"] != 0 and record["stderr_tail"].strip():
        print("stderr: " + record["stderr_tail"].strip()[-200:])
    return 0 if (record["rc"] == 0 and not record.get("error")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
