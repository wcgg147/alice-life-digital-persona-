"""
L1 Prompt 层 — Redis 存储用户压缩画像（会话级上下文冻结）

Hermes Agent 对应：MEMORY.md + USER.md 的会话级上下文冻结注入。
- 会话开始时从 Redis 读取完整 L1 内容，进行会话级上下文冻结作为 System Prompt 的一部分
- 会话过程中任何实时分析结果只写入 L2，绝不修改 L1 冻结副本
- L1 的刷新由中梦/大梦任务统一完成

注：此处的"上下文冻结"是应用层业务逻辑，用于保证会话期间数据一致性，
与 Redis 的 RDB/AOF 持久化快照机制完全不同。
"""

import json
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

try:
    from ..config import REDIS_URL, L1_MAX_TOKENS
except ImportError:
    from config import REDIS_URL, L1_MAX_TOKENS


class L1RedisProfile:
    """
    L1 画像管理

    Redis Key: user:profile:{user_id}  (Hash)
    Fields:
        cefr, recent_weakness, preferred_topics, learning_style,
        review_queue, updated_at

    Redis Key: user:profile:stable:{user_id}  (Hash)
    Fields: 持久稳定的画像（CEFR 等级、长期偏好）
    """

    PREFIX_PROFILE = "user:profile"
    PREFIX_STABLE = "user:profile:stable"
    PREFIX_DEBOUNCE = "user:dream:pending"

    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        self._redis = redis_client
        self._max_tokens = L1_MAX_TOKENS

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
        return self._redis

    # ── 会话级上下文冻结获取 ──────────────────────────────

    async def get_frozen_snapshot(self, user_id: str) -> dict:
        """
        获取会话级上下文冻结——会话开始时调用一次，之后不修改。

        合并 stable + session 两个逻辑分区。
        返回的 dict 作为 `ctx.profile` 供 prompt 组装读取
        （消费点：`TutorAgent._build_system_prompt` / `_build_control_prompt`；
        旧指向的 `prompt/prompt_builder.py` 是从未接线的死代码，已删）。

        注：此处的"上下文冻结"是应用层业务逻辑，在会话开始时锁定用户画像，
        以保证会话期间数据一致性，与 Redis 的 RDB/AOF 持久化快照机制完全不同。
        """
        r = await self._get_redis()
        profile_key = f"{self.PREFIX_PROFILE}:{user_id}"
        stable_key = f"{self.PREFIX_STABLE}:{user_id}"

        profile = await r.hgetall(profile_key)
        stable = await r.hgetall(stable_key)

        merged = {**stable, **profile}

        return {
            "cefr": merged.get("cefr", ""),
            "recent_weakness": merged.get("recent_weakness", ""),
            "preferred_topics": json.loads(merged.get("preferred_topics", "[]")),
            "learning_style": merged.get("learning_style", "balanced"),
            "communication_style": merged.get("communication_style", ""),
            "review_queue": merged.get("review_queue", ""),
            "confidence_score": float(merged.get("confidence_score", "0.5")),
            "updated_at": merged.get("updated_at", ""),
        }

    # ── 画像写入（由梦想任务调用） ──────────────────────────

    async def update_profile(self, user_id: str, profile_data: dict):
        """更新用户画像（由中梦/大梦任务调用）"""
        r = await self._get_redis()
        key = f"{self.PREFIX_PROFILE}:{user_id}"

        # 整档覆盖写：未传字段回默认值。communication_style 只有会话末的
        # profile_evolution 会算，main.py / session_manager 的写入不带它，
        # 不保留就会被抹成空串。
        existing = await r.hgetall(key)
        data = {
            "cefr": profile_data.get("cefr", ""),
            "recent_weakness": profile_data.get("recent_weakness", ""),
            "preferred_topics": json.dumps(profile_data.get("preferred_topics", [])),
            "learning_style": profile_data.get("learning_style", "balanced"),
            "communication_style": profile_data.get(
                "communication_style", existing.get("communication_style", "")
            ),
            "review_queue": profile_data.get("review_queue", ""),
            "confidence_score": str(profile_data.get("confidence_score", 0.5)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await r.hset(key, mapping=data)

    async def update_stable_profile(self, user_id: str, data: dict):
        """更新稳定画像（CEFR 等级、长期偏好变更时调用）"""
        r = await self._get_redis()
        key = f"{self.PREFIX_STABLE}:{user_id}"
        await r.hset(key, mapping=data)

    async def clear_old_profile(self, user_id: str):
        """归档前清除旧画像"""
        r = await self._get_redis()
        await r.delete(f"{self.PREFIX_PROFILE}:{user_id}")

    async def clear_user_profile(self, user_id: str) -> int:
        """彻底删除该用户的 L1 画像：session + stable 两键（plans/0923-04 §4.5）。

        只动这两个键，不碰别的用户。返回删除的键数；Redis 异常原样上抛，
        由调用方决定删除流程是否算失败。
        """
        r = await self._get_redis()
        return int(await r.delete(
            f"{self.PREFIX_PROFILE}:{user_id}",
            f"{self.PREFIX_STABLE}:{user_id}",
        ))

    # ── 压缩 ──────────────────────────────────────────────

    async def estimate_tokens(self, user_id: str) -> int:
        """估算当前 L1 的 token 数（粗略：字符数 / 4）"""
        snapshot = await self.get_frozen_snapshot(user_id)
        text = json.dumps(snapshot, ensure_ascii=False)
        return len(text) // 4

    async def compress_if_needed(self, user_id: str) -> bool:
        """
        检查是否需要压缩，超过硬上限时标记需要大梦任务处理。
        实际压缩由大梦任务执行，此处仅做检查。
        """
        tokens = await self.estimate_tokens(user_id)
        return tokens > self._max_tokens

    # ── 防抖标记 ──────────────────────────────────────────

    async def set_dream_pending(self, user_id: str, ttl: int = 1800) -> bool:
        """
        设置梦想任务防抖标记。
        如果已存在标记（30分钟内已触发），返回 False 表示跳过。
        """
        r = await self._get_redis()
        key = f"{self.PREFIX_DEBOUNCE}:{user_id}"
        # SET NX: 仅当 key 不存在时设置
        ok = await r.set(key, datetime.now(timezone.utc).isoformat(), nx=True, ex=ttl)
        return ok is True

    async def clear_dream_pending(self, user_id: str):
        """清除防抖标记"""
        r = await self._get_redis()
        await r.delete(f"{self.PREFIX_DEBOUNCE}:{user_id}")
