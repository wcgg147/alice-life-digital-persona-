"""
L2 Memory Layer -- PostgreSQL

本层包含两个核心职责：

1. 结构化事实存储（PostgreSQL 关系表）
   - 存储 error_fingerprints、mastered_expressions、session_archives 等结构化数据
   - 支持遗忘曲线衰减、聚合查询等业务逻辑

2. 语义向量检索（PostgreSQL + pgvector）
   - 对会话摘要、错误模式等进行向量嵌入存储和相似性搜索
   - 使用 ivfflat 索引实现亚 100ms 向量检索

参考架构：
- Halldyll Memory Model: PostgreSQL for persistent storage + Redis for caching
- MemoryOS: Postgres (pgvector) + Redis 实现亚 100ms 检索

Hermes Agent correspondence: FTS5 session db + structured fact storage.
"""
import asyncpg
import contextlib
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
try:
    from ..config import (
        DATABASE_URL, WEIGHT_DECAY_FACTOR, WEIGHT_DELETE_THRESHOLD, as_user_uuid,
    )
except ImportError:
    from config import (
        DATABASE_URL, WEIGHT_DECAY_FACTOR, WEIGHT_DELETE_THRESHOLD, as_user_uuid,
    )
from .provider import MemoryEntry as MemoryFact
from .history_retrieval import rank_history_sparse
from .entities import (
    assemble_entity_neighborhood,
    entity_neighborhood_ids,
    entity_search_terms,
    merge_entity_search_observations,
    prepare_entity_rows,
    prepare_entity_relation_rows,
)
# 画像表的时间列一律喂 datetime —— asyncpg 对 timestamptz 参数不接受 str。
from .learner_profile import parse_dt as _lp_parse_dt
from .learner_profile import WRITE_LEASE_TTL_HOURS
from . import learner_profile as _learner_profile
from .relationship_merge import (
    SINGULAR_RELATIONSHIP_KEYS,
    merge_relationship_items,
    _created_at as _item_created_at,
)

# 保留本地别名：本层 30 多处调用点都写 _as_uuid，实现统一在 config。
_as_uuid = as_user_uuid


def _timestamp_without_timezone(value):
    """Normalize TIMESTAMP (without time zone) values to naive UTC."""
    if value is None:
        return None
    parsed = _lp_parse_dt(value)
    return parsed.replace(tzinfo=None) if parsed else value

_ASCII_WORD = re.compile(r"^[A-Za-z0-9_.\-]+$")

POWER_D = 0.7      # 幂律遗忘指数（Anderson & Schooler 1991）
SCALE_DAYS = 1.0


def _retrieval_strength(strength: float, last_seen: str, now: datetime) -> float:
    """此刻的提取强度（0-1）。跟 agent/life_line.retrieval_strength 同一公式。"""
    try:
        ts = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
    except Exception:
        return 1.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return (1.0 + age_days / (max(0.05, strength) * SCALE_DAYS)) ** (-POWER_D)


class L2PostgresMemory:

    def __init__(self, pool: Optional[asyncpg.Pool] = None):
        self._pool = pool

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
            await self._ensure_columns()
        return self._pool

    async def _ensure_columns(self):
        """补列（幂等）：`db/init.sql` 只对**新库**生效，已经在跑的库得靠这里跟上。

        plans/0916-24：`material_items.analysis` —— 深度分析产物（六字段 JSON）。
        只补不改：列已存在时 PG 直接跳过。补不上也不让进程起不来（记一条 warning，
        素材功能自己会给出明确错误），语音主链路不该被一个增强列的 DDL 拖死。
        """
        for statement in (
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS turn_id TEXT NOT NULL DEFAULT ''; "
            "CREATE INDEX IF NOT EXISTS idx_conversation_messages_turn "
            "ON conversation_messages(user_id, conv_id, turn_id) WHERE turn_id<>''",
            "ALTER TABLE material_items ADD COLUMN IF NOT EXISTS "
            "analysis JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE companion_persona_facts ADD COLUMN IF NOT EXISTS "
            "interaction_context TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_persona_facts ADD COLUMN IF NOT EXISTS "
            "interaction_context_source TEXT NOT NULL DEFAULT ''",
            # plans/1017-02 §8.56：未闭环的事。只对**已经在跑的库**补建，
            # 新库由 db/init.sql 建；两边同形。
            "CREATE TABLE IF NOT EXISTS companion_open_threads ("
            " user_id UUID NOT NULL, thread_id TEXT NOT NULL,"
            " topic TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'open', source_conv_id TEXT,"
            " source_quote TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL DEFAULT '', last_seen_at TEXT NOT NULL DEFAULT '',"
            " closed_at TEXT NOT NULL DEFAULT '', closed_reason TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (user_id, thread_id))",
            "CREATE INDEX IF NOT EXISTS idx_companion_open_threads "
            "ON companion_open_threads (user_id, status, last_seen_at DESC)",
            # §8.59：到点触发需要"事情什么时候发生"，不是"什么时候记下来"。
            "ALTER TABLE companion_open_threads ADD COLUMN IF NOT EXISTS "
            "due_hint TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_open_threads ADD COLUMN IF NOT EXISTS "
            "due_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_open_threads ADD COLUMN IF NOT EXISTS "
            "asked_at TEXT NOT NULL DEFAULT ''",
            # §8.90：供给度量——"给了她什么、用没用上"，零模型调用、只在会话末判命中。
            "CREATE TABLE IF NOT EXISTS companion_supply_log ("
            " user_id UUID NOT NULL, session_id TEXT NOT NULL DEFAULT '',"
            " kind TEXT NOT NULL DEFAULT '', ref_id TEXT NOT NULL DEFAULT '',"
            " ref_hint TEXT NOT NULL DEFAULT '', used BOOLEAN NOT NULL DEFAULT FALSE,"
            " created_at TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (user_id, session_id, kind, ref_id))",
            "CREATE INDEX IF NOT EXISTS idx_companion_supply_log "
            "ON companion_supply_log (user_id, created_at)",
            # §8.79：她自己的"这周"（世界线）。一周一轮、7 天过期。
            "CREATE TABLE IF NOT EXISTS companion_life_items ("
            " user_id UUID NOT NULL, item_id TEXT NOT NULL,"
            " text TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',"
            " aliases TEXT NOT NULL DEFAULT '',"
            " valid_from TEXT NOT NULL DEFAULT '', valid_to TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'active',"
            " source TEXT NOT NULL DEFAULT 'weekly',"
            " created_at TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (user_id, item_id))",
            "CREATE INDEX IF NOT EXISTS idx_companion_life_items "
            "ON companion_life_items (user_id, status, valid_to)",
            # §8.92：写入时存"近义抓手"，让"换个说法"也撞得上（零额外模型调用）。
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " aliases TEXT NOT NULL DEFAULT ''",
            # 记忆衰退：重复加固（hits/strength），时间遗忘（读取时按半衰期算）。
            # 不再靠"3 天硬过期"当唯一机制——那样第 4 天她连自己做过什么都想不起来。
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " topic TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " hits INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " strength DOUBLE PRECISION NOT NULL DEFAULT 1.0",
            # 编码深度（Craik & Lockhart 1972）：看得越深，起点越高、掉得越慢。
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " depth DOUBLE PRECISION NOT NULL DEFAULT 1.0",
            # 提取练习（Roediger & Karpicke 2006）：被真正想起来一次，比再看一遍管用。
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " last_recalled_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " recalls INTEGER NOT NULL DEFAULT 0",
            # 这条记忆对应哪条外部内容（bvid/url）。没有它，"回头去做点什么"
            # 就找不到那支视频——实践轮就只能是空谈。
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " ref TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " title TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " author TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " detail TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE companion_life_items ADD COLUMN IF NOT EXISTS "
            " actions_json TEXT NOT NULL DEFAULT '{}'",
            # 同一倾向被独立观察到几次。1 = 只说过/只看到过一次，还不够成立。
            # 闸门读它，而不是读"同一条原文是否重复"（后者永远为 1）。
            "ALTER TABLE companion_persona_facts ADD COLUMN IF NOT EXISTS "
            " support_count INTEGER NOT NULL DEFAULT 1",
            # §8.76：她自己问过、用户没答的问题（同时最多三条，3 天过期；
            # 只有用户的话把门打开时才会被提起）。
            "CREATE TABLE IF NOT EXISTS companion_open_questions ("
            " user_id UUID NOT NULL, question_id TEXT NOT NULL,"
            " text TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT '',"
            " source_conv_id TEXT,"
            " asked_at TEXT NOT NULL DEFAULT '', shown_at TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " created_at TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (user_id, question_id))",
            "ALTER TABLE companion_open_questions ADD COLUMN IF NOT EXISTS "
            " topic TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_companion_open_questions "
            "ON companion_open_questions (user_id, status, asked_at DESC)",
            "CREATE TABLE IF NOT EXISTS companion_outbox ("
            " user_id UUID NOT NULL, msg_id TEXT NOT NULL, conv_id TEXT,"
            " text TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',"
            " due_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',"
            " read_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (user_id, msg_id))",
            "CREATE INDEX IF NOT EXISTS idx_companion_outbox_unread "
            "ON companion_outbox (user_id, read_at, created_at DESC)",
        ):
            try:
                await self._pool.execute(statement)
            except Exception as exc:  # noqa: BLE001 - 见 docstring 的取舍
                print("[l2] ensure column skipped: %s" % str(exc)[:120])

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def insert_feedback_signal(self, signal: dict) -> None:
        pool = await self._get_pool()
        await pool.execute(
            """INSERT INTO feedback_signals
               (session_id, user_id, turn_index, feedback_type, signal_value, context, timestamp)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)""",
            signal.get("session_id", ""), signal.get("user_id", ""),
            signal.get("turn_index", 0), signal.get("feedback_type", ""),
            float(signal.get("signal_value", 0.0)),
            json.dumps(signal.get("context", {}), ensure_ascii=False),
            float(signal.get("timestamp", 0.0)),
        )

    async def insert_voice_turn_metric(self, user_id: str, session_id: str, metrics: dict) -> None:
        pool = await self._get_pool()
        await pool.execute(
            """INSERT INTO voice_turn_metrics
               (user_id, session_id, turn_index, asr_duration_ms, first_text_latency_ms,
                first_audio_latency_ms, response_duration_ms, first_user_speech_latency_ms,
                session_entry_mode, stopped_by_user)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            user_id, session_id, int(metrics.get("turn_index", 0)),
            metrics.get("asr_duration_ms"), metrics.get("first_text_latency_ms"),
            metrics.get("first_audio_latency_ms"), metrics.get("response_duration_ms"),
            metrics.get("first_user_speech_latency_ms"), metrics.get("session_entry_mode", "fallback"),
            bool(metrics.get("stopped_by_user", False)),
        )

    async def upsert_metering_event(self, event: dict) -> dict:
        """Persist the same privacy-safe contract used by the local backend."""
        from .metering_contract import merge_metering_event, normalize_metering_event

        incoming = normalize_metering_event(event)
        pool = await self._get_pool()
        row = await pool.fetchrow(
            """SELECT * FROM metering_events
               WHERE session_id=$1 AND turn_id=$2 AND event_scope=$3""",
            incoming["session_id"], incoming["turn_id"], incoming["event_scope"],
        )
        existing = dict(row) if row else {}
        if existing and not isinstance(existing.get("metadata"), dict):
            existing["metadata"] = dict(existing.get("metadata") or {})
        merged = merge_metering_event(existing, incoming)
        await pool.execute(
            """INSERT INTO metering_events
               (schema_version, user_id, session_id, turn_id, event_scope,
                request_status, provider, model, request_count, retry_count,
                input_tokens, output_tokens, total_tokens, token_usage_status,
                asr_input_duration_ms, asr_usage_status, tts_output_duration_ms,
                tts_character_count, tts_usage_status, canceled, failure_code,
                structured_probe_issued, structured_probe_result,
                learning_evidence_valid, metadata, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                       $17,$18,$19,$20,$21,$22,$23,$24,$25::jsonb,NOW())
               ON CONFLICT(session_id, turn_id, event_scope) DO UPDATE SET
                 schema_version=EXCLUDED.schema_version, user_id=EXCLUDED.user_id,
                 request_status=EXCLUDED.request_status, provider=EXCLUDED.provider,
                 model=EXCLUDED.model, request_count=EXCLUDED.request_count,
                 retry_count=EXCLUDED.retry_count, input_tokens=EXCLUDED.input_tokens,
                 output_tokens=EXCLUDED.output_tokens, total_tokens=EXCLUDED.total_tokens,
                 token_usage_status=EXCLUDED.token_usage_status,
                 asr_input_duration_ms=EXCLUDED.asr_input_duration_ms,
                 asr_usage_status=EXCLUDED.asr_usage_status,
                 tts_output_duration_ms=EXCLUDED.tts_output_duration_ms,
                 tts_character_count=EXCLUDED.tts_character_count,
                 tts_usage_status=EXCLUDED.tts_usage_status, canceled=EXCLUDED.canceled,
                 failure_code=EXCLUDED.failure_code,
                 structured_probe_issued=EXCLUDED.structured_probe_issued,
                 structured_probe_result=EXCLUDED.structured_probe_result,
                 learning_evidence_valid=EXCLUDED.learning_evidence_valid,
                 metadata=EXCLUDED.metadata, updated_at=NOW()""",
            merged["schema_version"], merged["user_id"], merged["session_id"],
            merged["turn_id"], merged["event_scope"], merged["request_status"],
            merged["provider"], merged["model"], merged["request_count"],
            merged["retry_count"], merged["input_tokens"], merged["output_tokens"],
            merged["total_tokens"], merged["token_usage_status"],
            merged["asr_input_duration_ms"], merged["asr_usage_status"],
            merged["tts_output_duration_ms"], merged["tts_character_count"],
            merged["tts_usage_status"], merged["canceled"], merged["failure_code"],
            merged["structured_probe_issued"], merged["structured_probe_result"],
            merged["learning_evidence_valid"], json.dumps(merged["metadata"], ensure_ascii=False),
        )
        return merged

    async def get_session_metering(self, session_id: str) -> list[dict]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            """SELECT * FROM metering_events WHERE session_id=$1
               ORDER BY CASE event_scope WHEN 'turn' THEN 0 ELSE 1 END, id""",
            session_id,
        )
        return [dict(row) for row in rows]

    async def finalize_session_metering(self, user_id: str, session_id: str, turn_count: int) -> dict:
        turns = [item for item in await self.get_session_metering(session_id) if item["event_scope"] == "turn"]
        def total(field: str) -> int | None:
            values = [int(item[field]) for item in turns if item.get(field) is not None]
            return sum(values) if values else None
        token_states = {item.get("token_usage_status", "unavailable") for item in turns}
        token_status = "reported" if token_states == {"reported"} and turns else ("partial" if token_states & {"reported", "partial"} else "unavailable")
        providers = {item.get("provider") for item in turns if item.get("provider")}
        models = {item.get("model") for item in turns if item.get("model")}
        failed_turns = sum(1 for item in turns if item.get("request_status") == "failed")
        canceled_turns = sum(1 for item in turns if item.get("canceled"))
        probe_results = {item.get("structured_probe_result") for item in turns}
        session_probe_result = "correct" if "correct" in probe_results else (
            "incorrect" if "incorrect" in probe_results else (
                "not_attempted" if any(bool(item.get("structured_probe_issued")) for item in turns) else "not_issued"
            )
        )
        event = {
            "user_id": user_id, "session_id": session_id, "turn_id": "__session__",
            "event_scope": "session",
            "request_status": "completed_with_failures" if failed_turns or canceled_turns else "completed",
            "provider": next(iter(providers)) if len(providers) == 1 else ("multiple" if providers else ""),
            "model": next(iter(models)) if len(models) == 1 else ("multiple" if models else ""),
            "request_count": sum(int(item.get("request_count") or 0) for item in turns),
            "retry_count": sum(int(item.get("retry_count") or 0) for item in turns),
            "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "total_tokens": total("total_tokens"), "token_usage_status": token_status,
            "asr_input_duration_ms": total("asr_input_duration_ms"),
            "asr_usage_status": "observed" if any(item.get("asr_input_duration_ms") is not None for item in turns) else "unavailable",
            "tts_output_duration_ms": total("tts_output_duration_ms"),
            "tts_character_count": total("tts_character_count"),
            "tts_usage_status": "observed" if any(item.get("tts_output_duration_ms") is not None or item.get("tts_character_count") is not None for item in turns) else "unavailable",
            "canceled": canceled_turns > 0,
            "structured_probe_issued": any(bool(item.get("structured_probe_issued")) for item in turns),
            "structured_probe_result": session_probe_result,
            "learning_evidence_valid": any(bool(item.get("learning_evidence_valid")) for item in turns),
            "metadata": {
                "session_turn_count": int(turn_count), "metered_turns": len(turns),
                "successful_turns": sum(1 for item in turns if item.get("request_status") == "success"),
                "failed_turns": failed_turns, "canceled_turns": canceled_turns,
            },
        }
        return await self.upsert_metering_event(event)

    async def get_recent_feedback_signals(self, user_id: str, limit: int = 20) -> list[dict]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            """SELECT session_id, user_id, turn_index, feedback_type,
                      signal_value, context, timestamp
               FROM feedback_signals WHERE user_id = $1
               ORDER BY timestamp DESC LIMIT $2""",
            user_id, limit,
        )
        return [{
            "session_id": row["session_id"], "user_id": row["user_id"],
            "turn_index": row["turn_index"], "feedback_type": row["feedback_type"],
            "signal_value": float(row["signal_value"]),
            "context": dict(row["context"] or {}), "timestamp": float(row["timestamp"]),
        } for row in rows]

    # ── 事实写入 ─────────────────────────────────────────────
    #
    # 这三个写入器与 l2_sqlite 的方法名/签名保持一致。调用点（learning_engine /
    # profile_evolution）**没有写 await** —— 本地后端它们是同步 def 所以能跑；
    # 这里实现为 coroutine，调用点统一用 maybe_await() 包一层，两边都成立。

    async def add_error(self, user_id: str, error_type: str,
                        error_excerpt: str = "", weight: float = 1.0) -> str:
        """记录一次错误指纹：同 (user, type) 累加 count 并提权。"""
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO error_fingerprints (user_id, error_type, error_excerpt, count, weight)
            VALUES ($1::uuid, $2, $3, 1, $4)
            ON CONFLICT (user_id, error_type) DO UPDATE SET
                error_excerpt = CASE WHEN EXCLUDED.error_excerpt <> ''
                                     THEN EXCLUDED.error_excerpt
                                     ELSE error_fingerprints.error_excerpt END,
                count = error_fingerprints.count + 1,
                last_seen_at = NOW(),
                weight = LEAST(error_fingerprints.weight + 0.1, 1.0)
            RETURNING id
            """,
            user_id, str(error_type)[:50], error_excerpt, float(weight),
        )
        return str(row["id"]) if row else ""

    async def set_error_fingerprint(self, user_id: str, error_type: str,
                                    count: int, weight: float) -> None:
        """用精确的 count/weight 覆盖指纹（反映遗忘曲线衰减）；count<=0 视为已解决并删除。"""
        user_id = _as_uuid(user_id)
        error_type = str(error_type)[:50]
        pool = await self._get_pool()
        if count <= 0 or weight <= 0.05:
            await pool.execute(
                "DELETE FROM error_fingerprints WHERE user_id = $1::uuid AND error_type = $2",
                user_id, error_type,
            )
            return
        await pool.execute(
            """
            INSERT INTO error_fingerprints (user_id, error_type, count, weight, last_seen_at)
            VALUES ($1::uuid, $2, $3, $4, NOW())
            ON CONFLICT (user_id, error_type) DO UPDATE SET
                count = EXCLUDED.count, weight = EXCLUDED.weight, last_seen_at = NOW()
            """,
            user_id, error_type, int(count), float(weight),
        )

    async def add_mastered(self, user_id: str, expression: str,
                           confidence: float = 0.8) -> None:
        """记录一条已掌握表达。"""
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        await pool.execute(
            "INSERT INTO mastered_expressions (user_id, expression, confidence) VALUES ($1::uuid, $2, $3)",
            user_id, expression, float(confidence),
        )

    async def add_fact(self, user_id: str, fact: MemoryFact) -> str:
        """单条事实的兼容入口（供 hasattr 兜底路径调用）。

        这里原先读的是 `fact.fact_type`，而 MemoryEntry 的字段名是 `entry_type`
        → Postgres 后端每次写入都抛 AttributeError；调用点又把它包在
        `except Exception: pass` 里，所以错题/已掌握会**静默丢失**。
        """
        entry_type = getattr(fact, "entry_type", None) or getattr(fact, "fact_type", "") or "error"
        if entry_type == "mastered":
            await self.add_mastered(user_id, fact.content, float(fact.weight or 0.8))
            return ""
        return await self.add_error(
            user_id, str(entry_type)[:50], str(fact.content)[:200], float(fact.weight or 1.0)
        )

    async def replace_fact(self, user_id: str, fact_id: str, new_content: str) -> bool:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE error_fingerprints
                SET error_excerpt = $3, last_seen_at = NOW()
                WHERE id = $1::bigint AND user_id = $2::uuid
                """,
                int(fact_id), user_id, new_content,
            )
            return result == "UPDATE 1"

    async def remove_fact(self, user_id: str, fact_id: str) -> bool:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM error_fingerprints WHERE id = $1::bigint AND user_id = $2::uuid",
                int(fact_id), user_id,
            )
            return result.startswith("DELETE")

    async def get_errors(self, user_id: str, limit: int = 20) -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, error_type, error_excerpt, count, weight, last_seen_at
                FROM error_fingerprints
                WHERE user_id = $1::uuid AND weight > $2
                ORDER BY weight DESC LIMIT $3
                """,
                user_id, WEIGHT_DELETE_THRESHOLD, limit,
            )
            return [dict(r) for r in rows]

    async def get_mastered(self, user_id: str, limit: int = 50) -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, expression, confidence, mastered_at
                FROM mastered_expressions
                WHERE user_id = $1::uuid
                ORDER BY confidence DESC LIMIT $2
                """,
                user_id, limit,
            )
            return [dict(r) for r in rows]

    async def get_learning_state(self, user_id: str, item_type: str) -> dict:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM learning_item_states WHERE user_id = $1::uuid AND item_type = $2",
                user_id, item_type,
            )
            return dict(row) if row else {}

    async def upsert_learning_state(
        self, user_id: str, item_type: str, mastery_probability: float, **changes
    ) -> None:
        user_id = _as_uuid(user_id)
        current = await self.get_learning_state(user_id, item_type)
        def total(field: str, delta_name: str) -> int:
            return int(current.get(field, 0)) + int(changes.get(delta_name, 0))
        next_review = _timestamp_without_timezone(changes.get("next_review_at"))
        stability = float(changes.get("stability_days", current.get("stability_days", 1.0) or 1.0))
        difficulty = float(changes.get("difficulty", current.get("difficulty", 5.0) or 5.0))
        last_review = _timestamp_without_timezone(
            changes.get("last_review_at", current.get("last_review_at"))
        )
        review_count = int(current.get("review_count", 0)) + int(changes.get("review_delta", 0))
        evidence = json.dumps(changes.get("last_evidence", {}), ensure_ascii=False)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO learning_item_states
                   (user_id, item_type, mastery_probability, detected_count, immediate_success,
                    transfer_success, delayed_success, failed_attempts, next_review_at,
                    stability_days, difficulty, last_review_at, review_count, last_evidence, updated_at)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb, NOW())
                   ON CONFLICT(user_id, item_type) DO UPDATE SET
                     mastery_probability=EXCLUDED.mastery_probability,
                     detected_count=EXCLUDED.detected_count,
                     immediate_success=EXCLUDED.immediate_success,
                     transfer_success=EXCLUDED.transfer_success,
                     delayed_success=EXCLUDED.delayed_success,
                     failed_attempts=EXCLUDED.failed_attempts,
                     next_review_at=EXCLUDED.next_review_at,
                     stability_days=EXCLUDED.stability_days,
                     difficulty=EXCLUDED.difficulty,
                     last_review_at=EXCLUDED.last_review_at,
                     review_count=EXCLUDED.review_count,
                     last_evidence=EXCLUDED.last_evidence,
                     updated_at=NOW()""",
                user_id, item_type, mastery_probability,
                total("detected_count", "detected_delta"),
                total("immediate_success", "immediate_success"),
                total("transfer_success", "transfer_success"),
                total("delayed_success", "delayed_success"),
                total("failed_attempts", "failed_delta"),
                next_review, stability, difficulty, last_review, review_count, evidence,
            )

    async def get_due_learning_reviews(self, user_id: str, limit: int = 3) -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM learning_item_states
                   WHERE user_id = $1::uuid AND next_review_at IS NOT NULL AND next_review_at <= NOW()
                   ORDER BY mastery_probability ASC, next_review_at ASC LIMIT $2""",
                user_id, limit,
            )
            return [dict(row) for row in rows]

    async def get_recent_sessions(self, user_id: str, days: int = 7) -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, summary, error_count,
                       cefr_before, cefr_after, duration_seconds, created_at
                FROM session_archives
                WHERE user_id = $1::uuid
                  AND created_at > NOW() - ($2 || ' days')::interval
                ORDER BY created_at DESC
                """,
                user_id, str(days),
            )
            return [dict(r) for r in rows]

    async def get_relationship_memory(self, user_id: str) -> dict:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM relationship_memory WHERE user_id = $1::uuid",
                user_id,
            )
            result = dict(row) if row else {}
            # These legacy columns are JSONB lists. asyncpg returns their JSON
            # representation as str; parse through the shared tolerant helper
            # before any caller applies list() (which would split into chars).
            try:
                from ..engine.chunk_evidence import loads_json_list
            except ImportError:
                from engine.chunk_evidence import loads_json_list
            legacy_engagement_topics = loads_json_list(result.get("engagement_topics"))
            legacy_avoid_patterns = loads_json_list(result.get("avoid_patterns"))
            # asyncpg 默认把 jsonb 返回为 str——不解析则 list() 迭代的是字符，
            # 每个条目在过期检查时抛 AttributeError 被吞，active 恒为空
            # （postgres 后端关系记忆此前一直静默失效，plans/0915-08 修复）。
            raw_items = result.get("memory_items")
            if isinstance(raw_items, str):
                try:
                    raw_items = json.loads(raw_items)
                except (ValueError, TypeError):
                    raw_items = []
            items = list(raw_items or [])
            now = datetime.now(timezone.utc)
            active = []
            superseded_history = []
            hidden_history = []
            from memory.companion_persona import is_legacy_persona_memory_key
            for item in items:
                try:
                    if item.get("hidden_at"):
                        hidden_history.append(item)
                        continue
                    if is_legacy_persona_memory_key(item.get("key")):
                        continue
                    # 0915-12 流水账：被顶替的旧值打 superseded_by 标记留底，
                    # 读侧据此消解——当前有效值 = 最新的未 superseded 条目。
                    if item.get("superseded_by"):
                        superseded_history.append(item)
                        continue
                    expires = item.get("expires_at")
                    if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) <= now:
                        continue
                except Exception:
                    continue
                active.append(item)
            result["memory_items"] = active
            result["superseded_items"] = superseded_history
            result["hidden_items"] = hidden_history
            result["engagement_topics"] = list(dict.fromkeys(
                [str(item.get("value") or "").strip() for item in reversed(active)
                 if item.get("key") == "engagement_topic" and str(item.get("value") or "").strip()]
                + [str(value).strip() for value in legacy_engagement_topics if str(value).strip()]
            ))[:8]
            result["avoid_patterns"] = list(dict.fromkeys(
                [str(value).strip() for value in legacy_avoid_patterns if str(value).strip()]
            ))
            if active:
                result["last_topic"] = ""
                result["open_loop"] = ""
                result["recent_mood"] = ""
                result["preferred_tone"] = ""
            for item in active:
                key = item.get("key")
                if key in (
                    "last_topic", "open_loop", "recent_mood", "preferred_tone",
                    "learning_preference", "self_reported_weakness", "next_session_intent",
                    "next_session_intent_consumed",
                    # 身份记忆（plans/0915-04）：名字要浮到顶层供 prompt 身份行读取
                    "learner_name", "tutor_name",
                    # 戏剧别名（plans/0915-16）：同浮顶层，多条共存时最新一条生效
                    "learner_roleplay_alias", "tutor_roleplay_alias",
                ):
                    result[key] = item.get("value", "")
                elif key == "avoid_pattern":
                    result.setdefault("avoid_patterns", []).append(item.get("value", ""))
            result["avoid_patterns"] = list(dict.fromkeys(
                str(value).strip() for value in result.get("avoid_patterns", []) if str(value).strip()
            ))[-8:]
            preference = str(result.get("learning_preference") or "")
            if (
                not result.get("next_session_intent")
                and result.get("next_session_intent_consumed") != preference
                and any(
                marker in preference.lower()
                for marker in ("下次", "下一次", "下回", "以后如果继续", "以后继续练", "next time", "next session")
                )
            ):
                result["next_session_intent"] = preference
            return result

    async def get_last_archive_time(self, user_id: str) -> dict:
        """最近一盘归档的时间与摘要（plans/0915-17 时间系统：AI 要感受到时间流逝）。"""
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT summary, created_at FROM session_archives "
                "WHERE user_id = $1::uuid ORDER BY created_at DESC LIMIT 1",
                user_id,
            )
        if not row:
            return {}
        created = row.get("created_at")
        return {
            "summary": str(row.get("summary") or ""),
            "created_at": created.isoformat() if created else "",
        }

    async def compact_relationship_memory(self, user_id: str, *, max_items: int = 30) -> dict:
        """关系记忆物理清淤（plans/0915-08，承接 dream「压缩」理念的第一步）。

        过期条目此前只在读取时过滤、DB 里永远保留，还会占掉写侧 30 条预算
        挤出新鲜条目。本方法做三件确定性的事（无 LLM）：
        1. 物理清除已过期条目（与 get_relationship_memory 同一过期判定）
        2. (key, value) 完全重复的只保留最新一条
        3. 总量裁回 max_items（与写侧 merge_relationship_items 上限一致）
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT memory_items FROM relationship_memory WHERE user_id = $1::uuid",
                user_id,
            )
            if not row:
                return {"purged_expired": 0, "deduped": 0, "capped": 0}
            raw_items = row["memory_items"]
            if isinstance(raw_items, str):
                try:
                    raw_items = json.loads(raw_items)
                except (ValueError, TypeError):
                    raw_items = []
            items = list(raw_items or [])
        now = datetime.now(timezone.utc)
        kept, purged_expired = [], 0
        for item in items:
            expires = str((item or {}).get("expires_at") or "")
            if expires:
                try:
                    if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= now:
                        purged_expired += 1
                        continue
                except ValueError:
                    pass
            kept.append(item)
        # 去重：新→旧扫，首个出现者胜；(key,value) 完全重复算重复；
        # singular 同键多值对**留底条目**（superseded）豁免——一个键多份历史
        # 是流水账的设计意图，不是脏数据（0915-12）。
        seen, seen_singular, deduped_items, deduped = set(), set(), [], 0
        for item in sorted(kept, key=_item_created_at, reverse=True):
            sig = (item.get("key"), item.get("value"))
            key = item.get("key")
            is_history = bool(item.get("superseded_by"))
            if sig in seen or (
                not is_history
                and key in SINGULAR_RELATIONSHIP_KEYS
                and key in seen_singular
            ):
                deduped += 1
                continue
            seen.add(sig)
            if not is_history and key in SINGULAR_RELATIONSHIP_KEYS:
                seen_singular.add(key)
            deduped_items.append(item)
        deduped_items.reverse()  # 恢复旧→新顺序
        # 限额：live 条目用 max_items 预算；留底条目单独预算（先丢最旧的），
        # 不再让历史挤掉有效记忆的仓位。
        live_items = [i for i in deduped_items if not i.get("superseded_by")]
        history_items = [i for i in deduped_items if i.get("superseded_by")]
        capped = 0
        if len(live_items) > max_items:
            capped += len(live_items) - max_items
            live_items = live_items[-max_items:]
        history_cap = 24
        if len(history_items) > history_cap:
            capped += len(history_items) - history_cap
            history_items = history_items[-history_cap:]
        deduped_items = live_items + history_items
        if purged_expired or deduped or capped:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE relationship_memory SET memory_items=$2::jsonb, updated_at=NOW() WHERE user_id=$1::uuid",
                    user_id, json.dumps(deduped_items, ensure_ascii=False),
                )
        return {"purged_expired": purged_expired, "deduped": deduped, "capped": capped}

    async def compact_relationship_memory_all(self, *, max_items: int = 30) -> dict:
        """全用户版关系记忆清淤（audit_scheduler 每日任务用）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT user_id FROM relationship_memory")
        totals = {"users": 0, "purged_expired": 0, "deduped": 0, "capped": 0}
        for r in rows:
            res = await self.compact_relationship_memory(str(r["user_id"]), max_items=max_items)
            totals["users"] += 1
            for k in ("purged_expired", "deduped", "capped"):
                totals[k] += res[k]
        return totals

    async def upsert_relationship_items(self, user_id: str, items: list[dict]) -> None:
        user_id = _as_uuid(user_id)
        current = await self.get_relationship_memory(user_id)
        # merge 基底必须是全量（live + superseded 留底），否则每次 upsert
        # 都会把历史静默抹掉（0915-12）。
        base = (list(current.get("memory_items", []))
                + list(current.get("superseded_items", []))
                + list(current.get("hidden_items", [])))
        merged = merge_relationship_items(base, items)
        pool = await self._get_pool()
        await pool.execute(
            """INSERT INTO relationship_memory (user_id, memory_items, updated_at)
               VALUES ($1::uuid, $2::jsonb, NOW())
               ON CONFLICT(user_id) DO UPDATE SET memory_items=EXCLUDED.memory_items, updated_at=NOW()""",
            user_id, json.dumps(merged, ensure_ascii=False),
        )

    async def memory_depth(self, user_id: str) -> int:
        """「她记得你多少」——替代那个永远不会变的关系标签（plans/1017-02 §8.37）。

        三个来源相加：实体数 ＋ 画像事实数 ＋ 有内容的归档数（摘要去掉模板后 ≥20 字）。
        一次查询、每盘只跑一次；失败返回 0，调用方按"没算过"处理。
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            entity_count = await conn.fetchval(
                """SELECT count(*) FROM memory_entities WHERE user_id = $1::uuid""",
                user_id) or 0
            fact_count = await conn.fetchval(
                """SELECT count(*) FROM learner_profile_facts
                   WHERE user_id = $1::uuid AND coalesce(status,'active') = 'active'""",
                user_id) or 0
            archive_rows = await conn.fetch(
                """SELECT summary FROM session_archives WHERE user_id = $1::uuid""",
                user_id)
        formatted = 0
        for row in archive_rows:
            body = str(row["summary"] or "").split("Practice English")[0]
            body = body.replace("具体事件：", "").strip("；; ")
            if len(body) >= 20:
                formatted += 1
        return int(entity_count) + int(fact_count) + formatted

    async def delete_relationship_memory_item(self, user_id: str, memory_key: str) -> int:
        user_id = _as_uuid(user_id)
        current = await self.get_relationship_memory(user_id)
        # 0915-12：真删必须覆盖留底条目（用户要求"能导出、能真删"），
        # superseded 历史也是他的数据，导出可见即可删。
        before = (list(current.get("memory_items", []))
                  + list(current.get("superseded_items", []))
                  + list(current.get("hidden_items", [])))
        after = [item for item in before if item.get("id") != memory_key]
        if len(after) == len(before):
            return 0
        pool = await self._get_pool()
        await pool.execute(
            "UPDATE relationship_memory SET memory_items=$2::jsonb, updated_at=NOW() WHERE user_id=$1::uuid",
            user_id, json.dumps(after, ensure_ascii=False),
        )
        return 1

    # ── Alice 自我人格记忆（SQLite 方法面完全一致）────────────────────

    @staticmethod
    def _companion_persona_row(row) -> dict:
        if not row:
            return {}
        from engine.chunk_evidence import loads_json_list
        item = dict(row)
        item["supporting_fact_ids"] = loads_json_list(item.get("supporting_fact_ids"))
        item["related_entity_ids"] = loads_json_list(item.get("related_entity_ids"))
        return item

    async def ensure_companion_persona_legacy_migrated(self, user_id: str) -> int:
        """Import old Alice rows and user-confirmed archived identity claims once."""
        from memory.companion_persona import (
            LEGACY_BACKFILL_VERSION, legacy_items_to_facts,
            legacy_confirmed_identity_to_facts,
        )
        from engine.chunk_evidence import loads_json_list
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            marker = await conn.fetchval(
                "SELECT legacy_backfill_version FROM companion_persona_snapshots "
                "WHERE user_id=$1::uuid AND persona_key='alice'", uid,
            )
            if marker == LEGACY_BACKFILL_VERSION:
                return 0
            raw = await conn.fetchval(
                "SELECT memory_items FROM relationship_memory WHERE user_id=$1::uuid", uid,
            )
            archive_rows = await conn.fetch(
                """SELECT m.conv_id, m.seq, m.content, m.created_at,
                          u.seq AS confirmed_by_seq, u.content AS confirmation
                   FROM conversation_messages m
                   JOIN conversation_meta c
                     ON c.user_id=m.user_id AND c.conv_id=m.conv_id
                   JOIN conversation_messages u
                     ON u.user_id=m.user_id AND u.conv_id=m.conv_id
                    AND u.seq=m.seq + 1 AND u.role='user'
                   WHERE m.user_id=$1::uuid AND m.role='assistant'
                     AND c.hidden_at IS NULL
                     AND LOWER(COALESCE(c.scene,'')) IN ('daily','daily_talk','free_talk')
                     AND (m.content ILIKE '%from%' OR m.content ILIKE '%grew up%'
                          OR m.content ILIKE '%born in%' OR m.content ILIKE '%live in%'
                          OR m.content ILIKE '%Singaporean%')
                   ORDER BY m.created_at ASC, m.seq ASC""", uid,
            )
        facts = legacy_items_to_facts(loads_json_list(raw))
        written = await self.upsert_companion_persona_facts(user_id, facts, guard_identity=False)
        archive_facts = legacy_confirmed_identity_to_facts(
            [dict(row) for row in archive_rows], user_id=str(user_id),
        )
        written += await self.upsert_companion_persona_facts(
            user_id, archive_facts, guard_identity=True,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO companion_persona_snapshots
                       (user_id, persona_key, legacy_backfill_version)
                   VALUES ($1::uuid, 'alice', $2)
                   ON CONFLICT (user_id, persona_key) DO UPDATE
                   SET legacy_backfill_version=EXCLUDED.legacy_backfill_version""",
                uid, LEGACY_BACKFILL_VERSION,
            )
        return int(written or 0)

    async def upsert_companion_persona_facts(
        self, user_id: str, facts: list[dict], guard_identity: bool = True,
        source_conv_id: str = "",
    ) -> int:
        if not facts:
            return 0
        from engine.chunk_evidence import loads_json_list
        from memory.companion_persona import arbitrate_version_change
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        written = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for fact in facts:
                    if not isinstance(fact, dict):
                        continue
                    fact_id = str(fact.get("fact_id") or fact.get("id") or "").strip()
                    value = str(fact.get("value_text") or fact.get("value") or "").strip()[:1000]
                    if not fact_id or not value:
                        continue
                    persona_key = str(fact.get("persona_key") or "alice").strip()[:80]
                    kind = str(fact.get("kind") or "").strip()[:80]
                    facet = str(fact.get("facet_key") or "").strip()[:180]
                    status = str(fact.get("status") or "active").strip()
                    if status not in {"active", "candidate", "superseded", "forgotten"}:
                        status = "candidate"
                    if guard_identity and kind == "identity" and facet in {"origin", "residence"}:
                        established = await conn.fetchval(
                            """SELECT value_text FROM companion_persona_facts
                               WHERE user_id=$1::uuid AND persona_key=$2 AND kind='identity'
                                 AND facet_key=$3 AND status='active' AND hidden_at IS NULL
                                 AND valid_to=''
                               ORDER BY recorded_at ASC LIMIT 1""",
                            uid, persona_key, facet,
                        )
                        if established and str(established).casefold() != value.casefold():
                            status = "candidate"
                    supersedes = str(fact.get("supersedes") or "")[:180]
                    valid_from = str(fact.get("valid_from") or "")[:80]
                    try:
                        support_count_val = max(1, int(fact.get("support_count") or 1))
                    except (TypeError, ValueError):
                        support_count_val = 1
                    if guard_identity and kind in {"trait", "preference", "ability", "language_environment", "current_status"} and facet:
                        versions = await conn.fetch(
                            """SELECT fact_id, value_text, status, source_conv_id, observed_at, valid_from, support_count
                               FROM companion_persona_facts
                               WHERE user_id=$1::uuid AND persona_key=$2 AND kind=$3 AND facet_key=$4
                                 AND status IN ('active','candidate') AND hidden_at IS NULL""",
                            uid, persona_key, kind, facet,
                        )
                        version_rows = [dict(row) for row in versions]
                        same_value_sources = [
                            str(row.get("source_conv_id") or "") for row in version_rows
                            if str(row.get("value_text") or "").casefold() == value.casefold()
                        ]
                        # §8.42：同一 facet 的 candidate 在 ≥2 个独立会话/视频被观察到时（措辞可不同），
                        # 计入候选证据完成升格；注意不把已 active 的冲突旧版本算进新说法支持源。
                        candidate_facet_sources = [
                            str(row.get("source_conv_id") or "") for row in version_rows
                            if row.get("status") == "candidate"
                        ]
                        same_value_sources = list(dict.fromkeys(
                            same_value_sources + candidate_facet_sources))
                        transition = arbitrate_version_change(
                            {**fact, "kind": kind, "facet_key": facet, "value_text": value,
                             "status": status, "supersedes": supersedes, "valid_from": valid_from},
                            version_rows, same_value_sources,
                        )
                        import os as _os
                        if _os.getenv("RELATIONSHIP_DEBUG_STAGE"):
                            print(f"[DBG] arb kind={kind} facet={facet} "
                                  f"in={status} versions={len(version_rows)} "
                                  f"sources={len(set(same_value_sources))} "
                                  f"→ {transition.get('status')}")
                        status = str(transition.get("status") or status)
                        supersedes = str(transition.get("supersedes") or supersedes)[:180]
                        valid_from = str(transition.get("valid_from") or valid_from)[:80]
                        support_count_val = max(
                            support_count_val,
                            int(transition.get("support_count") or 1),
                        )
                        change_time = str(transition.get("change_time") or "")
                        supersede_ids = list(transition.get("supersede_ids") or [])
                        promote_ids = list(transition.get("promote_ids") or [])
                        supersede_cand_ids = list(transition.get("supersede_candidate_ids") or [])
                        ts_mark = (
                            change_time
                            or str(fact.get("recorded_at") or "")[:80]
                            or datetime.now(timezone.utc).isoformat()
                        )
                        if change_time and supersede_ids:
                            await conn.execute(
                                """UPDATE companion_persona_facts
                                   SET status='superseded', valid_to=$4
                                   WHERE user_id=$1::uuid AND persona_key=$2 AND fact_id=ANY($3::text[])
                                     AND status='active'""",
                                uid, persona_key, supersede_ids, change_time,
                            )
                        if supersede_cand_ids:
                            await conn.execute(
                                """UPDATE companion_persona_facts
                                   SET status='superseded', valid_to=$4
                                   WHERE user_id=$1::uuid AND persona_key=$2 AND fact_id=ANY($3::text[])
                                     AND status='candidate'""",
                                uid, persona_key, supersede_cand_ids, ts_mark,
                            )
                        if promote_ids:
                            await conn.execute(
                                """UPDATE companion_persona_facts
                                   SET status='active', valid_to='',
                                       support_count=GREATEST(support_count, $4)
                                   WHERE user_id=$1::uuid AND persona_key=$2 AND fact_id=ANY($3::text[])
                                     AND status='candidate'""",
                                uid, persona_key, promote_ids, support_count_val,
                            )
                    supporting = loads_json_list(fact.get("supporting_fact_ids"))
                    related = loads_json_list(fact.get("related_entity_ids"))
                    occurrence = str(fact.get("source_occurrence_id") or fact_id).strip()[:240]
                    confidence = fact.get("confidence") or 0.0
                    try:
                        confidence = max(0.0, min(1.0, float(confidence)))
                    except (TypeError, ValueError):
                        confidence = 0.0
                    result = await conn.execute(
                        """INSERT INTO companion_persona_facts (
                               user_id, persona_key, fact_id, kind, facet_key, value_text,
                               status, source_kind, source_conv_id, source_turn,
                               source_message_ref, evidence_quote, interaction_context,
                               interaction_context_source, confidence, observed_at,
                               valid_from, valid_to, recorded_at, time_basis, supersedes,
                               supporting_fact_ids, related_entity_ids, legacy_fact_id,
                               source_occurrence_id, hidden_at, support_count
                           ) VALUES (
                               $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                               $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                               $21, $22::jsonb, $23::jsonb, $24, $25, $26, $27
                           ) ON CONFLICT DO NOTHING""",
                        uid, persona_key, fact_id, kind, facet, value, status,
                        str(fact.get("source_kind") or fact.get("source") or "unknown")[:80],
                        str(fact.get("source_conv_id") or "")[:160] or None,
                        fact.get("source_turn"), str(fact.get("source_message_ref") or "")[:200],
                        str(fact.get("evidence_quote") or fact.get("evidence") or "")[:1000],
                        str(fact.get("interaction_context") or "")[:500],
                        str(fact.get("interaction_context_source") or "")[:40],
                        confidence, str(fact.get("observed_at") or "")[:80],
                         valid_from,
                        str(fact.get("valid_to") or "")[:80],
                        str(fact.get("recorded_at") or fact.get("created_at") or "")[:80],
                        str(fact.get("time_basis") or "")[:40],
                        str(fact.get("supersedes") or "")[:180],
                        json.dumps(supporting, ensure_ascii=False),
                        json.dumps(related, ensure_ascii=False),
                        str(fact.get("legacy_fact_id") or "")[:180],
                        occurrence, fact.get("hidden_at") or None, support_count_val,
                    )
                    inserted = int(str(result).split()[-1])
                    written += inserted
                    # Version only a newly inserted state in the same typed slot.
                    # Distinct experiences coexist; an ignored replay changes nothing here.
                    if status == "active" and inserted and kind != "experience" and facet:
                        await conn.execute(
                            """UPDATE companion_persona_facts
                               SET status='superseded', valid_to=$6
                               WHERE user_id=$1::uuid AND persona_key=$2 AND kind=$3 AND facet_key=$4
                                 AND fact_id<>$5 AND status='active'""",
                            uid, persona_key, kind, facet, fact_id,
                            str(fact.get("recorded_at") or "")[:80]
                            or datetime.now(timezone.utc).isoformat(),
                        )
                if written:
                    await conn.execute(
                        """UPDATE companion_persona_snapshots
                           SET narrative='', supporting_fact_ids='[]'::jsonb,
                               snapshot_version='', source_hash='', generated_at=NULL
                           WHERE user_id=$1::uuid AND persona_key='alice'""", uid,
                    )
        return written

    async def list_persona_facets(self, user_id: str, limit: int = 40) -> list[str]:
        """她已有的 facet 清单（plans/1017-02 §8.45）。

        用途：抽新自述时把这份清单交给模型，要求**同一件事沿用已有 slug**——
        实测同一件事会抽出 `spice-tolerance` 和 `mild-food` 两种 slug，
        按 facet 升格就永远不会发生。
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT DISTINCT facet_key FROM companion_persona_facts
                   WHERE user_id=$1::uuid AND persona_key='alice' AND hidden_at IS NULL
                     AND status IN ('active','candidate') AND facet_key <> ''
                   ORDER BY facet_key LIMIT $2""", user_id, max(1, min(200, limit)))
        return [str(row["facet_key"]) for row in rows if row["facet_key"]]

    # ── 未闭环的事（plans/1017-02 §8.56）────────────────────────────────
    async def upsert_open_threads(
        self, user_id: str, threads: list[dict], source_conv_id: str = "",
    ) -> int:
        """写入"他提过、还没有下文的事"。

        `thread_id` 由 topic 派生 ⇒ 同一件事反复提不会堆成多条，只刷新
        `last_seen_at`；已关闭的又被提起则重新打开。
        """
        uid = _as_uuid(user_id)
        rows = [t for t in (threads or [])
                if isinstance(t, dict) and str(t.get("topic") or "").strip()]
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        written = 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            for item in rows:
                topic = str(item.get("topic") or "").strip()[:80]
                thread_id = str(item.get("thread_id") or "").strip() or (
                    "th_" + uuid.uuid5(uuid.NAMESPACE_URL, topic.casefold()).hex[:12]
                )
                await conn.execute(
                    """INSERT INTO companion_open_threads
                       (user_id, thread_id, topic, detail, status, source_conv_id,
                        source_quote, created_at, last_seen_at, due_hint, due_at)
                       VALUES ($1::uuid,$2,$3,$4,'open',$5,$6,$7,$7,$8,$9)
                       ON CONFLICT (user_id, thread_id) DO UPDATE
                       SET detail = EXCLUDED.detail,
                           last_seen_at = EXCLUDED.last_seen_at,
                           status = 'open',
                           closed_at = '', closed_reason = '',
                           due_hint = EXCLUDED.due_hint,
                           due_at = EXCLUDED.due_at""",
                    uid, thread_id, topic, str(item.get("detail") or "")[:240],
                    source_conv_id or None, str(item.get("quote") or "")[:200], now,
                    str(item.get("due_hint") or "")[:60],
                    str(item.get("due_at") or "")[:32],
                )
                written += 1
        return written

    async def list_open_threads(self, user_id: str, limit: int = 8) -> list[dict]:
        """还没下文的事，最近提到的排前面。"""
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT thread_id, topic, detail, source_quote, last_seen_at,"
                " due_hint, due_at, asked_at"
                " FROM companion_open_threads"
                " WHERE user_id=$1::uuid AND status='open'"
                " ORDER BY last_seen_at DESC LIMIT $2",
                uid, max(1, min(50, int(limit or 8))),
            )
        return [dict(row) for row in rows]

    # ── 她的留言箱（§8.60）────────────────────────────────────────────
    async def enqueue_outbox(self, user_id: str, text: str, *, reason: str = "",
                             conv_id: str = "", due_at: str = "",
                             msg_id: str = "") -> str:
        """她留一条消息给用户（用户不在时）。**同一 reason 一天只发一条**。"""
        import uuid as _uuid
        body = " ".join(str(text or "").split())[:400]
        if not body:
            return ""
        uid = _as_uuid(user_id)
        now = datetime.now(timezone.utc).isoformat()
        mid = msg_id or ("ob_" + _uuid.uuid4().hex[:12])
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # 幂等：同一 reason（同一件事）同一天不重复发。
            if reason:
                dup = await conn.fetchval(
                    "SELECT count(*) FROM companion_outbox"
                    " WHERE user_id=$1::uuid AND reason=$2 AND created_at >= $3",
                    uid, reason, now[:10],
                )
                if dup:
                    return ""
            await conn.execute(
                """INSERT INTO companion_outbox
                   (user_id, msg_id, conv_id, text, reason, due_at, created_at)
                   VALUES ($1::uuid,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (user_id, msg_id) DO NOTHING""",
                uid, mid, conv_id or None, body, reason[:80], due_at[:32], now,
            )
        return mid

    async def list_outbox_unread(self, user_id: str, limit: int = 5) -> list[dict]:
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT msg_id, text, reason, due_at, created_at FROM companion_outbox"
                " WHERE user_id=$1::uuid AND read_at='' ORDER BY created_at DESC LIMIT $2",
                uid, max(1, min(20, int(limit or 5))),
            )
        return [dict(row) for row in rows]

    async def mark_outbox_read(self, user_id: str, msg_id: str) -> int:
        uid = _as_uuid(user_id)
        now = datetime.now(timezone.utc).isoformat()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE companion_outbox SET read_at=$3"
                " WHERE user_id=$1::uuid AND msg_id=$2 AND read_at=''",
                uid, str(msg_id or ""), now,
            )
        return 1

    async def mark_thread_asked(self, user_id: str, thread_ids: list[str]) -> int:
        """回写：她**问过了**。存在库里，重启后也知道"别天天问同一件"。"""
        ids = [str(x) for x in (thread_ids or []) if str(x).strip()]
        if not ids:
            return 0
        uid = _as_uuid(user_id)
        now = datetime.now(timezone.utc).isoformat()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE companion_open_threads SET asked_at=$3"
                " WHERE user_id=$1::uuid AND thread_id = ANY($2::text[])",
                uid, ids, now,
            )
        return len(ids)

    async def close_open_threads(
        self, user_id: str, thread_ids: list[str], reason: str = "",
    ) -> int:
        """用户说清楚了结果 ⇒ 关掉，不再追问。"""
        ids = [str(x) for x in (thread_ids or []) if str(x).strip()]
        if not ids:
            return 0
        uid = _as_uuid(user_id)
        now = datetime.now(timezone.utc).isoformat()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE companion_open_threads SET status='closed', closed_at=$3,"
                " closed_reason=$4 WHERE user_id=$1::uuid AND thread_id = ANY($2::text[])",
                uid, ids, now, str(reason or "")[:120],
            )
        return len(ids)

    async def list_recent_life_items(self, user_id: str, limit: int = 5) -> list[dict]:
        """连过期的一起取（最近的在前）——用来让"这周"接上"上周"（§8.80）。"""
        pool = await self._get_pool()
        rows = await pool.fetch(
            """SELECT item_id, text, kind, valid_to, aliases FROM companion_life_items
               WHERE user_id=$1::uuid ORDER BY created_at DESC LIMIT $2""",
            user_id, max(1, limit))
        return [dict(r) for r in rows]

    async def log_supply(self, user_id: str, session_id: str, kind: str,
                         ref_id: str, ref_hint: str = "") -> None:
        """记一条"这一轮摆给了她什么"（§8.90）。零模型调用；失败绝不打扰对话。"""
        pool = await self._get_pool()
        await pool.execute(
            """INSERT INTO companion_supply_log
                   (user_id, session_id, kind, ref_id, ref_hint, created_at)
               VALUES ($1::uuid, $2, $3, $4, $5, $6)
               ON CONFLICT (user_id, session_id, kind, ref_id) DO NOTHING""",
            user_id, str(session_id or ""), str(kind or ""), str(ref_id or ""),
            str(ref_hint or "")[:160], datetime.now(timezone.utc).isoformat())

    async def mark_supply_hits(self, user_id: str, session_id: str,
                              user_texts: list[str]) -> int:
        """会话末统一判命中：他这盘里有没有一句碰到过我们摆出去的东西。

        ⛔ 故意**不在轮次路径上**做（那是 Codex 正在收敛的延迟层）；
        判据是词面二元组，会低估（"嗯，对"这种接话判不到），已知并在档案里写明。
        """
        texts = [str(t or "") for t in (user_texts or []) if str(t or "").strip()]
        if not texts:
            return 0
        pool = await self._get_pool()
        rows = await pool.fetch(
            """SELECT kind, ref_id, ref_hint FROM companion_supply_log
               WHERE user_id=$1::uuid AND session_id=$2 AND used=FALSE""",
            user_id, str(session_id or ""))
        hits = 0
        for row in rows:
            hint = str(row.get("ref_hint") or "")
            if any(self._supply_overlap(hint, t) for t in texts):
                await pool.execute(
                    """UPDATE companion_supply_log SET used=TRUE
                       WHERE user_id=$1::uuid AND session_id=$2 AND kind=$3 AND ref_id=$4""",
                    user_id, str(session_id or ""), row.get("kind"), row.get("ref_id"))
                hits += 1
                # 提取练习（Roediger & Karpicke 2006）：摆出去的东西真被接住了，
                # 等于她想起来并说出口一次——比再读一遍更牢。判据是已有的供给日志，
                # 不再另开一条写入路径（词面命中会低估，已知，档案里写明）。
                if str(row.get("kind") or "") == "world_line":
                    # ref_id 是逗号拼接的多条（一轮可能摆了两条），按单条拆开逐条加固。
                    for _one in str(row.get("ref_id") or "").split(","):
                        _one = _one.strip()
                        if not _one:
                            continue
                        try:
                            await self._boost_life_item(user_id, _one)
                        except Exception:
                            pass
        return hits

    async def _boost_life_item(self, user_id: str, item_id: str) -> None:
        """被真的想起来一次：存储强度 +，记一次 recalls，刷新最后接触时间。"""
        if not item_id:
            return
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)
        owner_uid = user_id
        row = await pool.fetchrow(
            """SELECT strength, created_at, last_recalled_at FROM companion_life_items
               WHERE user_id=$1::uuid AND item_id=$2""", owner_uid, item_id)
        if not row:
            from config import ALICE_EXTERNAL_SHARED_ENABLED, ALICE_SHARED_USER_ID
            if ALICE_EXTERNAL_SHARED_ENABLED and ALICE_SHARED_USER_ID and ALICE_SHARED_USER_ID != str(user_id):
                owner_uid = ALICE_SHARED_USER_ID
                row = await pool.fetchrow(
                    """SELECT strength, created_at, last_recalled_at FROM companion_life_items
                       WHERE user_id=$1::uuid AND item_id=$2""", owner_uid, item_id)
        if not row:
            return
        r_now = _retrieval_strength(
            float(row["strength"] or 1.0),
            row["last_recalled_at"] or row["created_at"], now)
        await pool.execute(
            """UPDATE companion_life_items
               SET strength = LEAST(10.0, strength + $3),
                   recalls = recalls + 1, last_recalled_at = $4
               WHERE user_id=$1::uuid AND item_id=$2""",
            # 回想一次必须比再读一次更值钱（Roediger & Karpicke 2006）：
            # 再读的收益是 0.2 + 1.3·(1-R)，这里给 0.6 + 1.2·(1-R)。
            owner_uid, item_id, 0.6 + 1.2 * (1.0 - r_now), now.isoformat())

    async def supply_stats(self, user_id: str = "", days: int = 7) -> list[dict]:
        """§8.90 读侧：最近 N 天"给了多少 / 命中多少"，按来源分组。"""
        pool = await self._get_pool()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if user_id:
            rows = await pool.fetch(
                """SELECT kind, COUNT(*) AS given,
                          SUM(CASE WHEN used THEN 1 ELSE 0 END) AS used
                   FROM companion_supply_log
                   WHERE user_id=$1::uuid AND created_at > $2
                   GROUP BY kind ORDER BY given DESC""", user_id, cutoff)
        else:
            rows = await pool.fetch(
                """SELECT kind, COUNT(*) AS given,
                          SUM(CASE WHEN used THEN 1 ELSE 0 END) AS used
                   FROM companion_supply_log
                   WHERE created_at > $1
                   GROUP BY kind ORDER BY given DESC""", cutoff)
        return [dict(r) for r in rows]

    @staticmethod
    def _supply_overlap(hint: str, text: str) -> bool:
        import re as _re

        def grams(value: str) -> set:
            out: set = set()
            clean = _re.sub(r"[^一-鿿a-zA-Z ]", "", value or "")
            for token in _re.findall(r"[一-鿿]+", clean):
                out.update(token[i:i + 2] for i in range(len(token) - 1))
            out.update(w.lower() for w in _re.findall(r"[a-zA-Z]{4,}", clean))
            return out

        return bool(grams(hint) & grams(text))

    async def replace_life_items(self, user_id: str, items: list[dict],
                                 days: int = 7) -> int:
        """整批换掉她"这周的事"（§8.79）：旧的作废，新的 7 天有效。"""
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)
        await pool.execute(
            """UPDATE companion_life_items SET status='expired'
               WHERE user_id=$1::uuid AND status='active' AND kind <> 'reading'""", user_id)
        rows = [x for x in (items or []) if isinstance(x, dict) and str(x.get("text") or "").strip()]
        for item in rows[:5]:
            item_id = str(item.get("item_id") or "").strip() or (
                "life_" + str(abs(hash(str(item.get("text")))))[:10])
            await pool.execute(
                """INSERT INTO companion_life_items
                       (user_id, item_id, text, kind, valid_from, valid_to, created_at, aliases)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6, $5, $7)
                   ON CONFLICT (user_id, item_id) DO UPDATE SET
                     text=excluded.text, kind=excluded.kind,
                     valid_to=excluded.valid_to, status='active',
                     aliases=excluded.aliases""",
                user_id, item_id, str(item.get("text") or "")[:160],
                str(item.get("kind") or "")[:40], now.isoformat(),
                (now + timedelta(days=days)).isoformat(),
                str(item.get("aliases") or "")[:120])
        return len(rows[:5])

    async def set_persona_fact_value(self, user_id: str, fact_id: str, value: str,
                                     *, persona_key: str = "alice",
                                     kind: str = "preference",
                                     facet_key: str = "") -> int:
        """直接覆盖一条人格事实的值（先 UPDATE，改不到再 INSERT）。

        为什么需要它：`upsert_companion_persona_facts` 是 `ON CONFLICT DO NOTHING`
        + 固定 fact_id ⇒ 同一条事实**只能写进去一次**，之后永远静默跳过
        （2026-10-26 实测：品味第一次写入成功，之后每次合并都返回 0，
        表现为"沉淀了但对话没变化"）。
        """
        pool = await self._get_pool()
        uid = _as_uuid(user_id)
        val = str(value or "").strip()[:1000]
        if not uid or not fact_id or not val:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        updated = await pool.execute(
            """UPDATE companion_persona_facts
               SET value_text=$4, status='active', valid_to='', recorded_at=$5
               WHERE user_id=$1::uuid AND persona_key=$2 AND fact_id=$3""",
            uid, str(persona_key), str(fact_id), val, now)
        try:
            changed = int(str(updated).split()[-1])
        except Exception:
            changed = 0
        if changed:
            return changed
        await pool.execute(
            """INSERT INTO companion_persona_facts
                   (user_id, persona_key, fact_id, kind, facet_key, value_text,
                    status, source_kind, source_conv_id, source_turn,
                    source_message_ref, evidence_quote, interaction_context,
                    supporting_fact_ids, related_entity_ids, confidence,
                    recorded_at, valid_from, valid_to, hidden_at, observed_at,
                    source_occurrence_id)
               VALUES ($1::uuid, $2, $3, $4, $5, $6, 'active', 'reading', '', 0,
                       '', '', '', '[]'::jsonb, '[]'::jsonb, 0.9, $7, '', '', NULL, $7, $8)
               ON CONFLICT DO NOTHING""",
            uid, str(persona_key), str(fact_id), str(kind), str(facet_key)[:180],
            val, now, str(fact_id))
        return 1

    async def list_active_users(self, days: int = 7, limit: int = 5) -> list[str]:
        """最近 days 天有真实对话的用户（她自己读东西的 sweeper 用）。"""
        pool = await self._get_pool()
        # conversation_messages.created_at 是 timestamptz：必须传 datetime，
        # 传 ISO 字符串会 DataError（2026-10-26 实测）。
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        rows = await pool.fetch(
            """SELECT user_id::text AS uid, max(created_at) AS latest
               FROM conversation_messages
               WHERE created_at > $1 AND role IN ('user', 'assistant')
               GROUP BY user_id ORDER BY latest DESC LIMIT $2""",
            cutoff, max(1, limit))
        return [str(r["uid"]) for r in rows]

    async def append_life_item(self, user_id: str, item: dict, days: int = 3,
                              merge_days: int = 7) -> int:
        """追加一条她的外部经历，按具体来源 ref（bvid/url）独立保存，绝不用宽泛 topic 覆盖另一支视频。"""
        pool = await self._get_pool()
        text = str((item or {}).get("text") or "").strip()
        if not text:
            return 0
        now = datetime.now(timezone.utc)
        ref = str((item or {}).get("ref") or "").strip()[:200]
        item_id = str(item.get("item_id") or "").strip() or (
            ("read_" + hashlib.md5(ref.encode("utf-8")).hexdigest()[:10]) if ref
            else ("read_" + str(abs(hash(text)))[:10])
        )
        topic = str((item or {}).get("topic") or "").strip()[:40]
        title = str((item or {}).get("title") or "").strip()[:160]
        author = str((item or {}).get("author") or "").strip()[:80]
        detail = str((item or {}).get("detail") or "").strip()[:400]
        aliases = str((item or {}).get("aliases") or "").strip()[:240]
        depth = float((item or {}).get("depth") or 1.0)
        raw_actions = (item or {}).get("actions_json") or (item or {}).get("actions") or {}
        if isinstance(raw_actions, dict):
            actions_str = json.dumps(raw_actions, ensure_ascii=False) if raw_actions else "{}"
        else:
            actions_str = str(raw_actions or "{}").strip()[:1200] or "{}"

        # 1) 同一支具体内容（同 ref 非空）重逢：原地加固并补全字段，不丢已有动作记录。
        if ref:
            same_ref = await pool.fetchrow(
                """SELECT item_id, strength, created_at, last_recalled_at
                   FROM companion_life_items
                   WHERE user_id=$1::uuid AND status='active' AND ref=$2
                   ORDER BY created_at DESC LIMIT 1""",
                user_id, ref)
            if same_ref:
                r_before = _retrieval_strength(
                    float(same_ref["strength"] or 1.0),
                    same_ref["last_recalled_at"] or same_ref["created_at"], now)
                gain = (0.2 + 1.3 * (1.0 - r_before)) * depth
                await pool.execute(
                    """UPDATE companion_life_items
                       SET hits = hits + 1,
                           strength = LEAST(10.0, strength + $7),
                           depth = GREATEST(depth, $8),
                           text = $3, aliases = CASE WHEN $4 <> '' THEN $4 ELSE aliases END,
                           created_at = $5, valid_to = $6, status='active',
                           topic = CASE WHEN $9 <> '' THEN $9 ELSE topic END,
                           title = CASE WHEN $10 <> '' THEN $10 ELSE title END,
                           author = CASE WHEN $11 <> '' THEN $11 ELSE author END,
                           detail = CASE WHEN $12 <> '' THEN $12 ELSE detail END,
                           actions_json = CASE WHEN $13 NOT IN ('', '{}') THEN $13 ELSE actions_json END
                       WHERE user_id=$1::uuid AND item_id=$2""",
                    user_id, same_ref["item_id"], text[:200], aliases,
                    now.isoformat(), (now + timedelta(days=days)).isoformat(),
                    gain, depth, topic, title, author, detail, actions_str)
                return 1
            # 不同视频但同领域（topic 相同）：给同领域旧视频做轻微联想唤醒（+0.25 强度），绝不覆写旧视频！
            if topic and merge_days > 0:
                await pool.execute(
                    """UPDATE companion_life_items
                       SET strength = LEAST(10.0, strength + 0.25)
                       WHERE user_id=$1::uuid AND status='active' AND topic=$2
                         AND ref <> $3 AND created_at > $4""",
                    user_id, topic, ref, (now - timedelta(days=merge_days)).isoformat())
        elif topic and merge_days > 0:
            # 无外部 ref 的泛感受条目才按同 topic 合并。
            prior = await pool.fetchrow(
                """SELECT item_id, strength, created_at, last_recalled_at
                   FROM companion_life_items
                   WHERE user_id=$1::uuid AND status='active' AND topic=$2
                     AND ref='' AND created_at > $3
                   ORDER BY created_at DESC LIMIT 1""",
                user_id, topic, (now - timedelta(days=merge_days)).isoformat())
            if prior:
                r_before = _retrieval_strength(
                    float(prior["strength"] or 1.0),
                    prior["last_recalled_at"] or prior["created_at"], now)
                gain = (0.2 + 1.3 * (1.0 - r_before)) * depth
                await pool.execute(
                    """UPDATE companion_life_items
                       SET hits = hits + 1,
                           strength = LEAST(10.0, strength + $7),
                           text = $3, aliases = $4,
                           created_at = $5, valid_to = $6, status='active'
                       WHERE user_id=$1::uuid AND item_id=$2""",
                    user_id, prior["item_id"], text[:200], aliases,
                    now.isoformat(), (now + timedelta(days=days)).isoformat(), gain)
                return 1

        await pool.execute(
            """INSERT INTO companion_life_items
                   (user_id, item_id, text, kind, aliases, valid_from, valid_to,
                    status, source, created_at, topic, hits, strength, depth,
                    ref, title, author, detail, actions_json)
               VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, 'active', $8, $6, $9, 1,
                       $10, $10, $11, $12, $13, $14, $15)
               ON CONFLICT (user_id, item_id) DO UPDATE SET
                 text=excluded.text,
                 aliases=CASE WHEN excluded.aliases <> '' THEN excluded.aliases ELSE companion_life_items.aliases END,
                 valid_to=excluded.valid_to, status='active',
                 topic=CASE WHEN excluded.topic <> '' THEN excluded.topic ELSE companion_life_items.topic END,
                 ref=CASE WHEN excluded.ref <> '' THEN excluded.ref ELSE companion_life_items.ref END,
                 title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE companion_life_items.title END,
                 author=CASE WHEN excluded.author <> '' THEN excluded.author ELSE companion_life_items.author END,
                 detail=CASE WHEN excluded.detail <> '' THEN excluded.detail ELSE companion_life_items.detail END,
                 actions_json=CASE WHEN excluded.actions_json NOT IN ('', '{}') THEN excluded.actions_json ELSE companion_life_items.actions_json END""",
            user_id, item_id, text[:200], str(item.get("kind") or "reading")[:40],
            aliases, now.isoformat(), (now + timedelta(days=days)).isoformat(),
            str(item.get("source") or "reading")[:20], topic, depth,
            ref, title, author, detail, actions_str)
        return 1

    async def record_life_action(
        self, user_id: str, *, ref: str = "", item_id: str = "",
        actions: dict | None = None, comment: str = "",
        author: str = "", title: str = "",
    ) -> int:
        """把真实外部动作（点赞/投币/收藏/关注UP主/发评论）绑定回原经历行，绝不另覆盖成残句。"""
        pool = await self._get_pool()
        uid = _as_uuid(user_id)
        ref_clean = str(ref or "").strip()[:200]
        iid_clean = str(item_id or "").strip()
        row = None
        if iid_clean:
            row = await pool.fetchrow(
                """SELECT item_id, aliases, actions_json, author, title
                   FROM companion_life_items
                   WHERE user_id=$1::uuid AND item_id=$2""", uid, iid_clean)
        if not row and ref_clean:
            ref_base = ref_clean.split("?", 1)[0].split("#", 1)[0].rstrip("/")
            bv_match = re.search(r"(BV[0-9A-Za-z]{8,14})", ref_clean)
            bv_tok = bv_match.group(1) if bv_match else ""
            row = await pool.fetchrow(
                """SELECT item_id, aliases, actions_json, author, title
                   FROM companion_life_items
                   WHERE user_id=$1::uuid
                     AND (ref=$2 OR ref=$3 OR ($4 <> '' AND ref LIKE '%' || $4 || '%'))
                   ORDER BY created_at DESC LIMIT 1""",
                uid, ref_clean, ref_base, bv_tok)
        if not row:
            return 0
        try:
            merged = json.loads(row["actions_json"] or "{}")
            if not isinstance(merged, dict):
                merged = {}
        except Exception:
            merged = {}
        act = actions or {}
        for flag in ("like", "coin", "fav", "follow"):
            if act.get(flag):
                merged[flag] = True
        if str(act.get("why") or "").strip():
            merged["why"] = str(act.get("why")).strip()[:120]
        c_text = str(comment or "").strip()[:200]
        if c_text:
            comments = [str(x) for x in (merged.get("comments") or []) if str(x).strip()]
            if c_text not in comments:
                comments.append(c_text)
            merged["comments"] = comments[-5:]

        alias_parts = [str(row["aliases"] or "").strip()]
        eff_author = str(author or row["author"] or "").strip()[:80]
        eff_title = str(title or row["title"] or "").strip()[:160]
        if eff_author and eff_author not in alias_parts[0]:
            alias_parts.append(eff_author)
        for flag, kw in (
            ("follow", "关注 UP主 follow"),
            ("fav", "收藏 bookmark saved"),
            ("coin", "投币 coin"),
            ("like", "点赞 liked"),
        ):
            if merged.get(flag) and kw.split()[0] not in alias_parts[0]:
                alias_parts.append(kw)
        if merged.get("comments") and "评论" not in alias_parts[0]:
            alias_parts.append("评论 留言 comment")
        new_aliases = " ".join(p for p in alias_parts if p).strip()[:240]

        heavy = bool(c_text or merged.get("coin") or merged.get("fav") or merged.get("follow"))
        boost = 0.85 if heavy else 0.4
        min_depth = 1.35 if heavy else 1.15
        now_iso = datetime.now(timezone.utc).isoformat()
        await pool.execute(
            """UPDATE companion_life_items
               SET actions_json = $3,
                   aliases = $4,
                   author = CASE WHEN $5 <> '' THEN $5 ELSE author END,
                   title = CASE WHEN $6 <> '' THEN $6 ELSE title END,
                   strength = LEAST(10.0, strength + $7),
                   depth = GREATEST(depth, $8),
                   last_recalled_at = $9
               WHERE user_id=$1::uuid AND item_id=$2""",
            uid, row["item_id"], json.dumps(merged, ensure_ascii=False),
            new_aliases, eff_author, eff_title, boost, min_depth, now_iso)
        return 1

    @contextlib.asynccontextmanager
    async def reading_lock(self, user_id: str):
        """同一个人同一时刻只允许一次阅读。

        后台 sweeper 每 30 分钟扫一次，手动补跑是另起进程，两边可能撞上：
        会写两条生活记录，而且口味是"读-改-写"的单个字段，后写的会覆盖前一次的合并。
        PostgreSQL 咨询锁按连接生效，所以整段都占着同一条连接。
        yield True = 拿到锁；False = 别人正在跑，这一轮直接放弃。
        """
        pool = await self._get_pool()
        digest = hashlib.sha1(("reading:" + str(user_id)).encode("utf-8")).digest()
        key = int.from_bytes(digest[:8], "big", signed=True)
        async with pool.acquire() as conn:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
            try:
                yield bool(got)
            finally:
                if got:
                    await conn.execute("SELECT pg_advisory_unlock($1)", key)

    async def has_fresh_life_item(self, user_id: str, source: str, hours: int = 20) -> bool:
        """这个用户最近 hours 小时内有没有来自 source 的条目（防一天重复读）。"""
        pool = await self._get_pool()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = await pool.fetchval(
            """SELECT count(*) FROM companion_life_items
               WHERE user_id=$1::uuid AND source=$2 AND created_at > $3""",
            user_id, str(source), cutoff)
        return bool(row)

    async def list_life_items(self, user_id: str, limit: int = 3,
                              shared_user_id: str = "") -> list[dict]:
        pool = await self._get_pool()
        uid = _as_uuid(user_id)
        shared_uid = _as_uuid(shared_user_id) if str(shared_user_id or "").strip() else ""
        now = datetime.now(timezone.utc).isoformat()
        pool_limit = max(1, limit) * 4
        # 候选池按此刻提取强度排序；支持同时合并「通用爱丽丝」共享外部经历池（单次查询零额外往返）。
        if shared_uid and shared_uid != uid:
            rows = await pool.fetch(
                """SELECT user_id::text AS owner_id, item_id, text, kind, source, valid_to,
                          aliases, topic, hits, strength, depth, last_recalled_at,
                          created_at, ref, title, author, detail, actions_json
                   FROM companion_life_items
                   WHERE user_id = ANY($1::uuid[]) AND status='active' AND valid_to > $2
                   ORDER BY (user_id = $3::uuid AND kind <> 'reading') DESC,
                            (kind <> 'reading') DESC,
                            strength * power(
                       1.0 + (EXTRACT(EPOCH FROM (now() - COALESCE(NULLIF(last_recalled_at,''), created_at)::timestamptz)) / 86400.0)
                             / GREATEST(strength * GREATEST(depth, 0.5), 0.05), -0.7) DESC
                   LIMIT $4""",
                [uid, shared_uid], now, uid, pool_limit * 2)
            out: list[dict] = []
            seen_keys: set[str] = set()
            # 用户专属条目优先占坑，共享外部经历自动补齐。
            ordered = sorted(
                (dict(r) for r in rows),
                key=lambda r: 0 if str(r.get("owner_id") or "") == uid else 1,
            )
            # 保持按提取强度的原始顺序同时去重同 ref / item_id
            preferred_owners: dict[str, str] = {}
            for r in ordered:
                dedupe_k = f"ref:{r['ref']}" if str(r.get("ref") or "").strip() else f"id:{r['item_id']}"
                preferred_owners.setdefault(dedupe_k, str(r.get("owner_id") or ""))
            for r in rows:
                d = dict(r)
                dedupe_k = f"ref:{d['ref']}" if str(d.get("ref") or "").strip() else f"id:{d['item_id']}"
                if dedupe_k in seen_keys or preferred_owners.get(dedupe_k) != str(d.get("owner_id") or ""):
                    continue
                seen_keys.add(dedupe_k)
                d.pop("owner_id", None)
                out.append(d)
                if len(out) >= pool_limit:
                    break
            return out
        rows = await pool.fetch(
            """SELECT item_id, text, kind, source, valid_to, aliases, topic, hits,
                      strength, depth, last_recalled_at, created_at, ref,
                      title, author, detail, actions_json
               FROM companion_life_items
               WHERE user_id=$1::uuid AND status='active' AND valid_to > $2
               ORDER BY (kind <> 'reading') DESC,
                        strength * power(
                   1.0 + (EXTRACT(EPOCH FROM (now() - COALESCE(NULLIF(last_recalled_at,''), created_at)::timestamptz)) / 86400.0)
                         / GREATEST(strength * GREATEST(depth, 0.5), 0.05), -0.7) DESC
               LIMIT $3""",
            uid, now, pool_limit)
        return [dict(r) for r in rows]

    async def list_growth_facts_for_merge(self, user_id: str,
                                          limit: int = 200) -> list[dict]:
        """写入侧专用：连 candidate 一起给。

        `list_companion_persona_facts` 默认只返回 active（candidate 对 prompt 不可见，
        这是对的）；但"第二次同类观察"要能找到第一条候选，否则合并永远不成立。
        """
        pool = await self._get_pool()
        rows = await pool.fetch(
            """SELECT fact_id, kind, facet_key, value_text, status, support_count,
                      recorded_at, source_conv_id
               FROM companion_persona_facts
               WHERE user_id=$1::uuid AND persona_key='alice'
                 AND hidden_at IS NULL AND valid_to=''
                 AND status IN ('active','candidate')
                 AND facet_key LIKE 'growth:%'
               ORDER BY recorded_at DESC LIMIT $2""", user_id, max(1, min(400, int(limit))))
        return [dict(r) for r in rows]

    async def bump_persona_support(self, user_id: str, fact_id: str) -> None:
        """同一条人格事实又被观察到一次。"""
        pool = await self._get_pool()
        await pool.execute(
            """UPDATE companion_persona_facts
               SET support_count = support_count + 1,
                   status = 'active'
               WHERE user_id=$1::uuid AND fact_id=$2""", user_id, fact_id)

    async def list_life_topics(self, user_id: str, limit: int = 20) -> list[str]:
        """她已经有的领域标签。写新记忆时让她尽量复用，否则每轮造一个新词。"""
        pool = await self._get_pool()
        rows = await pool.fetch(
            """SELECT topic, count(*) AS n FROM companion_life_items
               WHERE user_id=$1::uuid AND topic <> ''
               GROUP BY topic ORDER BY n DESC LIMIT $2""", user_id, max(1, limit))
        return [str(r["topic"]) for r in rows]

    async def upsert_open_question(self, user_id: str, question: dict,
                                   source_conv_id: str = "") -> None:
        """写一条"她问过、他没答"的问题；同用户只保留最新三条。"""
        pool = await self._get_pool()
        text = str(question.get("text") or "").strip()[:160]
        qid = str(question.get("question_id") or "").strip()
        if not text or not qid:
            return
        now = datetime.now(timezone.utc).isoformat()
        await pool.execute(
            """INSERT INTO companion_open_questions
                   (user_id, question_id, text, topic, source_conv_id, asked_at, created_at)
               VALUES ($1::uuid, $2, $3, $4, $5, $6, $6)
               ON CONFLICT (user_id, question_id) DO NOTHING""",
            user_id, qid, text, str(question.get("topic") or "")[:80],
            source_conv_id or None, now)
        await pool.execute(
            """UPDATE companion_open_questions SET status='expired'
               WHERE user_id=$1::uuid AND status='pending' AND question_id NOT IN (
                   SELECT question_id FROM companion_open_questions
                   WHERE user_id=$1::uuid AND status='pending'
                   ORDER BY asked_at DESC LIMIT 3)""", user_id)

    async def list_open_questions(self, user_id: str, limit: int = 3) -> list[dict]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            """SELECT question_id, text, topic, asked_at FROM companion_open_questions
               WHERE user_id=$1::uuid AND status='pending'
               ORDER BY asked_at DESC LIMIT $2""", user_id, max(1, limit))
        return [dict(r) for r in rows]

    async def expire_open_questions(self, user_id: str, days: int = 3) -> int:
        pool = await self._get_pool()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await pool.execute(
            """UPDATE companion_open_questions SET status='expired'
               WHERE user_id=$1::uuid AND status='pending' AND asked_at < $2""",
            user_id, cutoff)
        return 0

    async def mark_question_shown(self, user_id: str, question_id: str) -> None:
        pool = await self._get_pool()
        await pool.execute(
            """UPDATE companion_open_questions
               SET shown_at=$3, status='asked'
               WHERE user_id=$1::uuid AND question_id=$2 AND status='pending'""",
            user_id, question_id, datetime.now(timezone.utc).isoformat())


    async def demote_companion_persona_facts(
            self, user_id: str, fact_ids: list[str], reason: str = "") -> int:
        """把指定事实降级（§8.91c 回音复核用）：status=superseded，不删除。"""
        ids = [str(x) for x in (fact_ids or []) if str(x).strip()]
        if not ids:
            return 0
        pool = await self._get_pool()
        now = datetime.now(timezone.utc).isoformat()
        result = await pool.execute(
            """UPDATE companion_persona_facts
               SET status='superseded', valid_to=$3,
                   supersedes = CASE WHEN supersedes = '' THEN $4 ELSE supersedes END
               WHERE user_id=$1::uuid AND fact_id=ANY($2::text[]) AND status='active'""",
            user_id, ids, now, str(reason or '')[:180])
        return int(str(result).split()[-1] or 0)

    async def list_companion_persona_facts(
        self, user_id: str, query: str = "", limit: int = 48,
    ) -> list[dict]:
        from memory.companion_persona import query_terms, wants_persona_history
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        params: list = [uid]
        include_history = wants_persona_history(query)
        status_clause = "status IN ('active','superseded')" if include_history else "status='active' AND valid_to=''"
        where = f"user_id=$1::uuid AND persona_key='alice' AND {status_clause} AND hidden_at IS NULL"
        pieces = []
        terms = sorted(query_terms(query))
        for term in (terms if not include_history else []):
            params.extend((f"%{term}%", f"%{term}%"))
            value_pos, facet_pos = len(params) - 1, len(params)
            pieces.append("(value_text ILIKE $" + str(value_pos) + " OR facet_key ILIKE $" + str(facet_pos) + ")")
        if pieces:
            where += " AND (" + " OR ".join(pieces) + ")"
        params.append(max(1, min(200, int(limit or 48))))
        limit_pos = len(params)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM companion_persona_facts WHERE " + where +
                " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
                "CASE kind WHEN 'identity' THEN 0 ELSE 1 END, recorded_at DESC, fact_id LIMIT $" +
                str(limit_pos),
                *params,
            )
        return [self._companion_persona_row(row) for row in rows]

    async def get_companion_persona_snapshot(self, user_id: str) -> dict:
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM companion_persona_snapshots "
                "WHERE user_id=$1::uuid AND persona_key='alice'", uid,
            )
        return self._companion_persona_row(row)

    async def save_companion_persona_snapshot(
        self, user_id: str, snapshot: dict,
    ) -> bool:
        from engine.chunk_evidence import loads_json_list
        ids = list(dict.fromkeys(
            str(value) for value in loads_json_list((snapshot or {}).get("supporting_fact_ids"))
            if str(value)
        ))
        narrative = str((snapshot or {}).get("narrative") or "").strip()[:600]
        if not narrative or not ids:
            return False
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM companion_persona_facts
                   WHERE user_id=$1::uuid AND persona_key='alice'
                     AND status IN ('active','superseded') AND hidden_at IS NULL
                     AND fact_id = ANY($2::text[])""",
                uid, ids,
            )
            if int(count or 0) != len(ids):
                return False
            await conn.execute(
                """INSERT INTO companion_persona_snapshots
                       (user_id, persona_key, narrative, supporting_fact_ids,
                        snapshot_version, source_hash, generated_at)
                   VALUES ($1::uuid, 'alice', $2, $3::jsonb, $4, $5, NOW())
                   ON CONFLICT (user_id, persona_key) DO UPDATE SET
                       narrative=EXCLUDED.narrative,
                       supporting_fact_ids=EXCLUDED.supporting_fact_ids,
                       snapshot_version=EXCLUDED.snapshot_version,
                       source_hash=EXCLUDED.source_hash,
                       generated_at=EXCLUDED.generated_at""",
                uid, narrative, json.dumps(ids, ensure_ascii=False),
                str(snapshot.get("snapshot_version") or "")[:80],
                str(snapshot.get("source_hash") or "")[:80],
            )
        return True

    async def invalidate_companion_persona_snapshot(self, user_id: str) -> None:
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        await pool.execute(
            """UPDATE companion_persona_snapshots
               SET narrative='', supporting_fact_ids='[]'::jsonb, snapshot_version='',
                   source_hash='', generated_at=NULL
               WHERE user_id=$1::uuid AND persona_key='alice'""", uid,
        )

    async def set_companion_persona_source_hidden(
        self, user_id: str, conv_id: str, hidden: bool,
    ) -> int:
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE companion_persona_facts
                   SET hidden_at=CASE WHEN $3 THEN NOW() ELSE NULL END
                   WHERE user_id=$1::uuid AND source_conv_id=$2""", uid, conv_id, bool(hidden),
            )
            await conn.execute(
                """UPDATE companion_persona_snapshots
                   SET narrative='', supporting_fact_ids='[]'::jsonb,
                       snapshot_version='', source_hash='', generated_at=NULL
                   WHERE user_id=$1::uuid AND persona_key='alice'""", uid,
            )
        return int(str(result).split()[-1])

    async def purge_companion_persona_source(self, user_id: str, conv_id: str) -> int:
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM companion_persona_facts WHERE user_id=$1::uuid AND source_conv_id=$2",
                uid, conv_id,
            )
            await conn.execute(
                """UPDATE companion_persona_snapshots
                   SET narrative='', supporting_fact_ids='[]'::jsonb,
                       snapshot_version='', source_hash='', generated_at=NULL
                   WHERE user_id=$1::uuid AND persona_key='alice'""", uid,
            )
        return int(str(result).split()[-1])

    # ── 统一学习者画像（plans/0923-04 阶段 1）────────────────────────────────
    # ⚠ 方法面（名字、参数、返回键）**必须**与 l2_sqlite 逐一同形。
    # evidence_refs / supporting_fact_ids 是 jsonb，读回来一律经 loads_json_list；
    # asyncpg 对 jsonb 返回 str，`isinstance(col, list)` 在本后端**永假**。

    async def upsert_learner_profile_facts(self, user_id: str, facts: list[dict]) -> int:
        """幂等投影：同 (user_id, fact_key, fact_subkey, source_occurrence_id) 不新增版本。"""
        if not facts:
            return 0
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        written = 0
        async with pool.acquire() as conn:
            for fact in facts:
                if not isinstance(fact, dict) or not fact.get("fact_key"):
                    continue
                result = await conn.execute(
                    """INSERT INTO learner_profile_facts (
                           fact_id, user_id, dimension, fact_key, fact_subkey, value_text,
                           source_pipeline, source_table, source_id, source_anchor_id,
                           source_occurrence_id, extractor_version, source_conv_id,
                           source_session_id, evidence_quote, evidence_refs, evidence_kind,
                           confidence, observed_at, valid_from, valid_to, recorded_at,
                           time_basis, expires_at, status
                       ) VALUES ($1,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                                 $16::jsonb,$17,$18,$19,$20,$21,$22,$23,$24,$25)
                       ON CONFLICT (user_id, fact_key, fact_subkey, source_occurrence_id)
                       DO NOTHING""",
                    str(fact.get("fact_id") or ""),
                    uid,
                    fact.get("dimension"),
                    str(fact.get("fact_key") or ""),
                    str(fact.get("fact_subkey") or ""),
                    str(fact.get("value_text") or ""),
                    str(fact.get("source_pipeline") or ""),
                    str(fact.get("source_table") or ""),
                    str(fact.get("source_id") or ""),
                    str(fact.get("source_anchor_id") or ""),
                    str(fact.get("source_occurrence_id") or ""),
                    str(fact.get("extractor_version") or ""),
                    fact.get("source_conv_id"),
                    fact.get("source_session_id"),
                    str(fact.get("evidence_quote") or ""),
                    json.dumps(list(fact.get("evidence_refs") or []), ensure_ascii=False),
                    str(fact.get("evidence_kind") or "unknown"),
                    fact.get("confidence"),
                    _lp_parse_dt(fact.get("observed_at")),
                    _lp_parse_dt(fact.get("valid_from")),
                    _lp_parse_dt(fact.get("valid_to")),
                    _lp_parse_dt(fact.get("recorded_at")) or datetime.now(timezone.utc),
                    str(fact.get("time_basis") or "unknown"),
                    _lp_parse_dt(fact.get("expires_at")),
                    str(fact.get("status") or "candidate"),
                )
                written += int(str(result).split()[-1])
        return written

    async def list_learner_profile_facts(
        self, user_id: str, *, statuses: Optional[tuple] = None
    ) -> list[dict]:
        """读该用户全部画像事实行（**含历史版本**，按证据时间升序稳定排序）。"""
        from engine.chunk_evidence import loads_json_list
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        sql = "SELECT * FROM learner_profile_facts WHERE user_id = $1::uuid"
        if statuses:
            sql += " AND status = ANY($2::text[])"
        sql += " ORDER BY COALESCE(observed_at, TIMESTAMPTZ 'epoch') ASC, recorded_at ASC, fact_id ASC"
        async with pool.acquire() as conn:
            if statuses:
                rows = await conn.fetch(sql, uid, list(statuses))
            else:
                rows = await conn.fetch(sql, uid)
        out = []
        for row in rows:
            item = self._jsonable(dict(row))
            item["evidence_refs"] = loads_json_list(item.get("evidence_refs"))
            out.append(item)
        return out

    async def put_learner_profile_narrative(
        self, user_id: str, dimension: str, text: str,
        supporting_fact_ids: Optional[list] = None,
    ) -> None:
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        await pool.execute(
            """INSERT INTO learner_profile_narratives
                   (user_id, dimension, text, supporting_fact_ids, updated_at)
               VALUES ($1::uuid, $2, $3, $4::jsonb, NOW())
               ON CONFLICT (user_id, dimension) DO UPDATE SET
                   text=EXCLUDED.text,
                   supporting_fact_ids=EXCLUDED.supporting_fact_ids,
                   updated_at=NOW()""",
            uid, str(dimension or ""), str(text or ""),
            json.dumps(list(supporting_fact_ids or []), ensure_ascii=False),
        )

    async def list_learner_profile_narratives(self, user_id: str) -> list[dict]:
        from engine.chunk_evidence import loads_json_list
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM learner_profile_narratives WHERE user_id = $1::uuid"
                " ORDER BY dimension ASC",
                uid,
            )
        out = []
        for row in rows:
            item = self._jsonable(dict(row))
            item["supporting_fact_ids"] = loads_json_list(item.get("supporting_fact_ids"))
            out.append(item)
        return out

    # ── 写入租约（plans/0923-04 §4.4 阶段 2「旧写手写入闸门」）─────────────────
    # ⚠ 方法面必须与 l2_sqlite 逐一同形（名字、参数名、返回类型）。
    # 表只存控制记录，不存原文/画像值；因此**不在 `_USER_TABLES` 里**（不导出），
    # 但全删路径必须撤销 + 物理清空。

    async def register_write_lease(self, user_id: str, token: str,
                                   scope_conv_id: str = "") -> bool:
        """登记一条写入令牌（幂等）。返回是否登记成功。

        顺带清掉该用户超过 `WRITE_LEASE_TTL_HOURS` 的旧行，避免表无限增长
        ——这样不需要额外加一个调度槽。清的是**早已结束**的作业的令牌，
        在跑的作业（秒级）不会被误伤。
        """
        token = str(token or "").strip()
        if not token:
            return False
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 与全删的 revoke 串行：同一用户同一把事务级 advisory lock
                # （§4.4「写入与全删在 PG 用同一用户 advisory lock」）。
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1::text)::bigint)", str(uid))
                await conn.execute(
                    "DELETE FROM learner_profile_write_leases "
                    "WHERE user_id = $1::uuid AND created_at < NOW() - make_interval(hours => $2::int)",
                    uid, int(WRITE_LEASE_TTL_HOURS),
                )
                await conn.execute(
                    """INSERT INTO learner_profile_write_leases
                           (user_id, token, scope_conv_id, status, created_at)
                       VALUES ($1::uuid, $2, $3, 'active', NOW())
                       ON CONFLICT (user_id, token) DO UPDATE SET
                           status = 'active',
                           scope_conv_id = EXCLUDED.scope_conv_id,
                           created_at = NOW()""",
                    uid, token, str(scope_conv_id or ""),
                )
        return True

    async def write_lease_active(self, user_id: str, tokens: list[str]) -> bool:
        """全部令牌都仍是本用户的 active 租约才算通过（缺一即拒）。

        **表缺失 / 查询异常时返回 True（fail-open）**：迁移未就绪的老库里
        闸门本就不存在，此时拒写会让所有正常写入一起失效 —— 这属于
        §4.5「新读口异常时整盘回旧路径」，不是"删除后仍放行"。反之，只要
        全删真的执行过（它必须先撤销并最终清空令牌行），这里就必然为 False。
        """
        wanted = sorted({str(t).strip() for t in (tokens or []) if str(t or "").strip()})
        if not wanted:
            return False
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        try:
            row = await pool.fetchrow(
                """SELECT COUNT(*) AS n FROM learner_profile_write_leases
                    WHERE user_id = $1::uuid AND status = 'active'
                      AND token = ANY($2::text[])""",
                uid, wanted,
            )
        except Exception:
            return True
        return int((row["n"] if row else 0) or 0) == len(wanted)

    async def revoke_write_leases(self, user_id: str, scope_conv_id: str = "") -> int:
        """把令牌置为 revoked（闸门立刻关闭）。`scope_conv_id` 为空 = 该用户全部。

        **不删行**：全删流程里这一步先跑，后续任何一步失败都不再清空令牌 ⇒
        闸门保持"拒写"状态等修好重试（§4.5）。
        """
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1::text)::bigint)", str(uid))
                scope = str(scope_conv_id or "")
                if scope:
                    result = await conn.execute(
                        "UPDATE learner_profile_write_leases SET status = 'revoked' "
                        "WHERE user_id = $1::uuid AND status = 'active' AND scope_conv_id = $2",
                        uid, scope,
                    )
                else:
                    result = await conn.execute(
                        "UPDATE learner_profile_write_leases SET status = 'revoked' "
                        "WHERE user_id = $1::uuid AND status = 'active'",
                        uid,
                    )
        return int(str(result).split()[-1])

    async def clear_write_leases(self, user_id: str) -> int:
        """物理清空该用户的令牌行（全删的**最后**一步）。幂等。"""
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1::text)::bigint)", str(uid))
                result = await conn.execute(
                    "DELETE FROM learner_profile_write_leases WHERE user_id = $1::uuid", uid,
                )
        return int(str(result).split()[-1])

    # ── forget 控制行（plans/0923-04 §4.3，阶段 2 补全）───────────────────────
    # ⚠ 方法面必须与 l2_sqlite 逐一同形。控制行与画像事实**同表**（status='forget'），
    # 不含原话与值：只存 fact_key / fact_subkey / 旧来源锚点。

    async def put_learner_profile_forget(self, user_id: str, fact_key: str,
                                         fact_subkey: str = "",
                                         source_anchor_id: str = "",
                                         evidence_refs: Optional[list] = None) -> None:
        """写一条 forget 控制行（幂等）。

        source_occurrence_id 用 `forget:<fact_key>` 当管道名派生 ⇒ 重复 forget
        同一 (键, 子键, 锚点) 落在同一行，不膨胀。行内 value/quote 恒空；
        evidence_refs 只留**被删行的来源引用 ID**（方案 §4.3 允许「旧来源锚点/
        消息 ID」）——供召回按被忘引用过滤，不含原话与值。
        """
        uid = _as_uuid(user_id)
        now = datetime.now(timezone.utc)
        occurrence = _learner_profile.source_occurrence_id(
            f"forget:{str(fact_key or '')}", str(source_anchor_id or ""),
            str(fact_subkey or ""),
        )
        refs_json = json.dumps(
            [str(r) for r in (evidence_refs or []) if str(r or "").strip()][:32],
            ensure_ascii=False)
        pool = await self._get_pool()
        await pool.execute(
            """INSERT INTO learner_profile_facts (
                   fact_id, user_id, dimension, fact_key, fact_subkey, value_text,
                   source_pipeline, source_table, source_id, source_anchor_id,
                   source_occurrence_id, extractor_version, source_conv_id,
                   source_session_id, evidence_quote, evidence_refs, evidence_kind,
                   confidence, observed_at, valid_from, valid_to, recorded_at,
                   time_basis, expires_at, status
               ) VALUES ($1,$2::uuid,NULL,$3,$4,'','forget','','',$5,$6,'v1',
                         NULL,NULL,'',$7::jsonb,'control',
                         NULL,$8,$8,NULL,$8,'unknown',NULL,'forget')
               ON CONFLICT (user_id, fact_key, fact_subkey, source_occurrence_id)
               DO NOTHING""",
            uuid.uuid4().hex, uid,
            str(fact_key or ""), str(fact_subkey or ""),
            str(source_anchor_id or ""), occurrence,
            refs_json,
            now,
        )

    async def list_learner_profile_forgets(self, user_id: str) -> list[dict]:
        """该用户全部 forget 控制行（投影抑制用；**不含原话与值**）。"""
        from engine.chunk_evidence import loads_json_list
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM learner_profile_facts "
                "WHERE user_id = $1::uuid AND status = 'forget' "
                "ORDER BY recorded_at ASC, fact_id ASC",
                uid,
            )
        out = []
        for row in rows:
            item = self._jsonable(dict(row))
            item["evidence_refs"] = loads_json_list(item.get("evidence_refs"))
            out.append(item)
        return out

    async def delete_learner_profile_facts(self, user_id: str, fact_key: str,
                                           fact_subkey: str = "") -> int:
        """物理删某逻辑事实的**全部版本**（不含 forget 控制行）。返回删除行数。"""
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        sub = str(fact_subkey or "")
        if sub:
            result = await pool.execute(
                "DELETE FROM learner_profile_facts "
                "WHERE user_id = $1::uuid AND fact_key = $2 AND fact_subkey = $3 "
                "AND status != 'forget'",
                uid, str(fact_key or ""), sub,
            )
        else:
            result = await pool.execute(
                "DELETE FROM learner_profile_facts "
                "WHERE user_id = $1::uuid AND fact_key = $2 AND status != 'forget'",
                uid, str(fact_key or ""),
            )
        return int(str(result).split()[-1])

    async def list_profile_sweep_users(self) -> list[str]:
        """需要画像补偿投影的用户（有关系记忆行或已有画像行的用户，去重升序）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id FROM relationship_memory "
                "UNION SELECT user_id FROM learner_profile_facts ORDER BY user_id ASC"
            )
        return [str(row["user_id"]) for row in rows if row["user_id"]]

    async def upsert_relationship_memory(self, user_id: str, updates: dict) -> None:
        user_id = _as_uuid(user_id)
        current = await self.get_relationship_memory(user_id)
        merged = {**current, **{k: v for k, v in updates.items() if v is not None}}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO relationship_memory
                   (user_id, last_topic, open_loop, recent_mood, preferred_tone,
                    engagement_topics, avoid_patterns, last_user_message,
                    last_assistant_response, updated_at)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, NOW())
                   ON CONFLICT(user_id) DO UPDATE SET
                     last_topic=EXCLUDED.last_topic,
                     open_loop=EXCLUDED.open_loop,
                     recent_mood=EXCLUDED.recent_mood,
                     preferred_tone=EXCLUDED.preferred_tone,
                     engagement_topics=EXCLUDED.engagement_topics,
                     avoid_patterns=EXCLUDED.avoid_patterns,
                     last_user_message=EXCLUDED.last_user_message,
                     last_assistant_response=EXCLUDED.last_assistant_response,
                     updated_at=NOW()""",
                user_id,
                merged.get("last_topic", ""),
                merged.get("open_loop", ""),
                merged.get("recent_mood", ""),
                merged.get("preferred_tone", ""),
                json.dumps(merged.get("engagement_topics", []), ensure_ascii=False),
                json.dumps(merged.get("avoid_patterns", []), ensure_ascii=False),
                merged.get("last_user_message", ""),
                merged.get("last_assistant_response", ""),
            )

    async def get_learning_progress(self, user_id: str, limit: int = 50) -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT item_type, mastery_probability, detected_count, immediate_success,
                          transfer_success, delayed_success, failed_attempts, next_review_at, updated_at
                   FROM learning_item_states WHERE user_id = $1::uuid
                   ORDER BY mastery_probability ASC, updated_at DESC LIMIT $2""",
                user_id, limit,
            )
            return [dict(row) for row in rows]

    async def record_speech_assessment(self, user_id: str, session_id: str, result: dict) -> None:
        user_id, session_id = _as_uuid(user_id), _as_uuid(session_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO speech_assessments
                   (user_id, session_id, transcript, duration_seconds, word_count, pause_count,
                    words_per_minute, fluency_score, pronunciation_accuracy, prosody_score, assessment_source)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)""",
                user_id, session_id, result.get("transcript", ""), result.get("duration_seconds"),
                result.get("word_count"), result.get("pause_count"), result.get("words_per_minute"),
                result.get("fluency_score"), result.get("pronunciation_accuracy"), result.get("prosody_score"),
                json.dumps(result.get("assessment_source", {}), ensure_ascii=False),
            )

    async def get_speech_assessments(self, user_id: str, limit: int = 20) -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM speech_assessments WHERE user_id = $1::uuid ORDER BY created_at DESC LIMIT $2",
                user_id, limit,
            )
            return [dict(row) for row in rows]

    async def get_prompt_patches(self, user_id: str, status: str = "candidate") -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM prompt_patches WHERE user_id=$1::uuid AND status=$2 ORDER BY created_at DESC LIMIT 5",
                user_id, status,
            )
            return [dict(row) for row in rows]

    async def save_prompt_patch(self, patch: dict) -> str:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO prompt_patches (user_id, patch_id, audit_recommendation_id, root_cause,
                   action, layer, from_value, to_value, reason, status, verified_sessions, max_retries)
                   VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                   ON CONFLICT(patch_id) DO NOTHING""",
                _as_uuid(patch["user_id"]), patch["patch_id"], patch.get("audit_recommendation_id", ""),
                patch.get("root_cause", ""), patch["action"], patch["layer"], patch.get("from_value", ""),
                patch.get("to_value", ""), patch.get("reason", ""), patch.get("status", "candidate"),
                patch.get("verified_sessions", 0), patch.get("max_retries", 3),
            )
        return patch["patch_id"]

    async def batch_save_prompt_patches(self, patches: list[dict]) -> int:
        for patch in patches:
            await self.save_prompt_patch(patch)
        return len(patches)

    async def start_prompt_patch_experiment(self, user_id: str, patch_id: str) -> bool:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE prompt_patches SET status='experiment', applied_at=NOW()
                   WHERE user_id=$1::uuid AND patch_id=$2 AND status='candidate'""", user_id, patch_id)
            return result == "UPDATE 1"

    async def update_patch_status(self, patch_id: str, status: str, verified_sessions: int = None) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE prompt_patches SET status=$2, verified_sessions=COALESCE($3, verified_sessions) WHERE patch_id=$1",
                patch_id, status, verified_sessions)
            return result == "UPDATE 1"

    async def rollback_failed_patches(self, user_id: str) -> int:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE prompt_patches SET status='rolled_back', rolled_back_at=NOW()
                   WHERE user_id=$1::uuid AND status='failed' AND verified_sessions >= max_retries""", user_id)
            return int(result.split()[-1])

    async def get_pending_patches_count(self, user_id: str) -> int:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM prompt_patches WHERE user_id=$1::uuid AND status IN ('candidate','experiment')", user_id)

    async def aggregate_errors(self, user_id: str, session_limit: int = 3) -> dict:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) as total_errors,
                       COALESCE(SUM(count), 0) as total_occurrences,
                       ARRAY_AGG(DISTINCT error_type) as error_types
                FROM error_fingerprints
                WHERE user_id = $1::uuid
                  AND last_seen_at > NOW() - INTERVAL '3 days'
                  AND weight > $2
                """,
                user_id, WEIGHT_DELETE_THRESHOLD,
            )
            return dict(row) if row else {}

    async def get_daily_session_count(self, user_id: str) -> int:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(session_count, 0) as cnt
                FROM user_daily_state
                WHERE user_id = $1::uuid AND date = CURRENT_DATE
                """,
                user_id,
            )
            return row["cnt"] if row else 0

    async def increment_session_count(self, user_id: str):
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_daily_state (user_id, date, session_count, updated_at)
                VALUES ($1::uuid, CURRENT_DATE, 1, NOW())
                ON CONFLICT (user_id, date)
                DO UPDATE SET session_count = user_daily_state.session_count + 1,
                              updated_at = NOW()
                """,
                user_id,
            )

    async def apply_forgetting_curve(self, user_id: str) -> dict:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE error_fingerprints
                    SET weight = weight * POWER($2,
                        EXTRACT(DAY FROM (NOW() - last_seen_at)))
                    WHERE user_id = $1::uuid
                      AND last_seen_at < NOW() - INTERVAL '1 day'
                      AND weight > $3
                    RETURNING id
                )
                SELECT COUNT(*) FROM updated
                """,
                user_id, WEIGHT_DECAY_FACTOR, WEIGHT_DELETE_THRESHOLD,
            )
            deleted = await conn.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM error_fingerprints
                    WHERE user_id = $1::uuid AND weight < $2
                    RETURNING id, error_type
                )
                SELECT COUNT(*) FROM deleted
                """,
                user_id, WEIGHT_DELETE_THRESHOLD,
            )
            return {"updated": updated or 0, "deleted": deleted or 0}

    async def apply_forgetting_curve_all(self) -> dict:
        """全用户版遗忘曲线衰减（audit_scheduler 每日任务用）。

        与 apply_forgetting_curve 同公式（weight *= 0.95^闲置天数，低于阈值删除），
        但不限定 user_id——一次 SQL 扫全表，避免逐用户循环。幂等：重复执行只在
        新的闲置天数差下进一步衰减。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE error_fingerprints
                    SET weight = weight * POWER($1,
                        EXTRACT(DAY FROM (NOW() - last_seen_at)))
                    WHERE last_seen_at < NOW() - INTERVAL '1 day'
                      AND weight > $2
                    RETURNING id
                )
                SELECT COUNT(*) FROM updated
                """,
                WEIGHT_DECAY_FACTOR, WEIGHT_DELETE_THRESHOLD,
            )
            deleted = await conn.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM error_fingerprints
                    WHERE weight < $1
                    RETURNING id, error_type
                )
                SELECT COUNT(*) FROM deleted
                """,
                WEIGHT_DELETE_THRESHOLD,
            )
            return {"updated": updated or 0, "deleted": deleted or 0}

    async def list_history_candidates(self, user_id: str, limit: int = 100, *,
                                      query: str = "",
                                      query_vec: "list[float] | None" = None) -> list[dict]:
        """Return bounded episode candidates; ranking is shared with SQLite.

        `query`（2026-10-28）：旧实现只按 created_at 取最新 N 条，导致"更早但正好
        对题"的归档永远进不了打分池——主号 303 条归档时，后 203 条 BM25 一次也看不到。
        给了 query 就先按词面把命中的老归档捞出来，排在最新 N 条**前面**；没给 query
        时行为完全不变。

        `query_vec`（2026-10-28 补）：只做词面预筛的话，**换一种说法**的问句（"停电"↔"断电"）
        在进池前就被筛掉了，后面的向量重排永远看不到它们。给了向量就额外按余弦距离取前 N 条
        一起进池；向量由调用方算一次，这里不新增模型调用。
        SQLite 版没跟这两个参数（生产走 Postgres）。
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        safe_limit = max(1, min(int(limit), 200))
        patterns: list = []
        regexes: list = []
        if query:
            try:
                from .history_retrieval import lexical_tokens
            except Exception:
                try:
                    from memory.history_retrieval import lexical_tokens  # type: ignore
                except Exception:
                    lexical_tokens = None  # type: ignore
            if lexical_tokens is not None:
                try:
                    tokens = [str(t) for t in lexical_tokens(query) if len(str(t)) >= 2][:8]
                except Exception:
                    tokens = []
                # 英文词必须按单词边界（\mcat\M），否则 '%cat%' 会把 communicate、
                # education 这类词全捞进来；中文没有分词边界，仍用子串。
                patterns = [f"%{t}%" for t in tokens if not _ASCII_WORD.match(t)]
                regexes = [r"\m" + re.escape(t) + r"\M"
                           for t in tokens if _ASCII_WORD.match(t)]
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT session_id, summary, error_count, created_at, summary_embedding
                FROM session_archives
                WHERE user_id = $1::uuid
                  AND summary IS NOT NULL
                ORDER BY created_at DESC LIMIT $2
                """,
                user_id, safe_limit,
            )
            handoff_records = await conn.fetch(
                """SELECT conv_id, handoff_json, updated_at
                   FROM cassette_handoffs
                   WHERE user_id = $1::uuid
                   ORDER BY updated_at DESC LIMIT $2""",
                user_id, safe_limit,
            )
            matched_rows: list = []
            matched_handoffs: list = []
            vector_rows: list = []
            if query_vec:
                vec_lit = "[" + ",".join(f"{float(x):.6f}" for x in query_vec) + "]"
                try:
                    vector_rows = await conn.fetch(
                        """
                        SELECT session_id, summary, error_count, created_at, summary_embedding
                        FROM session_archives
                        WHERE user_id = $1::uuid
                          AND summary IS NOT NULL
                          AND summary_embedding IS NOT NULL
                        ORDER BY summary_embedding <=> $3::vector
                        LIMIT $2
                        """,
                        user_id, safe_limit, vec_lit,
                    )
                except Exception:
                    vector_rows = []
            if patterns or regexes:
                matched_rows = await conn.fetch(
                    """
                    SELECT session_id, summary, error_count, created_at, summary_embedding
                    FROM session_archives
                    WHERE user_id = $1::uuid
                      AND summary IS NOT NULL
                      AND (summary ILIKE ANY($3::text[]) OR summary ~* ANY($4::text[]))
                    ORDER BY created_at DESC LIMIT $2
                    """,
                    user_id, safe_limit, patterns or [""], regexes or ["(?!)"],
                )
                matched_handoffs = await conn.fetch(
                    """SELECT conv_id, handoff_json, updated_at
                       FROM cassette_handoffs
                       WHERE user_id = $1::uuid
                         AND (handoff_json::text ILIKE ANY($2::text[])
                              OR handoff_json::text ~* ANY($3::text[]))
                       ORDER BY updated_at DESC LIMIT $4""",
                    user_id, patterns or [""], regexes or ["(?!)"], safe_limit,
                )
            handoff_rows = []
            for record in list(matched_handoffs) + list(handoff_records):
                raw = record.get("handoff_json") or {}
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (TypeError, ValueError):
                        raw = {}
                searchable, summary = self._cassette_handoff_text(dict(raw or {}))
                if searchable:
                    handoff_rows.append({
                        "session_id": str(record.get("conv_id") or ""),
                        "summary": summary,
                        "searchable": searchable,
                        "error_count": 0,
                        "created_at": record.get("updated_at"),
                        "source": "cassette_handoff",
                    })
            archive_rows = [
                {**dict(record), "searchable": str(record.get("summary") or ""),
                 "source": "session_archive"}
                for record in list(matched_rows) + list(vector_rows) + list(rows)
            ]
            # 命中的老归档排前面：limit 是硬边界，不能让它把老归档挤回去。
            seen: set = set()
            combined = []
            for item in handoff_rows + archive_rows:
                key = (item.get("source"), str(item.get("session_id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                combined.append(item)
            combined.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            matched_keys = {
                (item.get("source"), str(item.get("session_id") or ""))
                for item in handoff_rows[:len(matched_handoffs)]
            }
            matched_keys |= {
                ("session_archive", str(r.get("session_id") or "")) for r in matched_rows
            }
            # 向量命中次优先：它们词面没中，但对题。
            vector_keys = {
                ("session_archive", str(r.get("session_id") or "")) for r in vector_rows
            }
            # 命中组排前面，但组内仍按新的在前：先按时间倒序，再稳定按命中分组。
            combined.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            combined.sort(key=lambda item: (
                0 if (item.get("source"), str(item.get("session_id") or "")) in matched_keys
                else 1 if (item.get("source"), str(item.get("session_id") or "")) in vector_keys
                else 2))
            return combined[:safe_limit]

    async def search_history(self, user_id: str, query: str, top_k: int = 5) -> list[dict]:
        """历史检索（plans/0915-06）：稀疏 BM25 + 摘要向量 RRF 混合。

        向量通道让"换一种措辞的回忆"（如只说'看花田'不说'北海道薰衣草'）也能命中；
        查询 embed 失败或候选全无向量时自动退回纯稀疏，行为与升级前一致。
        """
        # 先算查询向量：池子要用它捞同义问句（词面筛不到的那些）。
        query_vec = None
        try:
            from rag.shared_embedder import embed_text
            query_vec = await embed_text(query)
        except Exception:
            query_vec = None
        candidates = await self.list_history_candidates(
            user_id, limit=100, query=query, query_vec=query_vec)
        # 全量稀疏打分（不只 top_k），供 RRF 与向量榜融合
        sparse_ranked = rank_history_sparse(candidates, query, top_k=max(1, len(candidates)))

        vec_scored: list = []
        if query_vec:
            for item in candidates:
                raw = item.get("summary_embedding")
                if not raw:
                    continue
                try:
                    if isinstance(raw, str):
                        emb = [float(x) for x in raw.strip("[]").split(",") if x.strip()]
                    else:
                        emb = [float(x) for x in raw]
                except (TypeError, ValueError):
                    continue
                if len(emb) != len(query_vec):
                    continue
                dot = sum(a * b for a, b in zip(query_vec, emb))
                na = sum(a * a for a in query_vec) ** 0.5
                nb = sum(b * b for b in emb) ** 0.5
                if na <= 0 or nb <= 0:
                    continue
                vec_scored.append((dot / (na * nb), item))
        vec_scored.sort(key=lambda pair: pair[0], reverse=True)

        if vec_scored:
            K = 60
            sparse_rank = {id(item): idx for idx, item in enumerate(sparse_ranked)}
            vec_rank = {id(item): idx for idx, (_, item) in enumerate(vec_scored)}
            vec_cos = {id(item): cos for cos, item in vec_scored}
            for item in candidates:
                rrf = 0.0
                if id(item) in sparse_rank:
                    rrf += 1.0 / (K + sparse_rank[id(item)] + 1)
                if id(item) in vec_rank:
                    rrf += 1.0 / (K + vec_rank[id(item)] + 1)
                item["_rrf"] = rrf
            fused = sorted(
                [item for item in candidates if item.get("_rrf", 0) > 0],
                key=lambda item: item["_rrf"], reverse=True,
            )[:top_k]
            # 高置信裁剪（2026-10-25 治误召）：具体回忆（"还记得那个糯米团子摊吗"）的
            # 榜首余弦实测 ≥0.59，而误召候选 ≤0.47，中间有一条干净的沟；泛化问题
            # （"我们聊过什么吃的"）全体贴着 0.45 左右、榜首未必正确——那种情况**不
            # 裁**，交给注入合同让模型自己挑。只在榜首显著高置信时，砍掉与榜首差出
            # 0.12 以上的候选；榜首永不被砍，候选不会裁空。
            if fused:
                top_cos = vec_cos.get(id(fused[0]), 0.0)
                if top_cos >= 0.55:
                    fused = [
                        item for item in fused
                        if vec_cos.get(id(item), 0.0) >= top_cos - 0.12
                    ] or [fused[0]]
            for item in fused:
                item.pop("_rrf", None)
                item.pop("searchable", None)
                item["retrieval_mode"] = "hybrid"
            return fused

        for row in sparse_ranked[:top_k]:
            row.pop("searchable", None)
            row["retrieval_mode"] = "sparse"
        return sparse_ranked[:top_k]

    @staticmethod
    def _cassette_handoff_text(handoff: dict) -> tuple[str, str]:
        scene = handoff.get("scene") or {}
        plan = handoff.get("conversation_plan") or {}
        ledger = handoff.get("scene_ledger") or {}

        def values(raw) -> list[str]:
            if isinstance(raw, list):
                return [str(item).strip() for item in raw if str(item).strip()]
            return [] if raw in (None, "") else [str(raw).strip()]

        fields = [
            ("场景", scene.get("brief") or scene.get("name")),
            ("角色", " / ".join(values(scene.get("learner_role")) + values(scene.get("tutor_role")))),
            ("当前步骤", scene.get("step") or plan.get("current_phase") or ledger.get("current_phase")),
            ("目标", scene.get("goal") or plan.get("real_world_goal") or plan.get("content_need")),
            ("下一动作", plan.get("next_counterpart_action")),
            ("待处理", "、".join(values(plan.get("open_items")) + values(ledger.get("open_items")))),
            ("已完成", "、".join(values(plan.get("completed_actions")) + values(ledger.get("completed_actions")))),
        ]
        checkpoint = "；".join(
            f"{label}={str(value).strip()}" for label, value in fields if str(value or "").strip()
        )
        recent_parts = []
        for item in list(handoff.get("recent_messages") or [])[-8:]:
            role = "用户" if item.get("role") == "user" else "AI"
            content = " ".join(str(item.get("content") or "").split())
            if content:
                recent_parts.append(f"{role}:{content[:240]}")
        recent = " / ".join(recent_parts)
        searchable = " ".join(part for part in (checkpoint, recent) if part).strip()
        summary = f"{checkpoint}；最近对话={recent}" if checkpoint and recent else checkpoint or (f"最近对话={recent}" if recent else "")
        return searchable[:2400], summary[:900]

    async def save_cassette_handoff(self, user_id: str, conv_id: str, handoff: dict) -> None:
        user_id = _as_uuid(user_id)
        if not conv_id:
            return
        pool = await self._get_pool()
        await pool.execute(
            """INSERT INTO cassette_handoffs (user_id, conv_id, handoff_json, updated_at)
               VALUES ($1::uuid, $2, $3::jsonb, NOW())
               ON CONFLICT(user_id, conv_id) DO UPDATE SET
                 handoff_json=EXCLUDED.handoff_json, updated_at=NOW()""",
            user_id, conv_id, json.dumps(handoff or {}, ensure_ascii=False),
        )

    async def load_cassette_handoff(self, user_id: str, conv_id: str) -> dict:
        user_id = _as_uuid(user_id)
        if not conv_id:
            return {}
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT handoff_json FROM cassette_handoffs WHERE user_id=$1::uuid AND conv_id=$2",
            user_id, conv_id,
        )
        return dict(row["handoff_json"] or {}) if row else {}

    async def save_memory_curator_run(self, run: dict) -> None:
        pool = await self._get_pool()
        await pool.execute(
            """INSERT INTO memory_curator_runs
               (run_id, user_id, session_id, conversation_id, trigger_name, status,
                candidates_json, accepted_json, metadata_json, created_at)
               VALUES ($1, $2::uuid, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, NOW())
               ON CONFLICT(run_id) DO UPDATE SET
                 status=EXCLUDED.status, candidates_json=EXCLUDED.candidates_json,
                 accepted_json=EXCLUDED.accepted_json, metadata_json=EXCLUDED.metadata_json""",
            str(run.get("run_id") or ""), _as_uuid(run.get("user_id") or ""),
            str(run.get("session_id") or ""), str(run.get("conversation_id") or ""),
            str(run.get("trigger_name") or "session_end"), str(run.get("status") or "unknown"),
            json.dumps(run.get("candidates") or [], ensure_ascii=False),
            json.dumps(run.get("accepted_items") or [], ensure_ascii=False),
            json.dumps(run.get("metadata") or {}, ensure_ascii=False),
        )

    async def load_memory_curator_runs(self, user_id: str, limit: int = 20) -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        rows = await pool.fetch(
            """SELECT * FROM memory_curator_runs WHERE user_id=$1::uuid
               ORDER BY created_at DESC LIMIT $2""",
            user_id, max(1, min(int(limit), 100)),
        )
        result = []
        for row in rows:
            item = dict(row)
            from engine.chunk_evidence import loads_json_list
            item["candidates"] = loads_json_list(item.pop("candidates_json", None))
            item["accepted_items"] = loads_json_list(item.pop("accepted_json", None))
            raw_metadata = item.pop("metadata_json", None)
            if isinstance(raw_metadata, dict):
                item["metadata"] = raw_metadata
            elif isinstance(raw_metadata, str) and raw_metadata.strip():
                try:
                    parsed_metadata = json.loads(raw_metadata)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_metadata = {}
                item["metadata"] = parsed_metadata if isinstance(parsed_metadata, dict) else {}
            else:
                item["metadata"] = {}
            result.append(item)
        return result

    @staticmethod
    def _lexical_units(text: str) -> set[str]:
        lowered = (text or "").lower()
        english = {token for token in re.findall(r"[a-z0-9]+", lowered) if len(token) > 1}
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
        grams = {
            chinese[index:index + size]
            for size in (2, 3)
            for index in range(max(0, len(chinese) - size + 1))
        }
        return english | grams

    # ── 向量检索（pgvector） ────────────────────────────────

    async def store_embedding(
        self,
        name: str,
        instruction: str,
        embedding: list[float],
        cefr_level: str = "",
        scene_tags: list[str] = None,
        boost_score: float = 0.0,
        source: str = "",
    ) -> str:
        """
        存储向量嵌入到 pgvector。

        Args:
            name: 实体名称
            instruction: 指令文本
            embedding: 向量嵌入（1024维）
            cefr_level: CEFR等级
            scene_tags: 场景标签列表
            boost_score: 提升分数
            source: 来源

        Returns:
            插入记录的ID
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO methodology_vectors (
                    name, instruction, example_dialogue, cefr_level,
                    scene_tags, embedding, boost_score, source
                ) VALUES ($1, $2, '', $3, $4, $5, $6, $7)
                ON CONFLICT (name) DO UPDATE SET
                    instruction = EXCLUDED.instruction,
                    embedding = EXCLUDED.embedding,
                    boost_score = EXCLUDED.boost_score,
                    updated_at = NOW()
                RETURNING id
                """,
                name, instruction, cefr_level,
                scene_tags or [], embedding, boost_score, source,
            )
            return str(row["id"])

    async def search_embeddings(
        self,
        query_embedding: list[float],
        cefr_level: str = "",
        top_k: int = 5,
    ) -> list[dict]:
        """
        使用 pgvector 进行向量相似性搜索。

        Args:
            query_embedding: 查询向量（1024维）
            cefr_level: CEFR等级过滤（可选）
            top_k: 返回结果数量

        Returns:
            相似性搜索结果列表，包含 name、instruction、similarity 等字段
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if cefr_level:
                rows = await conn.fetch(
                    """
                    SELECT id, name, instruction, cefr_level,
                           1 - (embedding <=> $1) as similarity
                    FROM methodology_vectors
                    WHERE cefr_level = $2
                    ORDER BY embedding <=> $1 LIMIT $3
                    """,
                    query_embedding, cefr_level, top_k,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, name, instruction, cefr_level,
                           1 - (embedding <=> $1) as similarity
                    FROM methodology_vectors
                    ORDER BY embedding <=> $1 LIMIT $2
                    """,
                    query_embedding, top_k,
                )
            return [dict(r) for r in rows]

    async def get_methodology_calls(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """
        获取用户的教学法调用记录。

        Args:
            user_id: 用户ID
            limit: 返回记录数量

        Returns:
            调用记录列表
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT mc.id, mv.name, mv.instruction, mc.cooperation_score,
                       mc.session_id, mc.created_at
                FROM methodology_calls mc
                JOIN methodology_vectors mv ON mc.methodology_id = mv.id
                WHERE mc.user_id = $1::uuid
                ORDER BY mc.created_at DESC LIMIT $2
                """,
                user_id, limit,
            )
            return [dict(r) for r in rows]

    async def log_methodology_call(
        self,
        user_id: str,
        methodology_id: int,
        session_id: str,
        cooperation_score: float = 0.0,
    ) -> str:
        user_id, session_id = _as_uuid(user_id), _as_uuid(session_id)
        """
        记录教学法调用日志。

        Args:
            user_id: 用户ID
            methodology_id: 教学法ID
            session_id: 会话ID
            cooperation_score: 合作分数

        Returns:
            插入记录的ID
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO methodology_calls (
                    user_id, methodology_id, session_id, cooperation_score
                ) VALUES ($1::uuid, $2, $3::uuid, $4)
                RETURNING id
                """,
                user_id, methodology_id, session_id, cooperation_score,
            )
            return str(row["id"]) if row else ""

    # ══════════════════════════════════════════════════════════════════
    # 补齐 redis 后端相对 l2_sqlite 缺失的方法
    #
    # 背景：/conversations/*、/memory/context/* 在 redis 后端直接 500；
    # /privacy/export 与 /privacy/data 被 hasattr 兜成「空导出 / 假删除」。
    # 用户诉求是「每个用户都要被记录 + 隐私可控」，所以这些必须是真的。
    #
    # user_id 的两种存储形态（沿用既有表，刻意不"统一"）：
    #   - 绝大多数表是 UUID 列 → 用 _as_uuid(user_id)
    #   - feedback_signals / voice_turn_metrics / metering_events 是 TEXT 列 → 原样字符串
    # ══════════════════════════════════════════════════════════════════

    # style_parameters 的可写列白名单（列名会拼进 SQL，必须白名单，不能透传调用方）
    _STYLE_COLUMNS = (
        "formality", "warmth", "humor", "directness", "correction_style",
        "error_tolerance", "challenge_level", "preferred_length", "language_mix",
        "example_frequency", "opening_style", "followup_style", "version",
        "feedback_count",
    )

    # 用户自有数据表：(导出键名, 表名, user_id 列类型)
    _USER_TABLES = (
        ("errors", "error_fingerprints", "uuid"),
        ("mastered", "mastered_expressions", "uuid"),
        ("learning_items", "learning_item_states", "uuid"),
        ("speech_assessments", "speech_assessments", "uuid"),
        ("style_parameters", "style_parameters", "uuid"),
        ("prompt_patches", "prompt_patches", "uuid"),
        ("conversation_meta", "conversation_meta", "uuid"),
        ("conversation_messages", "conversation_messages", "uuid"),
        ("session_archives", "session_archives", "uuid"),
        ("relationship_memory", "relationship_memory", "uuid"),
        ("companion_persona_facts", "companion_persona_facts", "uuid"),
        ("companion_persona_snapshots", "companion_persona_snapshots", "uuid"),
        ("companion_life_items", "companion_life_items", "uuid"),
        ("memory_entity_relations", "memory_entity_relations", "uuid"),
        ("memory_entities", "memory_entities", "uuid"),
        ("user_daily_state", "user_daily_state", "uuid"),
        ("cassette_handoffs", "cassette_handoffs", "uuid"),
        ("memory_curator_runs", "memory_curator_runs", "uuid"),
        ("feedback_events", "feedback_events", "uuid"),
        ("session_audit_status", "session_audit_status", "uuid"),
        ("methodology_calls", "methodology_calls", "uuid"),
        ("knowledge_usage", "knowledge_usage", "uuid"),
        ("evolution_log", "evolution_log", "uuid"),
        ("feedback_signals", "feedback_signals", "text"),
        ("voice_turn_metrics", "voice_turn_metrics", "text"),
        ("metering_events", "metering_events", "text"),
        # 素材（plans/0916-21）：正文是 A 类内容 → 导出/真删**必须**带上它，
        # 否则"隐私可控（能导出、能真删）"这条硬要求就有洞。
        ("materials", "material_items", "uuid"),
        # 素材库（plans/0923-08）：全量正文块与成盘提要都是用户自有内容 →
        # 导出/真删必须带上，否则"能导出、能真删"在素材库这条线上有洞。
        ("material_source_blocks", "material_source_blocks", "uuid"),
        ("material_tape_builds", "material_tape_builds", "uuid"),
        # 统一画像（plans/0923-04 §4.5）：两表都要能导出、能真删。
        ("profile_facts", "learner_profile_facts", "uuid"),
        ("profile_narratives", "learner_profile_narratives", "uuid"),
        # 练习盘来源快照（plans/0923-06 §4.1.2）：要能导出、能真删。
        ("expression_topic_assignments", "expression_topic_assignments", "uuid"),
    )

    # ── 序列化 / 时间戳小工具 ────────────────────────────────

    @staticmethod
    def _jsonable(row: dict) -> dict:
        """把 asyncpg 返回值转成可直接 JSON 序列化的 dict。

        本地后端这些列都是 TEXT，接口上就是字符串；显式转换是为了让两种后端
        返回给前端的 JSON 形状一致（否则前端对同一个字段的解析会分叉）。
        """
        out = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                out[key] = value.isoformat()
            elif value is None or isinstance(value, (str, int, float, bool, list, dict)):
                out[key] = value
            else:                     # uuid.UUID / Decimal 等
                out[key] = str(value)
        return out

    @staticmethod
    def _coerce_ts(raw):
        """把前端传来的时间戳归一成 datetime；无法识别时返回 None（落库用 NOW()）。

        前端 `_ConvMsg.created_at` 是 int（默认 0，但也可能是 epoch 毫秒），
        直接当字符串塞进 TIMESTAMPTZ 会报错，所以必须在这里判掉。
        """
        if raw in (None, "", 0, "0"):
            return None
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 1e11:          # 毫秒
                value /= 1000.0
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    # ── 会话存档 / L1 画像 / 风格参数 ───────────────────────

    async def save_entities(self, user_id: str, conv_id: str, entities: list[dict]) -> int:
        """落库摘要顺路抽取的实体（plans/0915-09，图谱-lite 写侧）。

        幂等：UNIQUE(user_id, etype, name, conv_id)，同会话重写只更新 detail。
        同一会话同一泛称若对应多个不同 detail，写入前分配稳定的局部后缀，
        避免唯一键让最后一个人静默覆盖前一个人。
        非法条目（缺 name / etype 越界）静默跳过，绝不抛错阻塞保存路径。

        2026-10-25 实体层大修：跨会话复用（prepare_entity_rows 保留了抽取器
        给出的既有 entity_id）时，按既有行的 entity_id 精确 UPDATE（detail
        追加、confidence/evidence 择优），绝不因 conv_id 不同而新开行——否则
        extractor 复用了 ID、SQL 却归档成新行（同 ID 不同 conv_id），既没合并
        还可能重复分裂（真机：同一事件 7 行，见 PROGRESS 实体层大修档）。无
        既有 ID 的新抽取仍走原 upsert（冲突键带 conv_id，防同会话两个同名
        不同人静默合并）。
        """
        user_id = _as_uuid(user_id)
        rows = prepare_entity_rows(str(user_id), conv_id, entities)
        import sys
        if not rows:
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            reused = []
            fresh = []
            for row in rows:
                if len(row) > 5 and row[5]:  # row[5] = entity_id（既有ID）
                    reused.append(row)
                else:
                    fresh.append(row)
            for row in reused:
                eid = str(row[5]).lower()
                exists = await conn.fetchval(
                    "SELECT 1 FROM memory_entities WHERE entity_id=$1 AND user_id=$2 LIMIT 1",
                    eid, user_id)
                if not exists:
                    # 目标行不存在（数据被清/重建）→ 等价于新实体，交回 fresh 路径
                    fresh.append(row)
                    continue
                detail = "" if row[4] is None else str(row[4])
                ev_ref = "" if len(row) <= 8 else str(row[8] or "")
                await conn.execute(
                    """UPDATE memory_entities SET
                           detail = CASE
                               WHEN $3 = '' OR POSITION($3 IN memory_entities.detail) > 0
                               THEN memory_entities.detail
                               ELSE LEFT(CONCAT_WS('；', NULLIF(memory_entities.detail, ''), $3), 600)
                           END,
                           confidence = GREATEST(memory_entities.confidence, $4),
                           evidence_source = CASE WHEN $5 = '' THEN memory_entities.evidence_source ELSE $5 END,
                           evidence_ref = CASE WHEN $6 = '' THEN memory_entities.evidence_ref ELSE $6 END,
                           source_message_seq = CASE WHEN $6 = '' THEN memory_entities.source_message_seq ELSE $7 END,
                           evidence_quote = CASE WHEN $6 = '' THEN memory_entities.evidence_quote ELSE $8 END
                       WHERE entity_id = $1 AND user_id = $2""",
                    eid, user_id,
                    detail,
                    float(row[7] if len(row) > 7 else 0.65),
                    ("" if len(row) <= 6 else str(row[6] or "")),
                    ev_ref,
                    (0 if len(row) <= 9 else row[9]),
                    ("" if len(row) <= 10 else str(row[10] or "")),
                )
            if fresh:
                await conn.executemany(
                    """INSERT INTO memory_entities
                           (user_id, conv_id, etype, name, detail, entity_id,
                            evidence_source, confidence, evidence_ref, source_message_seq, evidence_quote)
                       VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                       ON CONFLICT (user_id, etype, name, conv_id)
                       DO UPDATE SET detail = CASE
                                   WHEN EXCLUDED.detail = '' OR POSITION(EXCLUDED.detail IN memory_entities.detail) > 0
                                   THEN memory_entities.detail
                                   ELSE LEFT(CONCAT_WS('；', NULLIF(memory_entities.detail, ''), EXCLUDED.detail), 600)
                                 END,
                                 evidence_source = EXCLUDED.evidence_source,
                                 confidence = GREATEST(memory_entities.confidence, EXCLUDED.confidence),
                                 evidence_ref = CASE WHEN EXCLUDED.evidence_ref = ''
                                   THEN memory_entities.evidence_ref ELSE EXCLUDED.evidence_ref END,
                                 source_message_seq = CASE WHEN EXCLUDED.evidence_ref = ''
                                   THEN memory_entities.source_message_seq ELSE EXCLUDED.source_message_seq END,
                                 evidence_quote = CASE WHEN EXCLUDED.evidence_ref = ''
                                   THEN memory_entities.evidence_quote ELSE EXCLUDED.evidence_quote END""",
                    fresh,
                )
        return len(rows)


    async def save_entity_relations(self, user_id: str, conv_id: str,
                                    relations: list[dict]) -> int:
        """Append idempotent relation edges, accepting only this user's entity IDs."""
        user_id = _as_uuid(user_id)
        rows = prepare_entity_relation_rows(str(user_id), conv_id, relations)
        if not rows:
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            known_rows = await conn.fetch(
                "SELECT DISTINCT entity_id FROM memory_entities WHERE user_id = $1::uuid",
                user_id,
            )
            known_ids = {str(row["entity_id"] or "") for row in known_rows}
            rows = [row for row in rows if row[3] in known_ids and (not row[5] or row[5] in known_ids)]
            if not rows:
                return 0
            results = await conn.fetch(
                """INSERT INTO memory_entity_relations
                        (user_id, conv_id, edge_key, subject_entity_id, predicate,
                        object_entity_id, object_value, evidence_source, evidence_ref,
                        confidence, source_message_seq, evidence_quote)
                   SELECT $1::uuid, x.conv_id, x.edge_key, x.subject_entity_id, x.predicate,
                          x.object_entity_id, x.object_value, x.evidence_source, x.evidence_ref,
                          x.confidence, x.source_message_seq, x.evidence_quote
                   FROM UNNEST($2::text[], $3::text[], $4::text[], $5::text[], $6::text[],
                              $7::text[], $8::text[], $9::text[], $10::double precision[],
                              $11::integer[], $12::text[])
                        AS x(conv_id, edge_key, subject_entity_id, predicate, object_entity_id,
                             object_value, evidence_source, evidence_ref, confidence, source_message_seq,
                             evidence_quote)
                   ON CONFLICT (user_id, conv_id, edge_key) DO NOTHING
                   RETURNING id""",
                user_id,
                [str(row[1]) for row in rows],
                [str(row[2]) for row in rows],
                [str(row[3]) for row in rows],
                [str(row[4]) for row in rows],
                [str(row[5]) for row in rows],
                [str(row[6]) for row in rows],
                [str(row[7]) for row in rows],
                [str(row[8]) for row in rows],
                [float(row[9]) for row in rows],
                [row[10] for row in rows],
                [str(row[11]) for row in rows],
            )
            return len(results)

    async def search_entities_for_text(self, user_id: str, query_text: str,
                                       limit: int = 12) -> list[dict]:
        """Find old entities mentioned or cued by the current utterance.

        Unlike the session-start cache, this searches the user's full entity set.
        It is a bounded SQL read, uses no LLM, and never decides identity merges.
        """
        user_id = _as_uuid(user_id)
        query_text = " ".join(str(query_text or "").split())[:1200]
        patterns = [f"%{term}%" for term in entity_search_terms(query_text)]
        safe_limit = max(1, min(int(limit), 24))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT e.conv_id, e.etype, e.name, e.detail, e.entity_id,
                          e.evidence_source, e.confidence, e.evidence_ref, e.evidence_quote,
                          e.source_message_seq, e.created_at, m.created_at AS observed_at
                   FROM memory_entities e
                   LEFT JOIN conversation_messages m
                     ON m.user_id = e.user_id AND m.conv_id = e.conv_id AND m.seq = e.source_message_seq
                   WHERE e.user_id = $1::uuid AND (
                       (length(e.name) >= 2 AND position(lower(e.name) in lower($2)) > 0)
                       OR lower(e.name) LIKE ANY($3::text[])
                       OR lower(e.detail) LIKE ANY($3::text[])
                   )
                   ORDER BY CASE WHEN length(e.name) >= 2
                                      AND position(lower(e.name) in lower($2)) > 0
                                 THEN 1 ELSE 0 END DESC,
                            COALESCE(m.created_at, e.created_at) DESC LIMIT 240""",
                user_id, query_text, patterns,
            )
            entities = merge_entity_search_observations(
                [dict(row) for row in rows], query_text, limit=safe_limit,
            )
            entity_ids = [str(entity["entity_id"]) for entity in entities]
            if not entity_ids:
                return []
            relation_rows = await conn.fetch(
                """SELECT r.subject_entity_id, r.predicate, r.object_entity_id,
                          r.object_value, r.evidence_source, r.evidence_ref, r.confidence,
                          r.source_message_seq, r.evidence_quote, r.created_at,
                          m.created_at AS observed_at,
                          (SELECT e.name FROM memory_entities e
                           WHERE e.user_id = r.user_id AND e.entity_id = r.object_entity_id
                           ORDER BY e.created_at DESC LIMIT 1) AS object_name
                   FROM memory_entity_relations r
                   LEFT JOIN conversation_messages m
                     ON m.user_id = r.user_id AND m.conv_id = r.conv_id AND m.seq = r.source_message_seq
                   WHERE r.user_id = $1::uuid
                     AND (r.subject_entity_id = ANY($2::text[])
                          OR r.object_entity_id = ANY($2::text[]))
                   ORDER BY COALESCE(m.created_at, r.created_at) DESC LIMIT 120""",
                user_id, entity_ids,
            )
            relation_dicts = [dict(row) for row in relation_rows]
            neighbor_ids = entity_neighborhood_ids(
                set(entity_ids), relation_dicts, limit=min(4, safe_limit),
            )
            neighbor_rows = []
            if neighbor_ids:
                neighbor_rows = await conn.fetch(
                    """SELECT DISTINCT ON (e.entity_id)
                              e.conv_id, e.etype, e.name, e.detail, e.entity_id,
                              e.evidence_source, e.confidence, e.evidence_ref,
                              e.evidence_quote, e.source_message_seq, e.created_at,
                              m.created_at AS observed_at
                       FROM memory_entities e
                       LEFT JOIN conversation_messages m
                         ON m.user_id=e.user_id AND m.conv_id=e.conv_id AND m.seq=e.source_message_seq
                       WHERE e.user_id=$1::uuid AND e.entity_id=ANY($2::text[])
                       ORDER BY e.entity_id, COALESCE(m.created_at, e.created_at) DESC""",
                    user_id, neighbor_ids,
                )
            return assemble_entity_neighborhood(
                entities, [dict(row) for row in neighbor_rows], relation_dicts,
                neighbor_limit=min(4, safe_limit),
            )

    async def list_entities(self, user_id: str, name: str | None = None, limit: int = 50) -> list[dict]:
        """读侧查询（为将来的读侧接入预留）：按用户可选按实体名查，最新在前。"""
        user_id = _as_uuid(user_id)
        safe_limit = max(1, min(int(limit), 200))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if name:
                rows = await conn.fetch(
                    """SELECT e.conv_id, e.etype, e.name, e.detail, e.entity_id,
                              e.evidence_source, e.confidence, e.evidence_ref, e.evidence_quote,
                              e.source_message_seq, e.created_at, m.created_at AS observed_at
                       FROM memory_entities e
                       LEFT JOIN conversation_messages m
                         ON m.user_id = e.user_id AND m.conv_id = e.conv_id AND m.seq = e.source_message_seq
                       WHERE e.user_id = $1::uuid AND e.name ILIKE $2
                       ORDER BY COALESCE(m.created_at, e.created_at) DESC LIMIT $3""",
                    user_id, f"%{name.strip()}%", safe_limit)
            else:
                rows = await conn.fetch(
                    """SELECT e.conv_id, e.etype, e.name, e.detail, e.entity_id,
                              e.evidence_source, e.confidence, e.evidence_ref, e.evidence_quote,
                              e.source_message_seq, e.created_at, m.created_at AS observed_at
                       FROM memory_entities e
                       LEFT JOIN conversation_messages m
                         ON m.user_id = e.user_id AND m.conv_id = e.conv_id AND m.seq = e.source_message_seq
                       WHERE e.user_id = $1::uuid
                       ORDER BY COALESCE(m.created_at, e.created_at) DESC LIMIT $2""",
                    user_id, safe_limit)
            entities = [dict(r) for r in rows]
            entity_ids = list(dict.fromkeys(
                str(row.get("entity_id") or "") for row in entities
                if str(row.get("entity_id") or "")
            ))
            relations_by_subject: dict[str, list[dict]] = {}
            if entity_ids:
                relation_rows = await conn.fetch(
                    """SELECT r.subject_entity_id, r.predicate, r.object_entity_id,
                              r.object_value, r.evidence_source, r.evidence_ref, r.confidence,
                              r.source_message_seq, r.evidence_quote, r.created_at, m.created_at AS observed_at,
                              (SELECT e.name FROM memory_entities e
                               WHERE e.user_id = r.user_id AND e.entity_id = r.object_entity_id
                               ORDER BY e.created_at DESC LIMIT 1) AS object_name
                       FROM memory_entity_relations r
                       LEFT JOIN conversation_messages m
                         ON m.user_id = r.user_id AND m.conv_id = r.conv_id AND m.seq = r.source_message_seq
                       WHERE r.user_id = $1::uuid AND r.subject_entity_id = ANY($2::text[])
                       ORDER BY COALESCE(m.created_at, r.created_at) DESC LIMIT 120""",
                    user_id, entity_ids,
                )
                for row in relation_rows:
                    relation = dict(row)
                    subject_id = str(relation.pop("subject_entity_id") or "")
                    relations_by_subject.setdefault(subject_id, []).append(relation)
            for entity in entities:
                entity["relations"] = relations_by_subject.get(str(entity.get("entity_id") or ""), [])
        return entities

    # ── 词块本（plans/0916-11，与 L2SQLiteMemory 同方法面）────────────
    # 双后端硬约定（0916-09）：四个 chunk_* 方法两边必须成对实现——API 路由
    # 直接调 l2 方法，缺一个就是线上 500 或被 hasattr 兜成静默空值。

    async def save_chunk_candidates(self, user_id: str, conv_id: str,
                                    candidates: list[dict]) -> int:
        """候选落库（幂等）：UNIQUE(user_id, norm)。

        ON CONFLICT 只在已有行仍是 candidate 时刷新来源/语境信息
        （用户已收藏的条目绝不被重盘归纳改写）；非法条目（缺 text/norm 或
        kind 越界）静默跳过，绝不抛错阻塞保存路径。
        """
        from engine.chunk_evidence import CHUNK_KINDS
        user_id = _as_uuid(user_id)
        rows = []
        for c in candidates or []:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text") or "").strip()[:80]
            norm = str(c.get("norm") or "").strip()[:120]
            kind = str(c.get("kind") or "collocation").strip().lower()
            if not text or not norm or kind not in CHUNK_KINDS:
                continue
            turn = None
            raw_turn = str(c.get("source_turn") or "").strip()
            if raw_turn.isdigit():
                turn = int(raw_turn)
            rows.append((user_id, text, norm, kind,
                         str(c.get("source_type") or "tape")[:20],
                         str(c.get("source_id") or str(conv_id or ""))[:120],
                         turn,
                         str(c.get("context_snippet") or "")[:200],
                         str(c.get("gloss") or "")[:80],
                         str(c.get("example") or "")[:200],
                         str(c.get("slot") or "")[:40],
                         str(c.get("reason") or "")[:160]))
        if not rows:
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO chunk_items (user_id, text, norm, kind,
                                            source_type, source_id, source_turn,
                                            context_snippet, gloss, example, slot, reason)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                   ON CONFLICT (user_id, norm) DO UPDATE SET
                     text = EXCLUDED.text, kind = EXCLUDED.kind,
                     source_type = EXCLUDED.source_type,
                     source_id = EXCLUDED.source_id,
                     source_turn = EXCLUDED.source_turn,
                     context_snippet = EXCLUDED.context_snippet,
                     gloss = EXCLUDED.gloss, example = EXCLUDED.example,
                     slot = EXCLUDED.slot, reason = EXCLUDED.reason,
                     updated_at = NOW()
                   WHERE chunk_items.status = 'candidate'""",
                rows,
            )
        return len(rows)

    async def list_chunks(self, user_id: str, status: str | None = None,
                          limit: int = 200) -> list[dict]:
        """词块列表（本子读侧）：可选按状态过滤，新收的在前。"""
        user_id = _as_uuid(user_id)
        safe_limit = max(1, min(int(limit), 500))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    """SELECT * FROM chunk_items WHERE user_id = $1::uuid AND status = $2
                       ORDER BY updated_at DESC, id DESC LIMIT $3""",
                    user_id, str(status), safe_limit)
            else:
                rows = await conn.fetch(
                    """SELECT * FROM chunk_items WHERE user_id = $1::uuid
                       ORDER BY updated_at DESC, id DESC LIMIT $2""",
                    user_id, safe_limit)
        return [dict(r) for r in rows]

    async def list_chunks_for_notebook(self, user_id: str, bucket: str = "",
                                       offset: int = 0, limit: int = 50) -> dict:
        """词块本只读分页（plans/0923-05 §4.4）：按真实时间分桶 + 组内排序 + 切页。

        与旧 `list_chunks` **并行、互不影响**：这里不设 200/500 条截断，
        专门服务"放久的候选"折叠后仍能逐页取回。分桶/排序/切页全在
        `engine.chunk_evidence` 的共用纯函数里做（与 SQLite 侧同签名同返回键）。
        JSONB 的 `status_history` 读取一律经 `loads_json_list`（纯函数内）。
        """
        from engine.chunk_evidence import page_notebook_chunks
        uid = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM chunk_items WHERE user_id = $1::uuid ORDER BY id", uid)
        return page_notebook_chunks([dict(r) for r in rows],
                                    bucket, offset=offset, limit=limit)

    async def update_chunk_status(self, user_id: str, chunk_id: str,
                                  status: str) -> bool:
        """状态流转（append-only：每次流转追加进 status_history 留底）。

        收藏 = candidate→saved；撤销收藏 = saved→candidate（照方案 §4.3，
        退回候选不删行）；机会轮练过 = saved→activated；会用 = →mastered。
        目标状态越界或行不存在返回 False。
        """
        from engine.chunk_evidence import CHUNK_STATUSES
        status = str(status or "").strip().lower()
        if status not in CHUNK_STATUSES:
            return False
        try:
            cid = int(str(chunk_id).strip())
        except (ValueError, TypeError):
            return False
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, status, status_history FROM chunk_items "
                "WHERE user_id = $1::uuid AND id = $2::bigint",
                user_id, cid)
            if not row:
                return False
            # 同 record_chunk_use：jsonb 经 asyncpg 是 str，不能 isinstance(list) 判。
            from engine.chunk_evidence import loads_json_list
            history = loads_json_list(row["status_history"])
            history = list(history) + [{
                "from": str(row["status"] or ""),
                "to": status,
                "at": datetime.now(timezone.utc).isoformat(),
            }]
            result = await conn.execute(
                """UPDATE chunk_items SET status = $3, status_history = $4::jsonb,
                   updated_at = NOW() WHERE id = $1::bigint AND user_id = $2::uuid""",
                int(row["id"]), user_id, status, json.dumps(history, ensure_ascii=False))
        return result.endswith("1")

    async def record_chunk_use(self, user_id: str, chunk_id: str,
                               event: dict) -> dict:
        """记录一次真实使用（F13 会用确认的写侧，二期启用；一期先落方法面）。

        use_events 追加 [{conv_id, turn, via_opportunity, at}]，use_count +1，
        last_use_at 刷新。返回 {use_count, status} 供调用方做状态流转判定。
        """
        try:
            cid = int(str(chunk_id).strip())
        except (ValueError, TypeError):
            return {}
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, use_count, use_events, status FROM chunk_items "
                "WHERE user_id = $1::uuid AND id = $2::bigint",
                user_id, cid)
            if not row:
                return {}
            # asyncpg 默认把 jsonb 按 str 返回（不自动解码）——必须
            # loads_json_list 解析，否则每次都从空列表开始"追加"，
            # 历史被整列覆盖（真机冒烟抓到；与 l2_sqlite 同款实现对齐）。
            from engine.chunk_evidence import loads_json_list
            events = loads_json_list(row["use_events"])
            ev = dict(event or {})
            events = list(events) + [{
                "conv_id": str(ev.get("conv_id") or "")[:120],
                "turn": int(ev["turn"]) if str(ev.get("turn") or "").strip().isdigit() else None,
                "via_opportunity": bool(ev.get("via_opportunity")),
                "at": str(ev.get("at") or datetime.now(timezone.utc).isoformat()),
            }]
            use_count = int(row["use_count"] or 0) + 1
            await conn.execute(
                """UPDATE chunk_items SET use_count = $3, use_events = $4::jsonb,
                   last_use_at = NOW(), updated_at = NOW()
                   WHERE id = $1::bigint AND user_id = $2::uuid""",
                int(row["id"]), user_id, use_count,
                json.dumps(events, ensure_ascii=False))
            return {"use_count": use_count, "status": str(row["status"] or "")}

    async def list_message_chunks(self, user_id: str, limit: int = 60) -> list[dict]:
        """原始消息进检索池（plans/0915-14，书§3.3.5 上下文感知检索）。

        跨盘检索此前只有每盘一条 80 字摘要 + 交接包——问"我上次说的那家店
        叫什么"这类具体名词基本查不到（摘要里大概率没写店名）。本方法把已
        落库的 conversation_messages 按对话分组、滑窗切块，**每块前置该对话
        的归档摘要作为上下文前缀**：裸碎片（"好的，就订这个吧"）脱离上下文
        没有信息量，前缀同时进 BM25 与向量两路，是召回质量的关键。

        只在 episodic_recall 显式回忆轮被 HybridHistoryRetriever 调用，
        不会进入逐轮热路径。最近 8 盘、每盘最多 60 条消息、总量封顶。
        """
        user_id = _as_uuid(user_id)
        safe_limit = max(1, min(int(limit), 120))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            convs = await conn.fetch(
                """SELECT conv_id, max(created_at) AS latest
                   FROM conversation_messages
                   WHERE user_id = $1::uuid AND role IN ('user', 'assistant')
                     AND length(coalesce(content, '')) > 0
                   GROUP BY conv_id ORDER BY latest DESC LIMIT 8""",
                user_id,
            )
            if not convs:
                return []
            summaries = {
                str(r["session_id"]): str(r["summary"] or "")
                for r in await conn.fetch(
                    "SELECT session_id, summary FROM session_archives WHERE user_id = $1::uuid",
                    user_id,
                )
            }
            metas = {
                str(r["conv_id"]): (str(r["name"] or ""), str(r["scene"] or ""))
                for r in await conn.fetch(
                    "SELECT conv_id, name, scene FROM conversation_meta WHERE user_id = $1::uuid",
                    user_id,
                )
            }
            rows: list[dict] = []
            for conv in convs:
                conv_id = str(conv["conv_id"] or "")
                msgs = await conn.fetch(
                    """SELECT role, content, created_at
                       FROM conversation_messages
                       WHERE user_id = $1::uuid AND conv_id = $2
                         AND role IN ('user', 'assistant')
                         AND length(coalesce(content, '')) > 0
                       ORDER BY seq NULLS FIRST, created_at LIMIT 60""",
                    user_id, conv_id,
                )
                if not msgs:
                    continue
                # 上下文前缀：归档摘要优先，退化用对话名/场景，最后用占位
                prefix = summaries.get(str(_as_uuid(conv_id)), "")
                if not prefix:
                    name, scene = metas.get(conv_id, ("", ""))
                    prefix = name or scene or "更早的一盘对话"
                prefix = " ".join(prefix.split())[:60]
                for start in range(0, len(msgs), 4):
                    chunk = msgs[start:start + 6]
                    if not chunk:
                        continue
                    body = " / ".join(
                        ("用户" if m["role"] == "user" else "AI") + ":" + str(m["content"])
                        for m in chunk
                    )
                    if not body.strip():
                        continue
                    rows.append({
                        "candidate_id": f"{conv_id}#msgs-{start}-{start + len(chunk) - 1}",
                        "session_id": conv_id,
                        "summary": f"旧磁带原文片段（出自：{prefix}）={body}"[:1800],
                        "searchable": f"【出自旧对话：{prefix}】" + body[:2400],
                        "error_count": 0,
                        "created_at": chunk[-1]["created_at"],
                        "source": "conversation_message_chunk",
                    })
                    if len(rows) >= safe_limit:
                        return rows
            return rows

    async def search_message_chunks(self, user_id: str, query: str,
                                    limit: int = 60) -> list[dict]:
        """Search query-centered windows across all saved messages for one user.

        Called only after the recall router selects ``episodic_recall``. SQL
        first narrows by the user's lexical clues, then reads bounded neighbors;
        it does not load all transcripts into the application or prompt.
        """
        from memory.entities import entity_search_terms
        from memory.history_retrieval import build_message_chunk_candidates

        user_id = _as_uuid(user_id)
        query = " ".join(str(query or "").split())[:1200]
        terms = entity_search_terms(query, max_terms=12)
        if not terms:
            return []
        safe_limit = max(1, min(int(limit), 120))
        seed_limit = min(safe_limit, 60)
        patterns = [f"%{term}%" for term in terms]
        clauses = [f"lower(m.content) LIKE ${index + 2}"
                   for index in range(len(patterns))]
        score_expr = " + ".join(
            f"CASE WHEN {clause} THEN 1 ELSE 0 END" for clause in clauses
        )
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""WITH matched AS (
                       SELECT m.user_id, m.conv_id, m.seq, m.created_at,
                              ({score_expr}) AS match_score
                       FROM conversation_messages m
                       WHERE m.user_id = $1::uuid
                         AND m.role IN ('user', 'assistant')
                         AND length(coalesce(m.content, '')) > 0
                         AND ({' OR '.join(clauses)})
                       ORDER BY match_score DESC, m.created_at DESC
                       LIMIT ${len(patterns) + 2}
                   )
                   SELECT matched.conv_id, matched.seq AS seed_seq,
                          matched.match_score, matched.created_at AS seed_created_at,
                          nearby.seq, nearby.role, nearby.content, nearby.created_at
                   FROM matched
                   JOIN conversation_messages nearby
                     ON nearby.user_id = matched.user_id
                    AND nearby.conv_id = matched.conv_id
                    AND nearby.seq BETWEEN matched.seq - 2 AND matched.seq + 3
                    AND nearby.role IN ('user', 'assistant')
                    AND length(coalesce(nearby.content, '')) > 0
                   ORDER BY matched.match_score DESC, matched.created_at DESC,
                            matched.conv_id, matched.seq, nearby.seq
                   LIMIT ${len(patterns) + 3}""",
                user_id, *patterns, seed_limit, seed_limit * 6,
            )
        return build_message_chunk_candidates(
            [dict(row) for row in rows], query, limit=safe_limit
        )

    async def list_recent_openings(self, user_id: str, limit: int = 5) -> list[dict]:
        """最近 N 盘各自的首条 AI 消息（plans/0916-09 开场白跨盘去重用）。

        开场取材源就几个，不去重会把"个性化"退化成复读（diag_archive_dupes
        实锤过同句跨 4 盘重复）。只取 AI 侧第一条：开场句式复读是我们要拦的，
        用户的话不计入。取不到（旧盘没有落库消息）就当没这盘。
        """
        user_id = _as_uuid(user_id)
        safe_limit = max(1, min(int(limit), 20))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            convs = await conn.fetch(
                """SELECT conv_id, max(created_at) AS latest
                   FROM conversation_messages
                   WHERE user_id = $1::uuid AND role IN ('user', 'assistant')
                     AND length(coalesce(content, '')) > 0
                   GROUP BY conv_id ORDER BY latest DESC LIMIT $2""",
                user_id, safe_limit,
            )
            openings: list[dict] = []
            for conv in convs:
                row = await conn.fetchrow(
                    """SELECT content, created_at FROM conversation_messages
                       WHERE user_id = $1::uuid AND conv_id = $2 AND role = 'assistant'
                         AND length(coalesce(content, '')) > 0
                       ORDER BY seq NULLS FIRST, created_at LIMIT 1""",
                    user_id, conv["conv_id"],
                )
                if row:
                    openings.append({
                        "conv_id": str(conv["conv_id"] or ""),
                        "opening": str(row["content"] or "").strip(),
                        "created_at": self._jsonable({"t": row["created_at"]})["t"],
                    })
            return openings

    async def conv_message_times(self, user_id: str, conv_id: str) -> list[dict]:
        """本盘每条落库消息的时间（plans/0916-23）：role/content/created_at，按 seq 升序。

        历史时间标签的数据源。前端送来的 history 只有 {role, content}（时间在传输层
        就没了），所以由后端按 conv_id 补一次——改这一处，浏览器与 APK 同时覆盖。
        按 seq 升序返回，调用方建索引时"后写覆盖"即最新值。
        """
        user_id = _as_uuid(user_id)
        if not str(conv_id or "").strip():
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT role, content, created_at FROM conversation_messages
                   WHERE user_id = $1::uuid AND conv_id = $2
                   ORDER BY seq ASC""",
                user_id, str(conv_id).strip(),
            )
        return [dict(r) for r in rows]

    async def conv_message_stats(self, user_id: str, conv_id: str) -> dict:
        """本盘消息统计（plans/0916-23 议题2 拆续盘阈值）：总条数 + 用户轮数。

        拆续盘判据以落库为准：代理每 ~55 秒重连一次，内存窗口每次只种入
        history 的最后 40 条，只看内存会把长盘判短、永远不提示。
        """
        user_id = _as_uuid(user_id)
        if not str(conv_id or "").strip():
            return {"messages": 0, "user_turns": 0}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT count(*) AS n,
                          count(*) FILTER (WHERE role = 'user') AS user_turns
                   FROM conversation_messages
                   WHERE user_id = $1::uuid AND conv_id = $2
                     AND role IN ('user', 'assistant')
                     AND length(coalesce(content, '')) > 0""",
                user_id, str(conv_id).strip(),
            )
        if not row:
            return {"messages": 0, "user_turns": 0}
        return {"messages": int(row["n"] or 0), "user_turns": int(row["user_turns"] or 0)}

    async def count_conv_messages(self, user_id: str, conv_id: str) -> int:
        """一盘已有多少条落库消息（plans/0916-09：非空盘不再发开场白）。

        opening-line 端点的服务端守卫：页面刷新会重置前端的一次性取钥匙，
        对着聊了一半的旧盘重发开场就是"重新开场"，违背 [Opening Action] ②。
        """
        user_id = _as_uuid(user_id)
        if not str(conv_id or "").strip():
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT count(*) AS n FROM conversation_messages
                   WHERE user_id = $1::uuid AND conv_id = $2
                     AND role IN ('user', 'assistant')
                     AND length(coalesce(content, '')) > 0""",
                user_id, str(conv_id).strip(),
            )
            return int(row["n"]) if row else 0

    async def last_conv_message_at(self, user_id: str, conv_id: str) -> str:
        """本盘最后一条落库消息的时间（ISO 字符串；没消息返回空串）。

        与 L2SQLiteMemory 方法面一致（plans/0916-16 修复项 2）。开场守卫靠它
        判断"是不是刚聊过 10 分钟内"：只按"有没有消息"判会把常驻老盘的开场永久掐掉。
        """
        user_id = _as_uuid(user_id)
        if not str(conv_id or "").strip():
            return ""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT max(created_at) AS latest FROM conversation_messages
                   WHERE user_id = $1::uuid AND conv_id = $2
                     AND role IN ('user', 'assistant')
                     AND length(coalesce(content, '')) > 0""",
                user_id, str(conv_id).strip(),
            )
        if not row or row["latest"] is None:
            return ""
        return str(self._jsonable({"t": row["latest"]})["t"])

    async def recent_conv_messages(self, user_id: str, conv_id: str,
                                   limit: int = 6) -> list[dict]:
        """Read a bounded same-tape dialogue window for a return opening."""
        user_id = _as_uuid(user_id)
        if not str(conv_id or "").strip():
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT role, content FROM conversation_messages
                   WHERE user_id = $1::uuid AND conv_id = $2
                     AND role IN ('user', 'assistant')
                     AND length(coalesce(content, '')) > 0
                   ORDER BY seq DESC LIMIT $3""",
                user_id, str(conv_id).strip(), max(1, min(int(limit), 12)),
            )
        return [dict(row) for row in reversed(rows)]

    async def save_session_archive(self, user_id: str, session_id: str, summary: str,
                                   error_count: int, cefr_before: str = "",
                                   cefr_after: str = "",
                                   summary_embedding: list | None = None) -> None:
        """写入一条会话归档摘要。

        `session_archives.session_id` 是 UUID 列，而 session_id 来自 conv_id
        （自由字符串）→ 用 _as_uuid 做确定性映射，同一个 conv_id 永远映到同一个 UUID，
        回读才不会错位。
        summary_embedding（plans/0915-06）：摘要的 BGE-M3 1024 维向量，供混合检索；
        None = 调用方没算出来，该行检索时退回纯稀疏。
        """
        user_id, session_id = _as_uuid(user_id), _as_uuid(session_id)
        pool = await self._get_pool()
        emb_literal = None
        if summary_embedding:
            try:
                emb_literal = "[" + ",".join(f"{float(x):.6f}" for x in summary_embedding) + "]"
            except (TypeError, ValueError):
                emb_literal = None
        await pool.execute(
            """
            INSERT INTO session_archives
                (user_id, session_id, summary, error_count, cefr_before, cefr_after,
                 summary_embedding, created_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::vector, NOW())
            ON CONFLICT (session_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                error_count = EXCLUDED.error_count,
                cefr_before = EXCLUDED.cefr_before,
                cefr_after = EXCLUDED.cefr_after,
                summary_embedding = COALESCE(EXCLUDED.summary_embedding,
                                             session_archives.summary_embedding)
            """,
            user_id, session_id, summary, int(error_count), cefr_before, cefr_after,
            emb_literal,
        )

    async def update_l1_profile(self, user_id: str, fields: dict) -> None:
        """把分析结果回写到 L1 画像（redis 后端下 L1 是 Redis）。"""
        from .l1_redis import L1RedisProfile
        await L1RedisProfile().update_profile(user_id, fields)

    async def get_style_parameters(self, user_id: str) -> dict:
        """读取 Loop Engineering 优化过的教学风格参数。"""
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM style_parameters WHERE user_id = $1::uuid", user_id
        )
        return self._jsonable(dict(row)) if row else {}

    async def set_style_parameter(self, user_id: str, param_name: str, value) -> None:
        """写入单个风格参数（列名走白名单；不存在则建行）。"""
        if param_name not in self._STYLE_COLUMNS:
            return
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        await pool.execute(
            f"""
            INSERT INTO style_parameters (user_id, {param_name}, version, updated_at)
            VALUES ($1::uuid, $2, 1, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                {param_name} = EXCLUDED.{param_name},
                version = style_parameters.version + 1,
                updated_at = NOW()
            """,
            user_id, value,
        )

    async def save_style_parameters(self, user_id: str, data: dict) -> bool:
        """整行 upsert 风格参数（只认白名单列），供 ParameterStore 使用。"""
        clean = {k: v for k, v in (data or {}).items() if k in self._STYLE_COLUMNS}
        if not clean:
            return False
        user_id = _as_uuid(user_id)
        columns = list(clean)
        col_sql = ", ".join(columns)
        val_sql = ", ".join(f"${i + 2}" for i in range(len(columns)))
        update_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        pool = await self._get_pool()
        await pool.execute(
            f"""
            INSERT INTO style_parameters (user_id, {col_sql}, updated_at)
            VALUES ($1::uuid, {val_sql}, NOW())
            ON CONFLICT (user_id) DO UPDATE SET {update_sql}, updated_at = NOW()
            """,
            user_id, *[clean[c] for c in columns],
        )
        return True

    # ── 跨会话共享记忆 ─────────────────────────────────────

    async def get_memory_context(self, user_id: str) -> dict:
        """返回该用户跨全部会话的共享记忆（错题/已掌握/会话数/近况/关系记忆）。"""
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            error_rows = await conn.fetch(
                """SELECT error_type, error_excerpt, count, weight
                   FROM error_fingerprints
                   WHERE user_id = $1::uuid AND weight > 0.1
                   ORDER BY weight DESC LIMIT 10""",
                user_id,
            )
            mastered_rows = await conn.fetch(
                """SELECT expression, confidence FROM mastered_expressions
                   WHERE user_id = $1::uuid ORDER BY confidence DESC LIMIT 20""",
                user_id,
            )
            total_sessions = await conn.fetchval(
                "SELECT COUNT(*) FROM session_archives WHERE user_id = $1::uuid", user_id
            )
            recent_rows = await conn.fetch(
                """SELECT session_id, summary, cefr_before, cefr_after, error_count, created_at
                   FROM session_archives
                   WHERE user_id = $1::uuid
                   ORDER BY created_at DESC LIMIT 5""",
                user_id,
            )
        relationship = await self.get_relationship_memory(user_id)
        return {
            "errors": [dict(r) for r in error_rows],
            "mastered": [dict(r) for r in mastered_rows],
            "total_sessions": int(total_sessions or 0),
            "recent_sessions": [self._jsonable(dict(r)) for r in recent_rows],
            "relationship": relationship,
        }

    # ── 会话（磁带）持久化 ──────────────────────────────────
    #
    # 写入策略与本地后端一致：**append-only**，只追加 seq > 当前最大值的新消息，
    # 这样前端重复 POST / 服务重启 / 竞态丢包都不会把旧消息覆盖掉。

    async def save_conversation(self, user_id: str, conv_id: str, name: str,
                                target_lang: str, level: str, scene: str,
                                messages: list[dict]) -> dict:
        from memory.message_identity import assistant_turn_id
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO conversation_meta
                        (user_id, conv_id, name, target_lang, level, scene, updated_at)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (user_id, conv_id) DO UPDATE SET
                        name = EXCLUDED.name, target_lang = EXCLUDED.target_lang,
                        level = EXCLUDED.level, scene = EXCLUDED.scene, updated_at = NOW()
                    """,
                    user_id, conv_id, name, target_lang, level, scene,
                )
                current = await conn.fetchval(
                    """SELECT MAX(seq) FROM conversation_messages
                       WHERE user_id = $1::uuid AND conv_id = $2""",
                    user_id, conv_id,
                )
                max_seq = -1 if current is None else int(current)
                inserted = 0
                for index, msg in enumerate(messages):
                    if index <= max_seq:
                        continue
                    await conn.execute(
                        """
                        INSERT INTO conversation_messages
                            (user_id, conv_id, role, content, seq, created_at, turn_id)
                        VALUES ($1::uuid, $2, $3, $4, $5, COALESCE($6, NOW()), $7)
                        ON CONFLICT (user_id, conv_id, seq) DO NOTHING
                        """,
                        user_id, conv_id, str(msg.get("role", "user")),
                        str(msg.get("content", "")), index,
                        self._coerce_ts(msg.get("timestamp") or msg.get("created_at")),
                        assistant_turn_id(msg),
                    )
                    inserted += 1
        return {"inserted": inserted, "max_seq": max_seq}

    async def load_conversations(self, user_id: str, include_hidden: bool = False) -> list[dict]:
        """加载用户的会话列表。

        `include_hidden` 与本地后端一样**不参与过滤**（本地那两段 SQL 完全相同），
        保持行为一致；要看被隐藏的会话请用 load_hidden_conversations()。
        """
        return await self._load_conversation_rows(user_id, hidden=False)

    async def load_hidden_conversations(self, user_id: str) -> list[dict]:
        """加载被隐藏（软删除）但仍在保留期内的会话，供"可恢复"面板使用。"""
        return await self._load_conversation_rows(user_id, hidden=True)

    async def _load_conversation_rows(self, user_id: str, hidden: bool) -> list[dict]:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        if hidden:
            clause, order = "hidden_at IS NOT NULL", "hidden_at DESC"
        else:
            clause, order = "hidden_at IS NULL", "updated_at DESC"
        async with pool.acquire() as conn:
            meta_rows = await conn.fetch(
                f"""SELECT conv_id, name, target_lang, level, scene, created_at,
                           updated_at, hidden_at, expires_at
                    FROM conversation_meta
                    WHERE user_id = $1::uuid AND {clause}
                    ORDER BY {order}""",
                user_id,
            )
            result = []
            for meta in meta_rows:
                conv = self._jsonable(dict(meta))
                msg_rows = await conn.fetch(
                    """SELECT role, content, created_at, turn_id FROM conversation_messages
                       WHERE user_id = $1::uuid AND conv_id = $2 ORDER BY seq ASC""",
                    user_id, meta["conv_id"],
                )
                conv["messages"] = [
                    {
                        "role": r["role"],
                        "content": r["content"],
                        "turn_id": r["turn_id"],
                        "timestamp": self._jsonable({"t": r["created_at"]})["t"],
                        "created_at": self._jsonable({"t": r["created_at"]})["t"],
                    }
                    for r in msg_rows
                ]
                result.append(conv)
            return result

    # ── 删除一盘会话（回收站 / 彻底删除）────────────────────
    #
    # 删一盘 = 删一个 conv_id（前端一盘磁带最多两个：{tapeId}_A 与 {tapeId}_B）。
    #
    # 必须同时清掉「派生记忆」，否则删完 AI 还能把它想起来 —— 用户的原话是
    # 「磁带显示就要确实是删掉了那部分数据」。真正让 AI 记得一盘会话的不是
    # conversation_messages（那是原文，只用于恢复），而是这三样：
    #   - session_archives  : 摘要 + 向量，跨盘检索和「上次聊了什么」全靠它
    #   - memory_entities   : 从这盘抽出来的人/地/事
    #   - cassette_handoffs : 显式续盘时递给下一盘的交班条
    #
    # 键形态坑：session_archives.session_id 是 UUID 列，写侧走 _as_uuid(conv_id)
    # 的确定性映射；删的时候必须同样映射一次，否则删的是另一个 UUID，一行都删不掉
    # （而且不报错，是静默 0 行）。另外两张表直接用 conv_id 文本。

    _CONV_DERIVED_TABLES = ("memory_entity_relations", "memory_entities", "cassette_handoffs")

    async def _purge_conv_derived(self, conn, user_id, conv_id: str) -> dict:
        """清掉一个会话派生出来的记忆（摘要/实体/交班条）。幂等。"""
        counts = {}
        for table in self._CONV_DERIVED_TABLES:
            result = await conn.execute(
                f"DELETE FROM {table} WHERE user_id = $1::uuid AND conv_id = $2",
                user_id, conv_id,
            )
            counts[table] = int(str(result).split()[-1])
        result = await conn.execute(
            "DELETE FROM session_archives WHERE user_id = $1::uuid AND session_id = $2::uuid",
            user_id, _as_uuid(conv_id),
        )
        counts["session_archives"] = int(str(result).split()[-1])
        # plans/0923-06 §4.2.4／§5 第 12 条：练习盘会话末的五维冷评估会把证据经
        # `ProfileMemory` 写进统一画像③。隐藏／彻底删这一盘时**必须沿来源撤销**——
        # 否则删掉那盘的薄弱项还留在画像里，下一盘照样据它定焦点（"旧薄弱点复活"）。
        # ⚠ 列名是 `source_conv_id`（不是 `conv_id`），所以**不能**直接塞进
        # `_CONV_DERIVED_TABLES`（那个循环按 `conv_id` 拼 SQL，加进去会整条报错）。
        # 其它盘不往本表写 ⇒ 对闲聊／素材／场景盘零影响；缺表（老库未迁）不抛错。
        try:
            result = await conn.execute(
                "DELETE FROM learner_profile_facts "
                "WHERE user_id = $1::uuid AND source_conv_id = $2",
                user_id, conv_id,
            )
            counts["learner_profile_facts"] = int(str(result).split()[-1])
        except Exception:
            counts["learner_profile_facts"] = 0
        # plans/0923-04 §4.4 阶段 2：这一盘的写入令牌一并撤销 —— 否则"已登记令牌、
        # 还没落库"的迟到作业会在删盘之后把该盘的画像/关系条目重新写回来
        # （§4.5「阻止旧来源重扫复活」）。按 scope_conv_id 精确撤销，别的盘零影响；
        # 缺表（老库未迁）不抛错。**只置 revoked、不删行**：删盘不是全删，
        # 留着行才能让"已完成但被拒"的作业有据可查，全删时才会物理清空。
        try:
            result = await conn.execute(
                "UPDATE learner_profile_write_leases SET status = 'revoked' "
                "WHERE user_id = $1::uuid AND status = 'active' AND scope_conv_id = $2",
                user_id, conv_id,
            )
            counts["write_leases_revoked"] = int(str(result).split()[-1])
        except Exception:
            counts["write_leases_revoked"] = 0
        return counts

    async def hide_conversation(self, user_id: str, conv_id: str,
                                retention_days: int = 30) -> dict:
        """软删除（进回收站）：标记 hidden_at，并**立刻**清掉派生记忆。

        留下的只有 conversation_meta + conversation_messages —— 也就是这一盘的
        原文，专门留给 retention_days 内的「恢复」。恢复回来的是录音和文字，
        跨盘认知要重新长出来（这是回收站方案的固有代价，界面上要如实说明）。
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """UPDATE conversation_meta
                       SET hidden_at = NOW(),
                           expires_at = NOW() + make_interval(days => $3::int)
                       WHERE user_id = $1::uuid AND conv_id = $2""",
                    user_id, conv_id, int(retention_days),
                )
                existed = int(str(result).split()[-1]) > 0
                dropped = await self._purge_conv_derived(conn, user_id, conv_id)
                try:
                    row = await conn.fetchrow(
                        "SELECT memory_items FROM relationship_memory WHERE user_id=$1::uuid FOR UPDATE",
                        user_id,
                    )
                    raw_items = row["memory_items"] if row else []
                    items = json.loads(raw_items) if isinstance(raw_items, str) else list(raw_items or [])
                    hidden_count = 0
                    for item in items:
                        if (isinstance(item, dict)
                                and str(item.get("source_conv_id") or "") == str(conv_id)
                                and not item.get("hidden_at")):
                            item["hidden_at"] = datetime.now(timezone.utc).isoformat()
                            hidden_count += 1
                    if hidden_count:
                        await conn.execute(
                            "UPDATE relationship_memory SET memory_items=$2::jsonb, updated_at=NOW() WHERE user_id=$1::uuid",
                            user_id, json.dumps(items, ensure_ascii=False),
                        )
                    dropped["relationship_memory_items_hidden"] = hidden_count
                except Exception:
                    dropped["relationship_memory_items_hidden"] = 0
                persona_hidden = await conn.execute(
                    "UPDATE companion_persona_facts SET hidden_at=NOW() "
                    "WHERE user_id=$1::uuid AND source_conv_id=$2",
                    user_id, conv_id,
                )
                dropped["companion_persona_facts_hidden"] = int(str(persona_hidden).split()[-1])
                await conn.execute(
                    """UPDATE companion_persona_snapshots
                       SET narrative='', supporting_fact_ids='[]'::jsonb,
                           snapshot_version='', source_hash='', generated_at=NULL
                       WHERE user_id=$1::uuid AND persona_key='alice'""", user_id,
                )
        return {"status": "hidden", "conv_id": conv_id, "existed": existed,
                "retention_days": int(retention_days), "dropped": dropped}

    async def restore_conversation(self, user_id: str, conv_id: str) -> dict:
        """从回收站恢复：只把隐藏标记摘掉，原文原样回来。"""
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        result = await pool.execute(
            """UPDATE conversation_meta SET hidden_at = NULL, expires_at = NULL
               WHERE user_id = $1::uuid AND conv_id = $2""",
            user_id, conv_id,
        )
        async with pool.acquire() as conn:
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT memory_items FROM relationship_memory WHERE user_id=$1::uuid FOR UPDATE",
                        user_id,
                    )
                    raw_items = row["memory_items"] if row else []
                    items = json.loads(raw_items) if isinstance(raw_items, str) else list(raw_items or [])
                    restored = 0
                    for item in items:
                        if (isinstance(item, dict)
                                and str(item.get("source_conv_id") or "") == str(conv_id)
                                and item.pop("hidden_at", None)):
                            restored += 1
                    if restored:
                        await conn.execute(
                            "UPDATE relationship_memory SET memory_items=$2::jsonb, updated_at=NOW() WHERE user_id=$1::uuid",
                            user_id, json.dumps(items, ensure_ascii=False),
                        )
            except Exception:
                pass
            await conn.execute(
                "UPDATE companion_persona_facts SET hidden_at=NULL "
                "WHERE user_id=$1::uuid AND source_conv_id=$2", user_id, conv_id,
            )
            await conn.execute(
                """UPDATE companion_persona_snapshots
                   SET narrative='', supporting_fact_ids='[]'::jsonb,
                       snapshot_version='', source_hash='', generated_at=NULL
                   WHERE user_id=$1::uuid AND persona_key='alice'""", user_id,
            )
        return {"status": "restored", "conv_id": conv_id,
                "existed": int(str(result).split()[-1]) > 0,
                "derived_memory": "dropped_on_hide"}

    async def purge_conversation(self, user_id: str, conv_id: str) -> dict:
        """物理删除一盘会话的全部行（不可恢复，幂等）。

        调用方必须先拿到用户的明确确认（/conversations/.../purge 要求
        confirmation 字面量）。
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        deleted = {}
        async with pool.acquire() as conn:
            async with conn.transaction():
                deleted.update(await self._purge_conv_derived(conn, user_id, conv_id))
                persona_deleted = await conn.execute(
                    "DELETE FROM companion_persona_facts WHERE user_id=$1::uuid AND source_conv_id=$2",
                    user_id, conv_id,
                )
                deleted["companion_persona_facts"] = int(str(persona_deleted).split()[-1])
                await conn.execute(
                    """UPDATE companion_persona_snapshots
                       SET narrative='', supporting_fact_ids='[]'::jsonb,
                           snapshot_version='', source_hash='', generated_at=NULL
                       WHERE user_id=$1::uuid AND persona_key='alice'""", user_id,
                )
                # plans/0923-08 §4.2：素材盘成盘记录（该盘的提要/分组/话题快照）随**彻底删**
                # 一起清；**不在 hide 里清** —— 隐藏后恢复要能重读盘面，新构建会换 conv_id。
                for table in ("conversation_messages", "conversation_meta",
                              "expression_topic_assignments", "material_tape_builds"):
                    result = await conn.execute(
                        f"DELETE FROM {table} WHERE user_id = $1::uuid AND conv_id = $2",
                        user_id, conv_id,
                    )
                    deleted[table] = int(str(result).split()[-1])
        return {"conv_id": conv_id, "deleted": deleted,
                "rows": sum(deleted.values())}

    async def purge_expired_conversations(self, user_id: str = None) -> dict:
        """物理删除已过期的隐藏会话，返回 {deleted, purged_conv_ids}。

        比本地后端多删了一步 conversation_messages —— 否则留下的就是
        「永久删不掉的用户对话原文」，与隐私承诺相冲。派生记忆在这里再清一次
        是幂等兜底（隐藏时已清过），但原文删除只在这里发生。

        purged_conv_ids 交回调用方，用来删掉 L3 磁盘上的原始转写 ——
        数据库清了而磁盘还留着，等于没删干净。
        """
        pool = await self._get_pool()
        scope = "AND user_id = $1::uuid" if user_id else ""
        args = [_as_uuid(user_id)] if user_id else []
        purged: list[str] = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                expired = await conn.fetch(
                    f"""SELECT user_id, conv_id FROM conversation_meta
                        WHERE hidden_at IS NOT NULL AND expires_at < NOW() {scope}""",
                    *args,
                )
                for row in expired:
                    await self._purge_conv_derived(conn, row["user_id"], row["conv_id"])
                    await conn.execute(
                        "DELETE FROM companion_persona_facts "
                        "WHERE user_id=$1::uuid AND source_conv_id=$2",
                        row["user_id"], row["conv_id"],
                    )
                    await conn.execute(
                        """UPDATE companion_persona_snapshots
                           SET narrative='', supporting_fact_ids='[]'::jsonb,
                               snapshot_version='', source_hash='', generated_at=NULL
                           WHERE user_id=$1::uuid AND persona_key='alice'""",
                        row["user_id"],
                    )
                    for table in ("conversation_messages", "conversation_meta",
                                  "expression_topic_assignments", "material_tape_builds"):
                        await conn.execute(
                            f"""DELETE FROM {table}
                                WHERE user_id = $1::uuid AND conv_id = $2""",
                            row["user_id"], row["conv_id"],
                        )
                    purged.append(row["conv_id"])
        return {"deleted": len(purged), "purged_conv_ids": purged}

    # ── 练习盘话题分配（plans/0923-06 §4.1.2）──────────────────────
    #
    # 只服务练习盘（scene='expression'）。存建盘**当时的来源快照**，故后续扩库／改文案／
    # 热点过期都不会改写旧盘的出处。**故意不放进 hide 的派生清理**：隐藏盘要能在恢复后
    # 重新读到原话题快照（§4.4）；隐藏期间靠读取侧的 hidden 过滤保证不可见。
    # 彻底删（purge_conversation / purge_expired_conversations）会删掉本表行。

    async def put_expression_topic_assignment(self, user_id: str, conv_id: str,
                                              assignment: dict) -> dict:
        """写入一盘的建盘快照。同一 (user_id, conv_id) **幂等且不覆写**。

        `ON CONFLICT DO NOTHING` 是「同盘重试读取原分配、不重新抽题」的落点：
        已分配过的盘再调只会得到 `exists`，原出处不被后来的抽题改掉（§4.1.2）。
        """
        data = assignment or {}
        uid = _as_uuid(user_id)
        # asyncpg 不接受 ISO **字符串**去填 timestamptz（`DataError: expected a
        # datetime…`）——SQL 里的 `::timestamptz` 救不了它，编码发生在 cast 之前。
        # 所以这里把字符串解析成 datetime；解析不出来就让 DB 走 NOW() 兜底。
        raw_at = data.get("assigned_at")
        assigned_at = raw_at if hasattr(raw_at, "isoformat") else None
        if assigned_at is None and str(raw_at or "").strip():
            try:
                assigned_at = datetime.fromisoformat(str(raw_at).strip())
            except ValueError:
                assigned_at = None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """INSERT INTO expression_topic_assignments
                       (user_id, conv_id, topic_id, source_title, source_url,
                        source_date, prompt_text, assigned_at)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6, $7,
                           COALESCE($8::timestamptz, NOW()))
                   ON CONFLICT (user_id, conv_id) DO NOTHING""",
                uid, conv_id,
                str(data.get("topic_id") or ""),
                str(data.get("source_title") or ""),
                str(data.get("source_url") or ""),
                str(data.get("source_date") or ""),
                str(data.get("prompt_text") or ""),
                assigned_at,
            )
        inserted = int(str(result).split()[-1]) > 0
        return {"status": "assigned" if inserted else "exists",
                "conv_id": conv_id, "inserted": inserted}

    async def get_expression_topic_assignment(self, user_id: str, conv_id: str,
                                              include_hidden: bool = False) -> dict:
        """读一盘的分配快照。隐藏盘默认**不可见**（§4.4），恢复后才可见。"""
        uid = _as_uuid(user_id)
        hidden_clause = "" if include_hidden else "AND m.hidden_at IS NULL"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT a.conv_id, a.topic_id, a.source_title, a.source_url,
                           a.source_date, a.prompt_text, a.assigned_at
                    FROM expression_topic_assignments a
                    LEFT JOIN conversation_meta m
                           ON m.user_id = a.user_id AND m.conv_id = a.conv_id
                    WHERE a.user_id = $1::uuid AND a.conv_id = $2 {hidden_clause}""",
                uid, conv_id,
            )
        return self._jsonable(dict(row)) if row else {}

    async def list_recent_expression_topic_assignments(self, user_id: str,
                                                       limit: int = 5,
                                                       include_hidden: bool = True) -> list:
        """最近若干盘的分配（新盘据此排除重复的 topic_id，§4.1.2）。"""
        try:
            n = max(1, int(limit))
        except (TypeError, ValueError):
            n = 5
        uid = _as_uuid(user_id)
        hidden_clause = "" if include_hidden else "AND m.hidden_at IS NULL"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT a.conv_id, a.topic_id, a.assigned_at
                    FROM expression_topic_assignments a
                    LEFT JOIN conversation_meta m
                           ON m.user_id = a.user_id AND m.conv_id = a.conv_id
                    WHERE a.user_id = $1::uuid {hidden_clause}
                    ORDER BY a.assigned_at DESC, a.conv_id DESC
                    LIMIT $2""",
                uid, n,
            )
        return [self._jsonable(dict(r)) for r in rows]

    # ── 隐私：导出 / 删除 ───────────────────────────────────

    async def export_user_data(self, user_id: str) -> dict:
        """导出该学习者名下的全部记录（只读，不改动任何数据）。"""
        uid, raw = _as_uuid(user_id), str(user_id)
        pool = await self._get_pool()
        exported = {}
        async with pool.acquire() as conn:
            for key, table, kind in self._USER_TABLES:
                try:
                    if kind == "text":
                        rows = await conn.fetch(
                            f"SELECT * FROM {table} WHERE user_id = ANY($1::text[])", [raw, uid]
                        )
                    else:
                        rows = await conn.fetch(
                            f"SELECT * FROM {table} WHERE user_id = $1::uuid", uid
                        )
                    exported[key] = [self._jsonable(dict(r)) for r in rows]
                except Exception:
                    # 表缺失/列不匹配都不该让整次导出失败
                    exported[key] = []
        return exported

    async def delete_user_data(self, user_id: str) -> dict:
        """删除该学习者名下的全部记录（返回 {表名: 删除行数}）。

        调用方必须先拿到用户的明确确认（/privacy/data 要求 confirmation 字面量）。
        """
        uid, raw = _as_uuid(user_id), str(user_id)
        pool = await self._get_pool()
        deleted = {}
        async with pool.acquire() as conn:
            for _, table, kind in self._USER_TABLES:
                try:
                    if kind == "text":
                        result = await conn.execute(
                            f"DELETE FROM {table} WHERE user_id = ANY($1::text[])", [raw, uid]
                        )
                    else:
                        result = await conn.execute(
                            f"DELETE FROM {table} WHERE user_id = $1::uuid", uid
                        )
                    deleted[table] = int(str(result).split()[-1])
                except Exception:
                    deleted[table] = 0
        return deleted

    # ── 素材（plans/0916-21 D4/F3：粘贴/日记 + 网页链接 → 一盘能聊的素材带）──────
    # 方法面与 L2SQLiteMemory **必须一致**；blocks 是 jsonb，读写一律走
    # loads_json_list —— asyncpg 返回 str，isinstance(col, list) 在本后端永假
    # （0916-11c 真机冒烟抓到的"追加变整列覆盖"事故）。

    def _material_row(self, row, with_text: bool = True) -> dict:
        """行 → dict。with_text=False 用于列表（不回传正文，少一分泄露面）。"""
        if not row:
            return {}
        raw = dict(row)
        raw.pop("user_id", None)
        if with_text:
            from engine.chunk_evidence import loads_json_list
            raw["blocks"] = loads_json_list(raw.get("blocks"))
            raw["raw_text"] = str(raw.get("raw_text") or "")
        else:
            raw.pop("raw_text", None)
            raw.pop("blocks", None)
        # 深析产物（plans/0916-24）：JSONB → dict；坏数据不能把整行读挂
        try:
            parsed = raw.get("analysis")
            if isinstance(parsed, str):
                parsed = json.loads(parsed or "{}")
            raw["analysis"] = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            raw["analysis"] = {}
        return self._jsonable(raw)

    async def add_material(self, user_id: str, kind: str, title: str,
                           source_ref: str = "", raw_text: str = "",
                           summary: str = "", char_count: int = 0,
                           status: str = "ready", error: str = "",
                           blocks: list = None,
                           material_conv_id: str = "",
                           analysis: dict = None) -> int:
        """落一条素材，返回新行 id。

        status='failed' 的行**必须**留 material_conv_id 为空：失败不建带（§7 底线），
        只留一行明确原因给前端提示，绝不产出一盘空素材带。

        analysis（plans/0916-24）：深度分析产物（六字段框架，缺项空项正常）。
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            new_id = await conn.fetchval(
                """INSERT INTO material_items
                   (user_id, kind, title, source_ref, raw_text, summary, char_count,
                    material_conv_id, status, error, blocks, analysis, created_at, updated_at)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                           $11::jsonb, $12::jsonb, NOW(), NOW())
                   RETURNING id""",
                user_id, str(kind or "text"), str(title or ""), str(source_ref or ""),
                str(raw_text or ""), str(summary or ""), int(char_count or 0),
                str(material_conv_id or ""), str(status or "ready"), str(error or ""),
                json.dumps(list(blocks or []), ensure_ascii=False),
                json.dumps(dict(analysis or {}), ensure_ascii=False),
            )
            return int(new_id)

    async def list_materials(self, user_id: str, kind: str = "",
                             limit: int = 20, with_text: bool = False) -> list:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if str(kind or "").strip():
                rows = await conn.fetch(
                    """SELECT * FROM material_items
                       WHERE user_id = $1::uuid AND kind = $2
                       ORDER BY created_at DESC, id DESC LIMIT $3""",
                    user_id, str(kind).strip(), int(limit),
                )
            else:
                rows = await conn.fetch(
                    """SELECT * FROM material_items WHERE user_id = $1::uuid
                       ORDER BY created_at DESC, id DESC LIMIT $2""",
                    user_id, int(limit),
                )
        return [self._material_row(r, with_text=with_text) for r in rows]

    async def get_material(self, user_id: str, material_id,
                           with_text: bool = True) -> dict:
        user_id = _as_uuid(user_id)
        try:
            mid = int(str(material_id).strip())
        except (ValueError, TypeError):
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM material_items WHERE user_id = $1::uuid AND id = $2",
                user_id, mid,
            )
        return self._material_row(row, with_text=with_text)

    async def get_material_by_conv(self, user_id: str, conv_id: str) -> dict:
        """按"它作为一盘带的 conv_id"取素材（F4：素材带进会话时注入 [Material]）。"""
        user_id = _as_uuid(user_id)
        conv = str(conv_id or "").strip()
        if not conv:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM material_items
                   WHERE user_id = $1::uuid AND material_conv_id = $2
                   ORDER BY id DESC LIMIT 1""",
                user_id, conv,
            )
        return self._material_row(row)

    async def latest_diary_material(self, user_id: str,
                                    within_hours: int = 72) -> dict:
        """最近一篇日记级素材（F1 四级取材第 1 级）。超期/没有 → {}。

        窗口用服务器时间算（make_interval），避免 Python/DB 时基不一致。
        """
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM material_items
                   WHERE user_id = $1::uuid AND kind = 'diary' AND status = 'ready'
                     AND created_at >= NOW() - make_interval(hours => $2::int)
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                user_id, int(within_hours),
            )
        return self._material_row(row, with_text=False)

    async def delete_material(self, user_id: str, material_id) -> bool:
        """删一条素材（界面入口是 V1.1，方法面先就位）。"""
        user_id = _as_uuid(user_id)
        try:
            mid = int(str(material_id).strip())
        except (ValueError, TypeError):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM material_items WHERE user_id = $1::uuid AND id = $2",
                user_id, mid,
            )
        return str(result).endswith("1")

    # ── 素材库（plans/0923-08）：先入库、选材后成盘 ────────────────────────
    # 方法面与 L2SQLiteMemory **必须一致**（同名同参数同返回键）；列表列在本后端
    # 是 jsonb，读取一律 loads_json_list（asyncpg 返回 str，isinstance 恒假）。

    def _tape_build_row(self, row) -> dict:
        """成盘构建行 → dict（四个 JSON 列统一归一成 list）。"""
        if not row:
            return {}
        from engine.chunk_evidence import loads_json_list
        raw = dict(row)
        raw.pop("user_id", None)
        for key in ("source_ids", "groups", "hooks", "top_chunks"):
            raw[key] = loads_json_list(raw.get(key))
        return self._jsonable(raw)

    async def list_material_library_page(self, user_id: str, cursor: str = "",
                                         section: str = "", limit: int = 20) -> dict:
        """素材库分页：稳定游标 = 上一页最后一条 id（倒序扫描）。

        section: '' 全部 / 'material' 一般素材（非日记）/ 'diary' 日记。
        返回 {"items": [...], "next_cursor": str, "has_more": bool}——
        items 不回 raw_text（少一分泄露面），形状与 SQLite 侧一致。
        """
        user_id = _as_uuid(user_id)
        try:
            before = int(str(cursor).strip()) if str(cursor or "").strip() else 0
        except (ValueError, TypeError):
            before = 0
        n = max(1, min(int(limit or 20), 100))
        where = "user_id = $1::uuid"
        args: list = [user_id]
        if str(section or "").strip() == "material":
            where += " AND kind <> 'diary'"
        elif str(section or "").strip() == "diary":
            where += " AND kind = 'diary'"
        if before:
            args.append(before)
            where += f" AND id < ${len(args)}"
        args.append(n + 1)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, kind, title, source_ref, summary, char_count,
                           material_conv_id, status, error, created_at, updated_at
                    FROM material_items WHERE {where}
                    ORDER BY id DESC LIMIT ${len(args)}""", *args)
        items = [self._material_row(r, with_text=False) for r in rows]
        has_more = len(items) > n
        items = items[:n]
        return {
            "items": items,
            "next_cursor": str(items[-1]["id"]) if (items and has_more) else "",
            "has_more": has_more,
        }

    async def put_material_source_blocks(self, user_id: str, material_id,
                                         blocks: list) -> int:
        """整份覆盖写入某素材的全量正文/转写块，返回写入块数。"""
        user_id = _as_uuid(user_id)
        mid = int(str(material_id).strip())
        rows = []
        for index, block in enumerate(list(blocks or [])):
            if isinstance(block, dict):
                text = str(block.get("text") or "")
                span = str(block.get("span_kind") or "body")
                no = block.get("block_no")
            else:
                text, span, no = str(block or ""), "body", None
            try:
                no = int(no) if no is not None else index + 1
            except (ValueError, TypeError):
                no = index + 1
            rows.append((no, span, text))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM material_source_blocks "
                    "WHERE user_id = $1::uuid AND material_id = $2", user_id, mid)
                for no, span, text in rows:
                    await conn.execute(
                        """INSERT INTO material_source_blocks
                               (user_id, material_id, block_no, span_kind, text, created_at)
                           VALUES ($1::uuid, $2, $3, $4, $5, NOW())
                           ON CONFLICT (user_id, material_id, block_no) DO UPDATE SET
                               span_kind = EXCLUDED.span_kind, text = EXCLUDED.text""",
                        user_id, mid, no, span, text)
        return len(rows)

    async def list_material_source_blocks(self, user_id: str, material_ids,
                                          limit_per_material: int = 0) -> list:
        """取若干素材的全文块（按素材、块号升序）。material_ids 传 int 或列表。
        limit_per_material<=0 = 每份取全部。返回行带 material_id。"""
        user_id = _as_uuid(user_id)
        if isinstance(material_ids, (list, tuple, set)):
            ids = []
            for value in material_ids:
                try:
                    ids.append(int(str(value).strip()))
                except (ValueError, TypeError):
                    continue
        else:
            try:
                ids = [int(str(material_ids).strip())]
            except (ValueError, TypeError):
                return []
        if not ids:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if int(limit_per_material or 0) > 0:
                rows = await conn.fetch(
                    """SELECT material_id, block_no, span_kind, text
                       FROM (
                           SELECT material_id, block_no, span_kind, text,
                                  ROW_NUMBER() OVER (PARTITION BY material_id
                                                     ORDER BY block_no ASC) AS rn
                           FROM material_source_blocks
                           WHERE user_id = $1::uuid AND material_id = ANY($2::bigint[])
                       ) t WHERE rn <= $3
                       ORDER BY material_id ASC, block_no ASC""",
                    user_id, ids, int(limit_per_material))
            else:
                rows = await conn.fetch(
                    """SELECT material_id, block_no, span_kind, text
                       FROM material_source_blocks
                       WHERE user_id = $1::uuid AND material_id = ANY($2::bigint[])
                       ORDER BY material_id ASC, block_no ASC""", user_id, ids)
        return [{"material_id": int(r["material_id"]), "block_no": int(r["block_no"]),
                 "span_kind": str(r["span_kind"] or "body"),
                 "text": str(r["text"] or "")} for r in rows]

    async def create_material_tape_build(self, user_id: str, request_id: str,
                                         source_ids: list, status: str = "pending",
                                         error: str = "") -> dict:
        """建一条成盘构建记录（request_id 对同一用户唯一，重放回原行）。"""
        user_id = _as_uuid(user_id)
        req = str(request_id or "").strip()
        if not req:
            return {}
        ids = []
        for value in list(source_ids or []):
            try:
                ids.append(int(str(value).strip()))
            except (ValueError, TypeError):
                continue
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO material_tape_builds
                       (user_id, request_id, status, source_ids, error, created_at, updated_at)
                   VALUES ($1::uuid, $2, $3, $4::jsonb, $5, NOW(), NOW())
                   ON CONFLICT (user_id, request_id) DO NOTHING""",
                user_id, req, str(status or "pending"),
                json.dumps(ids, ensure_ascii=False), str(error or ""))
            row = await conn.fetchrow(
                "SELECT * FROM material_tape_builds WHERE user_id = $1::uuid AND request_id = $2",
                user_id, req)
        return self._tape_build_row(row)

    async def get_material_tape_build(self, user_id: str, build_id=0,
                                      request_id: str = "", conv_id: str = "") -> dict:
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if str(conv_id or "").strip():
                row = await conn.fetchrow(
                    "SELECT * FROM material_tape_builds WHERE user_id = $1::uuid AND conv_id = $2 "
                    "ORDER BY id DESC LIMIT 1", user_id, str(conv_id).strip())
            elif str(request_id or "").strip():
                row = await conn.fetchrow(
                    "SELECT * FROM material_tape_builds WHERE user_id = $1::uuid AND request_id = $2",
                    user_id, str(request_id).strip())
            else:
                try:
                    bid = int(str(build_id).strip())
                except (ValueError, TypeError):
                    return {}
                row = await conn.fetchrow(
                    "SELECT * FROM material_tape_builds WHERE user_id = $1::uuid AND id = $2",
                    user_id, bid)
        return self._tape_build_row(row)

    async def finish_material_tape_build(self, user_id: str, build_id,
                                         conv_id: str, name: str = "",
                                         summary: str = "", groups: list = None,
                                         hooks: list = None,
                                         top_chunks: list = None) -> bool:
        """一次事务写两处：零消息 scene='material' 的 conversation_meta + 构建行置 ready。

        两写必须同事务（方案 §4.2）：否则可能留下"柜里有盘、构建还 pending"或
        "构建 ready、柜里没盘"的半状态。
        """
        user_id = _as_uuid(user_id)
        try:
            bid = int(str(build_id).strip())
        except (ValueError, TypeError):
            return False
        conv = str(conv_id or "").strip()
        if not conv:
            return False
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO conversation_meta
                           (user_id, conv_id, name, target_lang, level, scene, updated_at)
                       VALUES ($1::uuid, $2, $3, 'English', '', 'material', NOW())
                       ON CONFLICT (user_id, conv_id) DO UPDATE SET
                           name = EXCLUDED.name, scene = 'material', updated_at = NOW()""",
                    user_id, conv, str(name or ""))
                result = await conn.execute(
                    """UPDATE material_tape_builds SET
                           status = 'ready', conv_id = $3, summary = $4,
                           groups = $5::jsonb, hooks = $6::jsonb, top_chunks = $7::jsonb,
                           error = '', updated_at = NOW()
                       WHERE user_id = $1::uuid AND id = $2""",
                    user_id, bid, conv, str(summary or ""),
                    json.dumps(list(groups or []), ensure_ascii=False),
                    json.dumps(list(hooks or []), ensure_ascii=False),
                    json.dumps(list(top_chunks or []), ensure_ascii=False))
        return str(result).endswith("1")

    async def fail_material_tape_build(self, user_id: str, build_id,
                                       error: str = "") -> bool:
        user_id = _as_uuid(user_id)
        try:
            bid = int(str(build_id).strip())
        except (ValueError, TypeError):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE material_tape_builds SET status = 'failed', error = $3, updated_at = NOW() "
                "WHERE user_id = $1::uuid AND id = $2",
                user_id, bid, str(error or "")[:400])
        return str(result).endswith("1")

    async def get_conversation_scene(self, user_id: str, conv_id: str) -> str:
        """按 (user_id, conv_id) 精确取盘型——会话热入口不许扫整柜。"""
        conv = str(conv_id or "").strip()
        if not conv:
            return ""
        user_id = _as_uuid(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT scene FROM conversation_meta WHERE user_id = $1::uuid AND conv_id = $2 "
                "ORDER BY id DESC LIMIT 1", user_id, conv)
        return str(value or "")

    async def mark_material_ready(self, user_id: str, material_id, *,
                                  title: str = "", summary: str = "",
                                  char_count: int = 0, blocks: list = None,
                                  analysis: dict = None) -> bool:
        """轮外分析完成后把库条置 ready（标题/摘要/块一次性补齐）。"""
        user_id = _as_uuid(user_id)
        try:
            mid = int(str(material_id).strip())
        except (ValueError, TypeError):
            return False
        sets = ["status = 'ready'", "error = ''", "updated_at = NOW()"]
        args: list = [user_id, mid]
        if str(title or "").strip():
            args.append(str(title).strip())
            sets.append(f"title = ${len(args)}")
        if str(summary or "").strip():
            args.append(str(summary))
            sets.append(f"summary = ${len(args)}")
        if int(char_count or 0) > 0:
            args.append(int(char_count))
            sets.append(f"char_count = ${len(args)}")
        if blocks is not None:
            args.append(json.dumps(list(blocks or []), ensure_ascii=False))
            sets.append(f"blocks = ${len(args)}::jsonb")
        if analysis is not None:
            args.append(json.dumps(dict(analysis or {}), ensure_ascii=False))
            sets.append(f"analysis = ${len(args)}::jsonb")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE material_items SET {', '.join(sets)} "
                "WHERE user_id = $1::uuid AND id = $2", *args)
        return str(result).endswith("1")

    async def mark_material_failed(self, user_id: str, material_id,
                                   error: str = "") -> bool:
        """轮外阶段失败：明确置 failed + 原因，条目不可选入盘（绝不产空带）。"""
        user_id = _as_uuid(user_id)
        try:
            mid = int(str(material_id).strip())
        except (ValueError, TypeError):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE material_items SET status = 'failed', error = $3, updated_at = NOW() "
                "WHERE user_id = $1::uuid AND id = $2",
                user_id, mid, str(error or "")[:400])
        return str(result).endswith("1")

    async def set_material_source_ref(self, user_id: str, material_id,
                                      source_ref: str) -> bool:
        """补写一条库条目的 source_ref（上传件落盘后才知道最终路径）。"""
        user_id = _as_uuid(user_id)
        try:
            mid = int(str(material_id).strip())
        except (ValueError, TypeError):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE material_items SET source_ref = $3, updated_at = NOW() "
                "WHERE user_id = $1::uuid AND id = $2",
                user_id, mid, str(source_ref or ""))
        return str(result).endswith("1")

    async def list_material_jobs(self, status: str = "processing",
                                 limit: int = 50) -> list:
        """取待续跑的导入作业（进程重启后按 ID 幂等续跑，plans/0923-08 §4.1）。

        返回行带 user_id（原样字符串）与 raw_text/source_ref —— 续跑要靠它们重放：
        text/diary 用 raw_text 重跑分析；link 用 source_ref 重抓；image/media 用
        source_ref 指向的临时件（已丢就标 failed，绝不满口 processing）。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, user_id, kind, source_ref, raw_text, title, status
                   FROM material_items WHERE status = $1
                   ORDER BY id ASC LIMIT $2""",
                str(status or "processing"), max(1, min(int(limit or 50), 200)))
        out = []
        for row in rows:
            item = dict(row)
            item["user_id"] = str(item.get("user_id") or "")
            item["raw_text"] = str(item.get("raw_text") or "")
            out.append(item)
        return out

    async def list_material_tape_builds(self, status: str = "pending",
                                        limit: int = 20) -> list:
        """取待续跑的成盘构建（服务重启后接着跑，plans/0923-08 §4.2）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, user_id, request_id, source_ids FROM material_tape_builds
                   WHERE status = $1 ORDER BY id ASC LIMIT $2""",
                str(status or "pending"), max(1, min(int(limit or 20), 100)))
        from engine.chunk_evidence import loads_json_list
        out = []
        for row in rows:
            item = dict(row)
            item["user_id"] = str(item.get("user_id") or "")
            item["source_ids"] = loads_json_list(item.get("source_ids"))
            out.append(item)
        return out

    # ── Loop Engineering：把候选 patch 显式提为实验 ─────────

    async def mark_patches_applied(self, user_id: str, session_id: str) -> int:
        user_id, session_id = _as_uuid(user_id), _as_uuid(session_id)
        pool = await self._get_pool()
        result = await pool.execute(
            """UPDATE prompt_patches
               SET status = 'experiment', applied_at = NOW(), session_id = $2::uuid
               WHERE user_id = $1::uuid AND status = 'candidate'""",
            user_id, session_id,
        )
        return int(str(result).split()[-1])
