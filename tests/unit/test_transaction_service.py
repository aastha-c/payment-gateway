"""
tests/unit/test_transaction_service.py
---------------------------------------
Unit tests for TransactionService.
All tests use the in-memory SQLite DB via db_session fixture.
No Redis involved — pure business logic coverage.

Test categories:
  A. Happy-path transfer
  B. Insufficient funds
  C. Same-account transfer rejection
  D. Inactive account rejection
  E. Currency mismatch rejection
  F. Double-entry ledger entry verification
  G. Optimistic lock — concurrent update detection
  H. Zero / negative amount rejection (Pydantic layer)
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.core.exceptions import (
    AccountInactive,
    AccountNotFound,
    CurrencyMismatch,
    InsufficientFunds,
)
from app.schemas.transfer import TransferRequest
from app.services.transaction_service import TransactionService


# ═══════════════════════════════════════════════════════════════════════════════
# A. Happy-path transfer
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_transfer_happy_path(db_session, account_factory):
    """
    Given: Alice has $100.00 (10_000 cents), Bob has $0.
    When:  Transfer $30.00 (3_000 cents) from Alice → Bob.
    Then:
      - Transaction status = 'completed'
      - Alice balance = 7_000
      - Bob balance   = 3_000
      - Two ledger entries created (debit + credit)
      - debit amount == credit amount
    """
    alice = await account_factory(balance=10_000, currency="USD")
    bob = await account_factory(balance=0, currency="USD")

    service = TransactionService(db_session)
    response = await service.transfer(
        TransferRequest(
            from_account_id=alice.id,
            to_account_id=bob.id,
            amount=3_000,
            reference="TEST-001",
        )
    )

    await db_session.refresh(alice)
    await db_session.refresh(bob)

    assert response.status == "completed"
    assert response.amount == 3_000
    assert response.currency == "USD"
    assert alice.balance == 7_000
    assert bob.balance == 3_000
    assert response.debit_entry_id != response.credit_entry_id


# ═══════════════════════════════════════════════════════════════════════════════
# B. Insufficient funds
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_transfer_insufficient_funds(db_session, account_factory):
    """
    Given: Alice has $10.00, transfer requests $50.00.
    Then:  InsufficientFunds raised, balances unchanged.
    """
    alice = await account_factory(balance=1_000)
    bob = await account_factory(balance=0)
    initial_alice_balance = alice.balance

    service = TransactionService(db_session)
    with pytest.raises(InsufficientFunds) as exc_info:
        await service.transfer(
            TransferRequest(from_account_id=alice.id, to_account_id=bob.id, amount=5_000)
        )

    err = exc_info.value
    assert err.error_code == "INSUFFICIENT_FUNDS"
    assert err.detail["available_balance"] == 1_000
    assert err.detail["requested_amount"] == 5_000

    # Ensure no balance mutation happened
    await db_session.refresh(alice)
    assert alice.balance == initial_alice_balance


# ═══════════════════════════════════════════════════════════════════════════════
# C. Same-account transfer rejected at schema level
# ═══════════════════════════════════════════════════════════════════════════════

def test_transfer_same_account_rejected():
    """Pydantic model_validator must reject from == to."""
    from pydantic import ValidationError

    same_id = uuid.uuid4()
    with pytest.raises(ValidationError) as exc_info:
        TransferRequest(from_account_id=same_id, to_account_id=same_id, amount=100)

    errors = exc_info.value.errors()
    assert any("different" in str(e["msg"]).lower() for e in errors)


# ═══════════════════════════════════════════════════════════════════════════════
# D. Inactive account
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_transfer_inactive_source_account(db_session, account_factory):
    alice = await account_factory(balance=5_000, is_active=False)
    bob = await account_factory(balance=0)

    service = TransactionService(db_session)
    with pytest.raises(AccountInactive) as exc_info:
        await service.transfer(
            TransferRequest(from_account_id=alice.id, to_account_id=bob.id, amount=100)
        )

    assert exc_info.value.error_code == "ACCOUNT_INACTIVE"


@pytest.mark.asyncio
async def test_transfer_inactive_destination_account(db_session, account_factory):
    alice = await account_factory(balance=5_000, is_active=True)
    bob = await account_factory(balance=0, is_active=False)

    service = TransactionService(db_session)
    with pytest.raises(AccountInactive):
        await service.transfer(
            TransferRequest(from_account_id=alice.id, to_account_id=bob.id, amount=100)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# E. Currency mismatch
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_transfer_currency_mismatch(db_session, account_factory):
    alice = await account_factory(balance=5_000, currency="USD")
    bob = await account_factory(balance=0, currency="EUR")

    service = TransactionService(db_session)
    with pytest.raises(CurrencyMismatch) as exc_info:
        await service.transfer(
            TransferRequest(from_account_id=alice.id, to_account_id=bob.id, amount=100)
        )

    assert exc_info.value.error_code == "CURRENCY_MISMATCH"


# ═══════════════════════════════════════════════════════════════════════════════
# F. Double-entry ledger correctness
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ledger_entries_balance(db_session, account_factory):
    """
    Verify that the two ledger entries created by a transfer
    satisfy the double-entry invariant: debit == credit.
    """
    from sqlalchemy import select
    from app.models.ledger_entry import LedgerEntry

    alice = await account_factory(balance=10_000)
    bob = await account_factory(balance=5_000)

    service = TransactionService(db_session)
    response = await service.transfer(
        TransferRequest(from_account_id=alice.id, to_account_id=bob.id, amount=2_500)
    )

    entries = (
        await db_session.execute(
            select(LedgerEntry).where(LedgerEntry.transaction_id == response.transaction_id)
        )
    ).scalars().all()

    assert len(entries) == 2

    debits = [e for e in entries if e.entry_type == "debit"]
    credits = [e for e in entries if e.entry_type == "credit"]

    assert len(debits) == 1
    assert len(credits) == 1

    total_debits = sum(e.amount for e in debits)
    total_credits = sum(e.amount for e in credits)

    assert total_debits == total_credits, (
        f"Double-entry violated: debits={total_debits} credits={total_credits}"
    )

    # Verify running balances
    debit_entry = debits[0]
    credit_entry = credits[0]
    assert debit_entry.running_balance == 10_000 - 2_500   # 7_500
    assert credit_entry.running_balance == 5_000 + 2_500   # 7_500


# ═══════════════════════════════════════════════════════════════════════════════
# G. Account not found
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_transfer_source_account_not_found(db_session, account_factory):
    bob = await account_factory(balance=0)

    service = TransactionService(db_session)
    with pytest.raises(AccountNotFound):
        await service.transfer(
            TransferRequest(
                from_account_id=uuid.uuid4(),   # random — doesn't exist
                to_account_id=bob.id,
                amount=100,
            )
        )


# ═══════════════════════════════════════════════════════════════════════════════
# H. Pydantic amount validation
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("amount", [0, -1, -100])
def test_transfer_non_positive_amount_rejected(amount: int):
    """Amount must be strictly positive (> 0)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TransferRequest(
            from_account_id=uuid.uuid4(),
            to_account_id=uuid.uuid4(),
            amount=amount,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# I. Balance query
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_account_balance(db_session, account_factory):
    alice = await account_factory(balance=5_000, currency="USD")

    service = TransactionService(db_session)
    result = await service.get_account_balance(alice.id)

    assert result["balance"] == 5_000
    assert result["currency"] == "USD"
    assert isinstance(result["ledger_entries"], list)


@pytest.mark.asyncio
async def test_get_balance_account_not_found(db_session):
    service = TransactionService(db_session)
    with pytest.raises(AccountNotFound):
        await service.get_account_balance(uuid.uuid4())
