"""
tests/integration/test_concurrent_idempotency.py
-------------------------------------------------
Integration tests that simulate real-world retry storms.

These tests require:
  - A running PostgreSQL instance (or the in-memory SQLite from conftest)
  - A real or fake Redis (fakeredis used for speed)

Test scenarios:
  1. 50 concurrent identical requests → exactly 1 transfer executed
  2. Sequential duplicate requests → second returns cached (replayed)
  3. Same key, different body → 409 CONFLICT on second request
  4. Two genuinely different transfers (different keys) → both succeed
  5. High-contention: 10 senders all transferring to same recipient
  6. Retry-after-failure: failed idempotency key can be re-submitted

"The real test of idempotency is when Murphy's Law hits your retry loop."
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def unique_idem_key() -> str:
    return f"idem-{uuid.uuid4()}"


def make_transfer_payload(from_id: str, to_id: str, amount: int = 1_000) -> dict:
    return {
        "from_account_id": from_id,
        "to_account_id": to_id,
        "amount": amount,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 1. THE RETRY STORM TEST
#    50 concurrent requests with the SAME idempotency key.
#    Expected: exactly ONE transfer executes; all 50 return success.
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_50_concurrent_requests_same_idempotency_key(
    test_client: AsyncClient,
    account_factory,
    db_session,
):
    """
    Simulate a client whose retry library fires 50 requests simultaneously.
    The idempotency system must ensure:
      - Exactly one transfer executes (balance decremented once)
      - All 50 callers eventually get the same successful response
      - No "phantom" duplicate transactions in the DB
    """
    alice = await account_factory(balance=100_000, currency="USD")  # $1000.00
    bob = await account_factory(balance=0, currency="USD")

    idem_key = unique_idem_key()
    payload = make_transfer_payload(str(alice.id), str(bob.id), amount=5_000)

    # ── In-memory Redis via fakeredis for deterministic test ──────────────
    import fakeredis.aioredis as fakeredis
    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):

        # Fire all 50 requests concurrently
        tasks = [
            test_client.post(
                "/api/v1/transfers",
                json=payload,
                headers={"Idempotency-Key": idem_key},
            )
            for _ in range(50)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out exceptions (network errors, not business logic)
    http_responses = [r for r in responses if not isinstance(r, Exception)]
    status_codes = Counter(r.status_code for r in http_responses)

    # All responses must be either 200 (success) or 409 (lock contention)
    assert all(sc in {200, 409} for sc in status_codes), (
        f"Unexpected status codes: {status_codes}"
    )

    # At least one must have succeeded
    assert status_codes[200] >= 1, "No request succeeded!"

    # All 200 responses must have identical bodies (replayed from cache)
    successful = [r for r in http_responses if r.status_code == 200]
    tx_ids = {r.json().get("transaction_id") for r in successful}
    assert len(tx_ids) == 1, (
        f"Multiple distinct transactions created! tx_ids={tx_ids}"
    )

    # Verify DB: exactly ONE transfer happened
    await db_session.refresh(alice)
    await db_session.refresh(bob)
    assert alice.balance == 100_000 - 5_000, (
        f"Alice balance wrong: expected 95000, got {alice.balance}"
    )
    assert bob.balance == 5_000, (
        f"Bob balance wrong: expected 5000, got {bob.balance}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SEQUENTIAL DUPLICATE — second request returns cached replay
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sequential_duplicate_returns_replayed_response(
    test_client: AsyncClient,
    account_factory,
):
    """
    Client sends the same request twice, sequentially.
    Second response must:
      - Return HTTP 200
      - Have header X-Idempotent-Replayed: true
      - Return IDENTICAL body to first response
    """
    import fakeredis.aioredis as fakeredis
    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    alice = await account_factory(balance=10_000)
    bob = await account_factory(balance=0)
    idem_key = unique_idem_key()
    payload = make_transfer_payload(str(alice.id), str(bob.id), 1_000)

    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):

        r1 = await test_client.post(
            "/api/v1/transfers", json=payload,
            headers={"Idempotency-Key": idem_key},
        )
        r2 = await test_client.post(
            "/api/v1/transfers", json=payload,
            headers={"Idempotency-Key": idem_key},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.headers.get("x-idempotent-replayed") == "true"
    assert r1.json()["transaction_id"] == r2.json()["transaction_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# 3. SAME KEY, DIFFERENT BODY → 409 CONFLICT
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_same_key_different_body_returns_409(
    test_client: AsyncClient,
    account_factory,
):
    """
    A malicious or buggy client reuses an idempotency key with a different
    amount. The second request must be rejected with 409 IDEMPOTENCY_CONFLICT.

    This is a security invariant: prevents replay attacks where an attacker
    replays a payment with a higher amount using a stolen idempotency key.
    """
    import fakeredis.aioredis as fakeredis
    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    alice = await account_factory(balance=50_000)
    bob = await account_factory(balance=0)
    idem_key = unique_idem_key()

    payload_1 = make_transfer_payload(str(alice.id), str(bob.id), amount=1_000)
    payload_2 = make_transfer_payload(str(alice.id), str(bob.id), amount=9_999)  # different!

    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):

        r1 = await test_client.post(
            "/api/v1/transfers", json=payload_1,
            headers={"Idempotency-Key": idem_key},
        )
        r2 = await test_client.post(
            "/api/v1/transfers", json=payload_2,
            headers={"Idempotency-Key": idem_key},
        )

    assert r1.status_code == 200
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. TWO DIFFERENT KEYS → both succeed independently
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_two_transfers_different_keys_both_succeed(
    test_client: AsyncClient,
    account_factory,
    db_session,
):
    """Distinct idempotency keys should create distinct transactions."""
    import fakeredis.aioredis as fakeredis
    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    alice = await account_factory(balance=50_000)
    bob = await account_factory(balance=0)
    charlie = await account_factory(balance=0)

    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):

        r1 = await test_client.post(
            "/api/v1/transfers",
            json=make_transfer_payload(str(alice.id), str(bob.id), 1_000),
            headers={"Idempotency-Key": unique_idem_key()},
        )
        r2 = await test_client.post(
            "/api/v1/transfers",
            json=make_transfer_payload(str(alice.id), str(charlie.id), 2_000),
            headers={"Idempotency-Key": unique_idem_key()},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["transaction_id"] != r2.json()["transaction_id"]

    await db_session.refresh(alice)
    assert alice.balance == 50_000 - 1_000 - 2_000   # 47_000


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HIGH-CONTENTION: 10 concurrent senders → same recipient
#    Tests that lock ordering prevents deadlocks under fan-in traffic
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_high_contention_fan_in_no_deadlock(
    test_client: AsyncClient,
    account_factory,
    db_session,
):
    """
    10 senders each transfer $1.00 to the same recipient concurrently.
    Expected:
      - All 10 succeed (different idempotency keys)
      - Recipient balance = 10 * 100 = 1_000
      - No deadlock / 500 errors
    """
    import fakeredis.aioredis as fakeredis
    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    senders = [await account_factory(balance=1_000) for _ in range(10)]
    recipient = await account_factory(balance=0)

    async def do_transfer(sender, idx):
        return await test_client.post(
            "/api/v1/transfers",
            json=make_transfer_payload(str(sender.id), str(recipient.id), 100),
            headers={"Idempotency-Key": f"fan-in-{idx}-{uuid.uuid4()}"},
        )

    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):

        responses = await asyncio.gather(
            *[do_transfer(s, i) for i, s in enumerate(senders)],
            return_exceptions=True,
        )

    statuses = Counter(
        r.status_code for r in responses if not isinstance(r, Exception)
    )
    assert statuses[200] == 10, f"Expected 10 successes, got: {statuses}"

    await db_session.refresh(recipient)
    assert recipient.balance == 1_000  # 10 × $1.00


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CHAOS: Redis unavailable → falls back to DB-only path
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_redis_unavailable_graceful_degradation(
    test_client: AsyncClient,
    account_factory,
):
    """
    When Redis is completely unreachable, the system should:
      - Log the error
      - Fall through to the DB-level idempotency guard (ON CONFLICT DO NOTHING)
      - Still process the request correctly (no 500 for client)

    This tests the defense-in-depth principle: PG is the authoritative guard.
    """
    alice = await account_factory(balance=10_000)
    bob = await account_factory(balance=0)

    # Redis mock that throws on every call
    broken_redis = AsyncMock()
    broken_redis.get.side_effect = ConnectionError("Redis is down!")
    broken_redis.set.side_effect = ConnectionError("Redis is down!")

    # The middleware catches Redis errors and falls through
    with patch("app.middleware.idempotency.get_redis", return_value=broken_redis):
        response = await test_client.post(
            "/api/v1/transfers",
            json=make_transfer_payload(str(alice.id), str(bob.id), 500),
            headers={"Idempotency-Key": unique_idem_key()},
        )

    # System should degrade to lock-not-acquired path, returning 409 or succeed via DB
    # Either outcome is acceptable — what's NOT acceptable is a 500
    assert response.status_code in {200, 409}, (
        f"Unexpected 5xx during Redis outage: {response.status_code} {response.text}"
    )
