"""Host lifecycle only: keep/restart one WSL process; never choose Alice's goals."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "alice"))
from cognition.state import InstanceLock


def save(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def run(config_path):
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    stop = Path(config["stop_file_windows"])
    state = config_path.with_name("cognition-supervisor-status.json")
    child, attempts, terminating = None, 0, False
    def terminate(*args):
        nonlocal terminating
        terminating = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        with InstanceLock(config_path.with_suffix(".lock")):
            while not stop.exists() and not terminating:
                command = ["wsl.exe", "-d", config["distro"], "--exec", config["python"], config["script"],
                           "--config-root", config["config_root"], "--state", config["state"],
                           "--stop-file", config["stop_file_linux"], "--rounds", "0", "--daily-cap", str(config["daily_cap"]), "--bridge"]
                attempts += 1
                started = time.time()
                with Path(config["stdout"]).open("ab", buffering=0) as out, Path(config["stderr"]).open("ab", buffering=0) as err:
                    child = subprocess.Popen(command, stdout=out, stderr=err,
                                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    while child.poll() is None and not stop.exists() and not terminating:
                        save(state, {"supervisor_pid": os.getpid(), "child_pid": child.pid, "attempts": attempts,
                                     "status": "running", "started": started, "heartbeat": time.time()})
                        time.sleep(2)
                    if stop.exists() or terminating:
                        try:
                            child.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            child.terminate()
                            child.wait(timeout=10)
                        break
                code = child.returncode
                delay = min(60, 5 * attempts) if time.time() - started < 300 else 5
                save(state, {"supervisor_pid": os.getpid(), "child_pid": None, "attempts": attempts,
                             "status": "restarting", "last_exit": code, "heartbeat": time.time()})
                for _ in range(delay):
                    if stop.exists() or terminating:
                        break
                    time.sleep(1)
            save(state, {"supervisor_pid": os.getpid(), "child_pid": None, "attempts": attempts,
                         "status": "stopped", "heartbeat": time.time()})
    finally:
        if child and child.poll() is None:
            child.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    run(parser.parse_args().config.resolve())
