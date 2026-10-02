"""
tests/unit/test_idempotency_middleware.py
-----------------------------------------
Unit tests for the IdempotencyMiddleware.
Redis is replaced with FakeRedis (AsyncMock) — no real Redis needed.

Test categories:
  A. Idempotency-Key header validation
  B. Cache hit → returns cached response immediately
  C. Lock contention → 409 response
  D. DB conflict: same key, different body → 409
  E. Request body hash is order-independent (canonical JSON)
  F. Non-idempotency routes pass through untouched
  G. Server error (5xx) does NOT cache response
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def make_transfer_body(amount: int = 1000) -> dict:
    return {
        "from_account_id": str(uuid.uuid4()),
        "to_account_id": str(uuid.uuid4()),
        "amount": amount,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# A. Header validation
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_missing_idempotency_key_header(test_client: AsyncClient):
    """POST /transfers without Idempotency-Key → 400."""
    response = await test_client.post(
        "/api/v1/transfers",
        json=make_transfer_body(),
        # deliberately omit Idempotency-Key header
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_MISSING"


@pytest.mark.asyncio
async def test_idempotency_key_too_long(test_client: AsyncClient):
    """Idempotency-Key longer than 255 chars → 400."""
    response = await test_client.post(
        "/api/v1/transfers",
        json=make_transfer_body(),
        headers={"Idempotency-Key": "x" * 256},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_TOO_LONG"


# ═══════════════════════════════════════════════════════════════════════════════
# B. Cache hit — returns cached response, never hits downstream handler
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cache_hit_returns_cached_response(test_client: AsyncClient):
    """
    When Redis has a cached response for this key, the middleware must
    return it immediately with X-Idempotent-Replayed: true header.
    No DB should be touched.
    """
    cached_payload = json.dumps({
        "status_code": 200,
        "body": {"transaction_id": str(uuid.uuid4()), "status": "completed"},
    })

    redis_mock = AsyncMock()
    redis_mock.get.return_value = cached_payload

    with patch("app.middleware.idempotency.get_redis", return_value=redis_mock):
        response = await test_client.post(
            "/api/v1/transfers",
            json=make_transfer_body(),
            headers={"Idempotency-Key": "idem-cached-001"},
        )

    assert response.status_code == 200
    assert response.headers.get("x-idempotent-replayed") == "true"
    body = response.json()
    assert "transaction_id" in body


# ═══════════════════════════════════════════════════════════════════════════════
# C. Lock contention → 409 with Retry-After
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_lock_contention_returns_409(test_client: AsyncClient):
    """
    When Redis SET NX returns False (lock held by another instance),
    middleware returns 409 with a Retry-After header.
    """
    redis_mock = AsyncMock()
    redis_mock.get.return_value = None      # cache miss
    redis_mock.set.return_value = False     # lock acquisition fails

    with patch("app.middleware.idempotency.get_redis", return_value=redis_mock):
        response = await test_client.post(
            "/api/v1/transfers",
            json=make_transfer_body(),
            headers={"Idempotency-Key": "idem-locked-001"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_PROCESSING"
    assert "Retry-After" in response.headers


# ═══════════════════════════════════════════════════════════════════════════════
# D. Canonical JSON body hashing — order-independent
# ═══════════════════════════════════════════════════════════════════════════════

def test_canonical_body_hash_is_key_order_independent():
    """
    Two JSON objects with the same fields but different key order
    must produce the same SHA-256 hash.
    """
    from app.middleware.idempotency import _canonical_body

    body_a = b'{"amount": 1000, "from_account_id": "abc", "to_account_id": "def"}'
    body_b = b'{"to_account_id": "def", "from_account_id": "abc", "amount": 1000}'

    assert _canonical_body(body_a) == _canonical_body(body_b)


def test_canonical_body_hash_differs_for_different_amounts():
    from app.middleware.idempotency import _canonical_body

    body_a = b'{"amount": 1000}'
    body_b = b'{"amount": 2000}'
    assert _canonical_body(body_a) != _canonical_body(body_b)


# ═══════════════════════════════════════════════════════════════════════════════
# E. GET requests bypass idempotency middleware
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_request_bypasses_middleware(test_client: AsyncClient):
    """
    GET /health should never be blocked by missing Idempotency-Key.
    (GET is idempotent by HTTP spec — no enforcement needed.)
    """
    response = await test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# F. Idempotency key scoped to path
# ═══════════════════════════════════════════════════════════════════════════════

def test_requires_idempotency_matching():
    from app.middleware.idempotency import _requires_idempotency

    assert _requires_idempotency("POST", "/api/v1/transfers") is True
    assert _requires_idempotency("POST", "/api/v1/accounts") is True
    assert _requires_idempotency("GET",  "/api/v1/transfers") is False
    assert _requires_idempotency("POST", "/health") is False
    assert _requires_idempotency("DELETE", "/api/v1/transfers") is False
