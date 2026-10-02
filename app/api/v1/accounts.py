"""
api/v1/accounts.py — Account management endpoints
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AccountNotFound, PaymentGatewayError
from app.db.session import get_db
from app.models.account import Account
from app.schemas.transfer import AccountCreateRequest, AccountResponse, BalanceResponse
from app.services.transaction_service import TransactionService

router = APIRouter(prefix="/accounts", tags=["Accounts"])


@router.post(
    "",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new account",
)
async def create_account(
    payload: AccountCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> AccountResponse:
    account = Account(
        owner_id=payload.owner_id,
        currency=payload.currency,
        account_type=payload.account_type,
        balance=payload.initial_balance,
    )
    db.add(account)
    await db.flush()
    await db.commit()
    await db.refresh(account)
    return AccountResponse(
        account_id=account.id,
        owner_id=account.owner_id,
        currency=account.currency,
        account_type=account.account_type,
        balance=account.balance,
        is_active=account.is_active,
        created_at=account.created_at,
    )


@router.get(
    "/{account_id}/balance",
    response_model=BalanceResponse,
    summary="Get account balance and recent ledger entries",
)
async def get_balance(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> BalanceResponse:
    service = TransactionService(db)
    try:
        data = await service.get_account_balance(account_id)
        return BalanceResponse(**data)
    except AccountNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.message)
