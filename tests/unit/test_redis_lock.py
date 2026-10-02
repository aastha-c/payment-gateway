"""
tests/unit/test_redis_lock.py
------------------------------
Unit tests for RedisLock distributed locking behaviour.
Uses AsyncMock — no real Redis required.

Tests:
  A. Successful lock acquisition and release
  B. Lock fails when key already held (SET NX returns False)
  C. Release does nothing when lock was not acquired
  D. Context manager releases lock on normal exit
  E. Context manager releases lock on exception (no lock leak)
  F. Lua unlock script is called with correct owner ID
  G. Lock key includes both path and idempotency key (namespacing)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, call, patch

from app.core.exceptions import LockAcquisitionFailed
from app.db.redis_client import RedisLock, _lock_key, _cache_key


# ═══════════════════════════════════════════════════════════════════════════════
# A. Successful acquire and release
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_lock_acquire_success(redis_mock):
    redis_mock.set.return_value = True

    lock = RedisLock(redis_mock, "idem-123", "/api/v1/transfers")
    acquired = await lock.acquire()

    assert acquired is True
    assert lock._owner_id is not None
    redis_mock.set.assert_called_once()
    # Verify NX and PX were set
    _, kwargs = redis_mock.set.call_args
    assert kwargs.get("nx") is True
    assert "px" in kwargs


@pytest.mark.asyncio
async def test_lock_release_calls_lua_script(redis_mock):
    redis_mock.set.return_value = True
    redis_mock.eval.return_value = 1

    lock = RedisLock(redis_mock, "idem-123", "/api/v1/transfers")
    await lock.acquire()
    owner_id = lock._owner_id
    released = await lock.release()

    assert released is True
    # Lua script must be called with the owner's ID to ensure atomic release
    redis_mock.eval.assert_called_once()
    eval_args = redis_mock.eval.call_args[0]
    assert owner_id in eval_args  # owner ID passed as ARGV[1]


# ═══════════════════════════════════════════════════════════════════════════════
# B. Lock fails when already held
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_lock_acquire_fails_when_held(redis_mock):
    redis_mock.set.return_value = False  # NX failed — key exists

    lock = RedisLock(redis_mock, "idem-456", "/api/v1/transfers")
    acquired = await lock.acquire()

    assert acquired is False
    assert lock._owner_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# C. Release without prior acquisition is a no-op
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_release_without_acquire_is_noop(redis_mock):
    lock = RedisLock(redis_mock, "idem-789", "/api/v1/transfers")
    released = await lock.release()

    assert released is False
    redis_mock.eval.assert_not_called()  # Lua script should never run


# ═══════════════════════════════════════════════════════════════════════════════
# D. Context manager: __aexit__ always releases lock
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_context_manager_releases_on_success(redis_mock):
    redis_mock.set.return_value = True
    redis_mock.eval.return_value = 1

    async with RedisLock(redis_mock, "idem-cm-1", "/api/v1/transfers"):
        pass  # normal exit

    redis_mock.eval.assert_called_once()  # release was called


@pytest.mark.asyncio
async def test_context_manager_releases_on_exception(redis_mock):
    """Lock must be released even when the body raises an exception."""
    redis_mock.set.return_value = True
    redis_mock.eval.return_value = 1

    with pytest.raises(RuntimeError, match="simulated error"):
        async with RedisLock(redis_mock, "idem-cm-2", "/api/v1/transfers"):
            raise RuntimeError("simulated error")

    redis_mock.eval.assert_called_once()  # lock was released despite exception


# ═══════════════════════════════════════════════════════════════════════════════
# E. Context manager raises LockAcquisitionFailed when lock is held
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_context_manager_raises_when_lock_held(redis_mock):
    redis_mock.set.return_value = False  # lock held

    with pytest.raises(LockAcquisitionFailed):
        async with RedisLock(redis_mock, "idem-held", "/api/v1/transfers"):
            pass  # should never reach here


# ═══════════════════════════════════════════════════════════════════════════════
# F. Key namespacing
# ═══════════════════════════════════════════════════════════════════════════════

def test_lock_key_namespacing():
    """Lock keys must include path to prevent cross-endpoint collisions."""
    key_a = _lock_key("idem-123", "/api/v1/transfers")
    key_b = _lock_key("idem-123", "/api/v1/accounts")
    assert key_a != key_b
    assert "/api/v1/transfers" in key_a
    assert "idem-123" in key_a


def test_cache_key_namespacing():
    key_a = _cache_key("idem-abc", "/api/v1/transfers")
    key_b = _cache_key("idem-abc", "/api/v1/accounts")
    assert key_a != key_b


def test_different_idempotency_keys_different_lock_keys():
    k1 = _lock_key("idem-aaa", "/api/v1/transfers")
    k2 = _lock_key("idem-bbb", "/api/v1/transfers")
    assert k1 != k2
