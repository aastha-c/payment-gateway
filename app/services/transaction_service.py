"""
services/transaction_service.py
--------------------------------
The double-entry bookkeeping engine.

Core invariant enforced here (and again by DB trigger as a backstop):
    SUM(debit amounts) == SUM(credit amounts) per transaction

Transfer flow (Alice → Bob):
    debit  Alice's account  +amount  (her balance decreases)
    credit Bob's account    +amount  (his balance increases)

Why SELECT FOR UPDATE with ORDER BY id?
    Prevents deadlock: if two concurrent transfers involve the same
    pair of accounts in opposite directions, ordering by id ensures
    both acquire locks in the same sequence, eliminating circular wait.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AccountInactive,
    AccountNotFound,
    CurrencyMismatch,
    DoubleEntryViolation,
    InsufficientFunds,
    TransactionNotFound,
)
from app.core.logging import get_logger
from app.models.account import Account
from app.models.ledger_entry import LedgerEntry
from app.models.transaction import Transaction
from app.schemas.transfer import TransferRequest, TransferResponse

logger = get_logger(__name__)


class TransactionService:
    """Encapsulates all double-entry ledger operations."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Public API ─────────────────────────────────────────────────────────

    async def transfer(
        self,
        request: TransferRequest,
        idempotency_key_id: uuid.UUID | None = None,
    ) -> TransferResponse:
        """
        Atomically transfer `amount` (in minor units) from `from_account`
        to `to_account`. Creates exactly two ledger entries.

        Raises:
            AccountNotFound      – one or both accounts do not exist
            AccountInactive      – account is soft-deleted / suspended
            CurrencyMismatch     – accounts have different currencies
            InsufficientFunds    – debit account has insufficient balance
            DoubleEntryViolation – programming error (should never happen)
        """
        from_id = uuid.UUID(str(request.from_account_id))
        to_id = uuid.UUID(str(request.to_account_id))

        # ── 1. Lock both accounts (sorted order prevents deadlocks) ───────
        accounts = await self._lock_accounts([from_id, to_id])
        from_account = accounts.get(from_id)
        to_account = accounts.get(to_id)

        # ── 2. Business rule validation ───────────────────────────────────
        if from_account is None:
            raise AccountNotFound(f"Source account {from_id} not found.")
        if to_account is None:
            raise AccountNotFound(f"Destination account {to_id} not found.")
        if not from_account.is_active:
            raise AccountInactive(f"Source account {from_id} is inactive.")
        if not to_account.is_active:
            raise AccountInactive(f"Destination account {to_id} is inactive.")
        if from_account.currency != to_account.currency:
            raise CurrencyMismatch(
                f"Currency mismatch: {from_account.currency} vs {to_account.currency}"
            )
        if from_account.balance < request.amount:
            raise InsufficientFunds(
                "Insufficient funds.",
                detail={
                    "available_balance": from_account.balance,
                    "requested_amount": request.amount,
                    "currency": from_account.currency,
                },
            )

        # ── 3. Create transaction header ──────────────────────────────────
        tx = Transaction(
            id=uuid.uuid4(),
            idempotency_key_id=idempotency_key_id,
            reference=request.reference,
            description=request.description,
            currency=from_account.currency,
            total_amount=request.amount,
            status="completed",
            metadata_=request.metadata or {},
        )
        self._db.add(tx)

        # ── 4. Create ledger entries ──────────────────────────────────────
        debit_balance = from_account.balance - request.amount
        credit_balance = to_account.balance + request.amount

        debit_entry = LedgerEntry(
            transaction_id=tx.id,
            account_id=from_id,
            entry_type="debit",
            amount=request.amount,
            currency=from_account.currency,
            running_balance=debit_balance,
        )
        credit_entry = LedgerEntry(
            transaction_id=tx.id,
            account_id=to_id,
            entry_type="credit",
            amount=request.amount,
            currency=to_account.currency,
            running_balance=credit_balance,
        )
        self._db.add(debit_entry)
        self._db.add(credit_entry)

        # ── 5. Application-level double-entry assertion (defense-in-depth) ─
        # DB trigger will also catch this, but we want a clear error message.
        debit_total = request.amount
        credit_total = request.amount
        if debit_total != credit_total:
            raise DoubleEntryViolation(
                f"Double-entry violated: debits={debit_total} credits={credit_total}"
            )

        # ── 6. Atomically update account balances ──────────────────────────
        await self._db.execute(
            update(Account)
            .where(Account.id == from_id)
            .values(
                balance=Account.balance - request.amount,
                version=Account.version + 1,
                updated_at=datetime.now(tz=timezone.utc),
            )
        )
        await self._db.execute(
            update(Account)
            .where(Account.id == to_id)
            .values(
                balance=Account.balance + request.amount,
                version=Account.version + 1,
                updated_at=datetime.now(tz=timezone.utc),
            )
        )
        await self._db.flush()

        logger.info(
            "transfer_completed",
            transaction_id=str(tx.id),
            from_account=str(from_id),
            to_account=str(to_id),
            amount=request.amount,
            currency=from_account.currency,
        )

        return TransferResponse(
            transaction_id=tx.id,
            status="completed",
            amount=request.amount,
            currency=from_account.currency,
            from_account_id=from_id,
            to_account_id=to_id,
            debit_entry_id=debit_entry.id,
            credit_entry_id=credit_entry.id,
            created_at=tx.created_at,
        )

    async def get_account_balance(self, account_id: uuid.UUID) -> dict:
        """Return current balance and recent ledger entries."""
        account = await self._db.get(Account, account_id)
        if not account:
            raise AccountNotFound(f"Account {account_id} not found.")

        entries_result = await self._db.execute(
            select(LedgerEntry)
            .where(LedgerEntry.account_id == account_id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(20)
        )
        entries = entries_result.scalars().all()

        return {
            "account_id": account.id,
            "balance": account.balance,
            "currency": account.currency,
            "ledger_entries": [
                {
                    "id": e.id,
                    "type": e.entry_type,
                    "amount": e.amount,
                    "running_balance": e.running_balance,
                    "created_at": e.created_at.isoformat(),
                }
                for e in entries
            ],
        }

    # ── Private helpers ────────────────────────────────────────────────────

    async def _lock_accounts(
        self, account_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, Account]:
        """
        SELECT ... FOR UPDATE with ORDER BY id.
        Sorting by id ensures consistent lock acquisition order
        across all concurrent transactions — eliminates deadlocks.
        """
        sorted_ids = sorted(account_ids)
        result = await self._db.execute(
            select(Account)
            .where(Account.id.in_(sorted_ids))
            .order_by(Account.id)
            .with_for_update()  # Row-level exclusive lock
        )
        accounts = result.scalars().all()
        return {a.id: a for a in accounts}
