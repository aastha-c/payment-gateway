"""
schemas/transfer.py — Pydantic request/response models
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class TransferRequest(BaseModel):
    from_account_id: uuid.UUID = Field(..., description="Source account UUID")
    to_account_id: uuid.UUID = Field(..., description="Destination account UUID")
    amount: int = Field(..., gt=0, description="Amount in minor units (e.g., cents). Must be > 0.")
    reference: str | None = Field(None, max_length=255, description="Human-readable reference")
    description: str | None = Field(None, max_length=1000)
    metadata: dict[str, Any] | None = Field(default_factory=dict)

    @model_validator(mode="after")
    def accounts_must_differ(self) -> "TransferRequest":
        if self.from_account_id == self.to_account_id:
            raise ValueError("from_account_id and to_account_id must be different.")
        return self


class TransferResponse(BaseModel):
    transaction_id: uuid.UUID
    status: str
    amount: int
    currency: str
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    debit_entry_id: uuid.UUID
    credit_entry_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class AccountCreateRequest(BaseModel):
    owner_id: uuid.UUID
    currency: str = Field(..., min_length=3, max_length=3, description="ISO 4217 currency code")
    account_type: str = Field(
        default="asset",
        description="One of: asset, liability, revenue, expense, equity",
    )
    initial_balance: int = Field(
        default=0, ge=0, description="Initial balance in minor units (funding accounts only)"
    )

    @field_validator("currency")
    @classmethod
    def currency_uppercase(cls, v: str) -> str:
        return v.upper()

    @field_validator("account_type")
    @classmethod
    def valid_account_type(cls, v: str) -> str:
        valid = {"asset", "liability", "revenue", "expense", "equity"}
        if v not in valid:
            raise ValueError(f"account_type must be one of {valid}")
        return v


class AccountResponse(BaseModel):
    account_id: uuid.UUID
    owner_id: uuid.UUID
    currency: str
    account_type: str
    balance: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class BalanceResponse(BaseModel):
    account_id: uuid.UUID
    balance: int
    currency: str
    ledger_entries: list[dict]


class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
