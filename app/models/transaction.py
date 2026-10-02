"""
models/transaction.py
"""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("idempotency_keys.id"),
        nullable=True,
    )
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("total_amount > 0", name="ck_tx_positive_amount"),
        CheckConstraint(
            "status IN ('pending','completed','failed','reversed')",
            name="ck_tx_valid_status",
        ),
    )

    idempotency_key: Mapped["IdempotencyKey | None"] = relationship(
        back_populates="transactions", lazy="noload",
        primaryjoin="Transaction.idempotency_key_id == IdempotencyKey.id",
        foreign_keys="Transaction.idempotency_key_id",
    )
    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction", lazy="noload"
    )
