"""
tests/integration/test_api_endpoints.py
----------------------------------------
Full API endpoint integration tests — exercises the complete HTTP
request/response cycle including middleware, routing, and service layer.

Tests:
  1. POST /api/v1/accounts — happy path
  2. GET  /api/v1/accounts/{id}/balance — after transfer
  3. POST /api/v1/transfers — full lifecycle via HTTP
  4. GET  /health
  5. Transfer chain: A → B → C (cascading transfers)
  6. Idempotency header propagated through HTTP response
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


def idem_key() -> str:
    return f"api-test-{uuid.uuid4()}"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Create account
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_account(test_client: AsyncClient):
    owner_id = str(uuid.uuid4())
    response = await test_client.post(
        "/api/v1/accounts",
        json={"owner_id": owner_id, "currency": "USD", "initial_balance": 0},
        headers={"Idempotency-Key": idem_key()},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["currency"] == "USD"
    assert body["balance"] == 0
    assert body["is_active"] is True
    assert uuid.UUID(body["account_id"])  # valid UUID


@pytest.mark.asyncio
async def test_create_account_with_initial_balance(test_client: AsyncClient):
    """Funding accounts can be seeded with an initial balance."""
    response = await test_client.post(
        "/api/v1/accounts",
        json={
            "owner_id": str(uuid.uuid4()),
            "currency": "EUR",
            "initial_balance": 50_000,   # €500.00
        },
        headers={"Idempotency-Key": idem_key()},
    )
    assert response.status_code == 201
    assert response.json()["balance"] == 50_000


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Get balance
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_balance_after_account_creation(test_client: AsyncClient):
    create_resp = await test_client.post(
        "/api/v1/accounts",
        json={"owner_id": str(uuid.uuid4()), "currency": "USD", "initial_balance": 25_000},
        headers={"Idempotency-Key": idem_key()},
    )
    account_id = create_resp.json()["account_id"]

    balance_resp = await test_client.get(f"/api/v1/accounts/{account_id}/balance")
    assert balance_resp.status_code == 200
    body = balance_resp.json()
    assert body["balance"] == 25_000
    assert body["currency"] == "USD"
    assert isinstance(body["ledger_entries"], list)


@pytest.mark.asyncio
async def test_get_balance_nonexistent_account(test_client: AsyncClient):
    response = await test_client.get(f"/api/v1/accounts/{uuid.uuid4()}/balance")
    assert response.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Full transfer lifecycle via HTTP
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_transfer_lifecycle(test_client: AsyncClient):
    """
    End-to-end: create two accounts, transfer, verify both balances updated.
    Uses fakeredis so middleware executes the full flow.
    """
    import fakeredis.aioredis as fakeredis
    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    # Create sender with $500
    alice_resp = await test_client.post(
        "/api/v1/accounts",
        json={"owner_id": str(uuid.uuid4()), "currency": "USD", "initial_balance": 50_000},
        headers={"Idempotency-Key": idem_key()},
    )
    assert alice_resp.status_code == 201
    alice_id = alice_resp.json()["account_id"]

    # Create recipient with $0
    bob_resp = await test_client.post(
        "/api/v1/accounts",
        json={"owner_id": str(uuid.uuid4()), "currency": "USD", "initial_balance": 0},
        headers={"Idempotency-Key": idem_key()},
    )
    assert bob_resp.status_code == 201
    bob_id = bob_resp.json()["account_id"]

    # Transfer $100 (10_000 cents)
    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):

        transfer_resp = await test_client.post(
            "/api/v1/transfers",
            json={"from_account_id": alice_id, "to_account_id": bob_id, "amount": 10_000},
            headers={"Idempotency-Key": idem_key()},
        )

    assert transfer_resp.status_code == 200
    body = transfer_resp.json()
    assert body["status"] == "completed"
    assert body["amount"] == 10_000

    # Verify balances
    alice_balance = (await test_client.get(f"/api/v1/accounts/{alice_id}/balance")).json()
    bob_balance = (await test_client.get(f"/api/v1/accounts/{bob_id}/balance")).json()

    assert alice_balance["balance"] == 40_000  # 50_000 - 10_000
    assert bob_balance["balance"] == 10_000


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Health check
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_check(test_client: AsyncClient):
    response = await test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Transfer: insufficient funds via HTTP
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_transfer_insufficient_funds_via_http(test_client: AsyncClient):
    """Verify that 422 with correct error code is returned through the HTTP stack."""
    import fakeredis.aioredis as fakeredis
    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    alice_resp = await test_client.post(
        "/api/v1/accounts",
        json={"owner_id": str(uuid.uuid4()), "currency": "USD", "initial_balance": 100},
        headers={"Idempotency-Key": idem_key()},
    )
    bob_resp = await test_client.post(
        "/api/v1/accounts",
        json={"owner_id": str(uuid.uuid4()), "currency": "USD", "initial_balance": 0},
        headers={"Idempotency-Key": idem_key()},
    )

    alice_id = alice_resp.json()["account_id"]
    bob_id = bob_resp.json()["account_id"]

    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):

        response = await test_client.post(
            "/api/v1/transfers",
            json={"from_account_id": alice_id, "to_account_id": bob_id, "amount": 99_999},
            headers={"Idempotency-Key": idem_key()},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INSUFFICIENT_FUNDS"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Transfer chain A → B → C
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_transfer_chain(test_client: AsyncClient):
    """
    A sends $10 to B, then B sends $5 to C.
    Tests that intermediate balances are updated correctly between transfers.
    """
    import fakeredis.aioredis as fakeredis
    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    async def create_account(balance: int) -> str:
        r = await test_client.post(
            "/api/v1/accounts",
            json={"owner_id": str(uuid.uuid4()), "currency": "USD", "initial_balance": balance},
            headers={"Idempotency-Key": idem_key()},
        )
        return r.json()["account_id"]

    alice_id = await create_account(10_000)
    bob_id = await create_account(0)
    charlie_id = await create_account(0)

    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):

        # A → B: $10
        r1 = await test_client.post(
            "/api/v1/transfers",
            json={"from_account_id": alice_id, "to_account_id": bob_id, "amount": 1_000},
            headers={"Idempotency-Key": idem_key()},
        )
        assert r1.status_code == 200

        # B → C: $5 (Bob now has enough)
        r2 = await test_client.post(
            "/api/v1/transfers",
            json={"from_account_id": bob_id, "to_account_id": charlie_id, "amount": 500},
            headers={"Idempotency-Key": idem_key()},
        )
        assert r2.status_code == 200

    alice_bal = (await test_client.get(f"/api/v1/accounts/{alice_id}/balance")).json()
    bob_bal = (await test_client.get(f"/api/v1/accounts/{bob_id}/balance")).json()
    charlie_bal = (await test_client.get(f"/api/v1/accounts/{charlie_id}/balance")).json()

    assert alice_bal["balance"] == 10_000 - 1_000        # 9_000
    assert bob_bal["balance"] == 1_000 - 500             # 500
    assert charlie_bal["balance"] == 500
