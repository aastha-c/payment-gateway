"""
db/redis_client.py
------------------
Async Redis client with distributed lock helpers.

Locking algorithm:
  SET key value NX PX ttl_ms
  → Lua script for atomic unlock (only owner can release)

Why Lua for unlock?
  A naive GET-then-DEL has a race: between GET and DEL another worker
  could have acquired the lock (after TTL expiry). Lua runs atomically
  inside Redis, so the check+delete is a single indivisible operation.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.exceptions import LockAcquisitionFailed
from app.core.logging import get_logger

settings = get_settings()
logger = get_logger(__name__)

# ── Lua Scripts ────────────────────────────────────────────────────────────

# Atomically releases a lock ONLY if the current owner matches.
UNLOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# ── Redis key namespacing ──────────────────────────────────────────────────

def _lock_key(idempotency_key: str, path: str) -> str:
    return f"idem:lock:{path}:{idempotency_key}"

def _cache_key(idempotency_key: str, path: str) -> str:
    return f"idem:resp:{path}:{idempotency_key}"


# ── Client singleton ───────────────────────────────────────────────────────

_redis_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _redis_client


async def close_redis() -> None:
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None


# ── Distributed Lock ───────────────────────────────────────────────────────

class RedisLock:
    """
    Non-reentrant distributed lock backed by Redis SET NX PX.

    Usage:
        async with RedisLock(redis, "idem-key-123", "/api/v1/transfers") as owner:
            # critical section — only one process runs this at a time
            ...
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        idempotency_key: str,
        path: str,
        ttl_ms: int | None = None,
    ) -> None:
        self._redis = redis
        self._key = _lock_key(idempotency_key, path)
        self._ttl_ms = ttl_ms or settings.REDIS_LOCK_TTL_MS
        self._owner_id: str | None = None

    async def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if successful."""
        owner_id = str(uuid.uuid4())
        acquired = await self._redis.set(
            self._key,
            owner_id,
            nx=True,          # Only set if Not eXists
            px=self._ttl_ms,  # Auto-expire to prevent deadlocks on crash
        )
        if acquired:
            self._owner_id = owner_id
            logger.debug("lock_acquired", key=self._key, owner=owner_id)
        return bool(acquired)

    async def release(self) -> bool:
        """Release lock only if we are the owner."""
        if not self._owner_id:
            return False
        result = await self._redis.eval(UNLOCK_SCRIPT, 1, self._key, self._owner_id)
        released = bool(result)
        if released:
            logger.debug("lock_released", key=self._key, owner=self._owner_id)
        else:
            logger.warning(
                "lock_release_skipped",
                key=self._key,
                owner=self._owner_id,
                reason="lock expired or already released",
            )
        return released

    async def __aenter__(self) -> "RedisLock":
        if not await self.acquire():
            raise LockAcquisitionFailed(
                f"Could not acquire lock for key={self._key}. "
                "Another request is processing. Retry after 1–2 seconds."
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.release()


# ── Response Cache ─────────────────────────────────────────────────────────

class IdempotencyCache:
    """Read/write cached responses for already-processed idempotency keys."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def get(self, idempotency_key: str, path: str) -> str | None:
        """Return cached JSON response string, or None on miss."""
        return await self._redis.get(_cache_key(idempotency_key, path))

    async def set(
        self,
        idempotency_key: str,
        path: str,
        response_json: str,
        ttl_s: int | None = None,
    ) -> None:
        """Cache a response payload."""
        await self._redis.set(
            _cache_key(idempotency_key, path),
            response_json,
            ex=ttl_s or settings.REDIS_CACHE_TTL_S,
        )

    async def delete(self, idempotency_key: str, path: str) -> None:
        await self._redis.delete(_cache_key(idempotency_key, path))
