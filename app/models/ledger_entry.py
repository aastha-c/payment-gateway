"""
models/ledger_entry.py
"""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False, index=True
    )
    entry_type: Mapped[str] = mapped_column(String(6), nullable=False)  # 'debit' | 'credit'
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)       # always positive
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    running_balance: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_positive_amount"),
        CheckConstraint("entry_type IN ('debit','credit')", name="ck_ledger_valid_type"),
    )

    transaction: Mapped["Transaction"] = relationship(
        back_populates="ledger_entries", lazy="noload",
        primaryjoin="LedgerEntry.transaction_id == Transaction.id",
        foreign_keys="LedgerEntry.transaction_id",
    )
    account: Mapped["Account"] = relationship(
        back_populates="ledger_entries", lazy="noload",
        primaryjoin="LedgerEntry.account_id == Account.id",
        foreign_keys="LedgerEntry.account_id",
    )
