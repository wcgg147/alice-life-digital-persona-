"""Real LLM, executable controlled environment; NOT an internet-learning claim.

Training choices are model outputs, never hand-authored successful profiles.
Paired trials have identical raw experiences; only derived policies are disabled.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
from pathlib import Path
import random
import sqlite3
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alice"))
from cognition.environment import LiveModel, load_runtime
from cognition.loop import CognitiveLoop
from cognition.state import Ledger, digest, encoded


class FixtureEnvironment:
    def __init__(self, need, index, phase):
        self.need, self.index, self.phase = need, index, phase

    async def observe(self):
        kinds = [self.phase[0]] if self.phase[0] in {"a", "b"} else ["a", "b"]
        self.resources = [{"id": f"{self.need}-{self.index}-{self.phase}-{kind}",
                           "family": digest([self.need, self.index, kind])[:10],
                           "title": f"材料{self.index + 1}的新对象，入口{kind}",
                           "description": "实验环境登记的读取入口；是否提供所需证据须按真实读取回执判断"} for kind in kinds]
        # Alternate display order; there is no universally rewarded position.
        if self.index % 2:
            self.resources.reverse()
        return self.resources, []

    async def read(self, resource):
        good = resource["id"].endswith("-b")
        text = ("这是用于对照实验的来源原文，其中包含可读取的连续文本与已登记的出处。" * 8) if good or self.need != "text" else "只有短标题"
        audio = False
        if good and self.need == "audio":
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(8000)
                writer.writeframes(b"\0\0" * 800)
            with wave.open(io.BytesIO(buffer.getvalue()), "rb") as reader:
                audio = reader.getnframes() > 0
        return {"status": "ok", "source_ref": "controlled:" + resource["id"], "text": text,
                "audio_observed": audio, "primary_observed": good and self.need == "primary",
                "environment": "controlled_fixture_v1"}


class TraceModel:
    def __init__(self, model, path, label):
        self.model, self.path, self.label = model, path, label

    async def complete(self, messages):
        if self.model.calls >= 150:
            raise RuntimeError("evaluation_global_call_cap")
        started = time.monotonic()
        try:
            raw = await self.model.complete(messages)
            record = {"label": self.label, "model": self.model.name, "messages": messages,
                      "raw_output": raw, "seconds": round(time.monotonic() - started, 3)}
        except Exception as exc:
            record = {"label": self.label, "error_type": type(exc).__name__}
            raise
        finally:
            if "record" in locals():
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(encoded(record) + "\n")
        return raw


async def scenario(need, index, model, out, semaphore):
    async with semaphore:
        name = f"{need}-{index}"
        ledger = Ledger(out / (name + ".sqlite3"))
        # Explicit experimental task: not counted as autonomously generated interest.
        question = {"text": "取得新材料的可读正文", "audio": "取得新材料的真实音频",
                    "primary": "取得新材料的原始来源正文"}[need]
        with ledger.db:
            ledger.db.execute("INSERT INTO goals(owner,id,question,need,origin,status,updated) VALUES (?,?,?,?,?,?,?)",
                              (ledger.owner, "test-goal", question, need, "experiment_task", "active", time.time()))
        training = []
        for phase in ["a1", "b1", "a2", "b2"]:
            env = FixtureEnvironment(need, index, phase)
            trace = TraceModel(model, out / "model-trace.jsonl", name + ":train:" + phase)
            result = await CognitiveLoop(ledger, trace, env, daily_cap=12).tick()
            training.append(result)
        active_before = ledger.view()["strategies"]
        results = {}
        for condition in ["raw_only", "learned", "revoked"]:
            target = out / (name + "-" + condition + ".sqlite3")
            conn = sqlite3.connect(target)
            ledger.db.backup(conn)
            conn.close()
            trial = Ledger(target)
            try:
                if condition == "revoked":
                    for ep in trial.view()["episodes"]:
                        trial.revoke(ep["id"])
                trace = TraceModel(model, out / "model-trace.jsonl", name + ":" + condition)
                result = await CognitiveLoop(trial, trace, FixtureEnvironment(need, index, "heldout"),
                                             daily_cap=12, use_strategies=condition != "raw_only").tick()
                chosen = trial.view()["episodes"][0]["decision"].get("goal") or {}
                results[condition] = {**result, "requested_need": chosen.get("need"),
                                      "task_success": chosen.get("need") == need and bool(result.get("met_need")),
                                      "active_policies": len(trial.view()["strategies"])}
            except Exception as exc:
                results[condition] = {"status": "evaluation_error", "error_type": type(exc).__name__, "met_need": False}
            finally:
                trial.close()
        ledger.close()
        record = {"scenario": name, "need": need, "training": training,
                  "active_before_holdout": len(active_before), "conditions": results}
        with (out / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(encoded(record) + "\n")
        print(encoded({"scenario": name, "conditions": results}), flush=True)
        return record


async def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    model = LiveModel(load_runtime(args.config_root))
    semaphore = asyncio.Semaphore(3)
    try:
        # Preserve stdout decision events separately; request/response trace is exact.
        with (args.out / "events.log").open("w", encoding="utf-8") as events, contextlib.redirect_stdout(events):
            outcomes = await asyncio.gather(*(scenario(need, i, model, args.out, semaphore)
                                              for need in ["text", "audio", "primary"] for i in range(5)), return_exceptions=True)
        rows = [r for r in outcomes if isinstance(r, dict)]
        differences = [int(r["conditions"]["learned"].get("task_success", False)) - int(r["conditions"]["raw_only"].get("task_success", False)) for r in rows]
        rng = random.Random(20261104)
        boot = sorted(sum(rng.choices(differences, k=len(differences))) / len(differences) for _ in range(2000)) if differences else []
        summary = {"environment": "controlled_fixture_v1_real_model", "model": model.name,
                   "scenarios": len(rows), "scenario_errors": [type(r).__name__ for r in outcomes if isinstance(r, BaseException)],
                   "model_calls": model.calls,
                   "success_counts": {c: sum(bool(r["conditions"][c].get("task_success")) for r in rows) for c in ["raw_only", "learned", "revoked"]},
                   "paired_delta": sum(differences) / len(differences) if differences else None,
                   "paired_bootstrap_95": [boot[49], boot[1949]] if boot else None,
                   "note": "小样本同接口换对象测试；音频为程序生成的无声 WAV，仅验证音频可取得，不验证语言理解或互联网迁移。"}
        (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(encoded(summary), flush=True)
    finally:
        await model.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
