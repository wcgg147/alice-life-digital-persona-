"""Linux live diagnostics: actual source reads and read-only persona fingerprint."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "alice"), str(ROOT / "alice/life")]
from cognition.environment import ReadingEnvironment, load_runtime
from cognition.state import InstanceLock, Ledger, digest, encoded


async def probe(args, ledger):
    import asyncpg
    cfg = load_runtime(args.config_root)
    connection = await asyncpg.connect(os.environ.get("ALICE_DATABASE_URL") or os.environ["DATABASE_URL"])
    try:
        async with connection.transaction(readonly=True):
            before = await connection.fetchval("SELECT md5(string_agg(md5(row_to_json(t)::text),'' ORDER BY fact_id)) FROM companion_persona_facts t")
        env = ReadingEnvironment(args.config_root, cfg, ledger)
        resources, facts = await env.observe()
        print(encoded({"event": "catalog", "families": [r["family"] for r in resources],
                       "persona_facts_read": len(facts), "observation_limits": env.catalog_errors}))
        bili = next((r for r in resources if r["family"] == "bilibili_metadata_comments"), None)
        if bili:
            result = await env.read(bili)
            print(encoded({"event": "diagnostic_read_not_autonomous", "status": result["status"],
                           "source_ref": result["source_ref"], "text_chars": len(result["text"]), "hash": digest(result)}))
        async with connection.transaction(readonly=True):
            after = await connection.fetchval("SELECT md5(string_agg(md5(row_to_json(t)::text),'' ORDER BY fact_id)) FROM companion_persona_facts t")
        print(encoded({"event": "persona_integrity", "before": before, "after": after,
                       "unchanged_during_probe": before == after}))
        print(encoded({"event": "ledger", "revision": ledger.view()["revision"],
                       "goals": [{"id": g["id"], "status": g["status"]} for g in ledger.view()["goals"]],
                       "episodes": len(ledger.export()["episodes"]), "strategies": ledger.view()["strategies"]}))
    finally:
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    with InstanceLock(args.state.with_suffix(".lock")):
        ledger = Ledger(args.state)
        try:
            asyncio.run(probe(args, ledger))
        finally:
            ledger.close()
