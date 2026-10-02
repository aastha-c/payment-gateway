"""
api/v1/transfers.py — Transfer endpoints
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AccountInactive,
    AccountNotFound,
    CurrencyMismatch,
    InsufficientFunds,
    PaymentGatewayError,
    TransactionNotFound,
)
from app.db.session import get_db
from app.schemas.transfer import TransferRequest, TransferResponse
from app.services.transaction_service import TransactionService

router = APIRouter(prefix="/transfers", tags=["Transfers"])


@router.post(
    "",
    response_model=TransferResponse,
    status_code=status.HTTP_200_OK,
    summary="Initiate a fund transfer between two accounts",
    responses={
        200: {"description": "Transfer completed successfully"},
        409: {"description": "Idempotency key conflict or request in-flight"},
        422: {"description": "Validation error (insufficient funds, inactive account, etc.)"},
    },
)
async def create_transfer(
    payload: TransferRequest,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> TransferResponse:
    """
    Transfer funds between two accounts using double-entry bookkeeping.

    - Amount is in **minor units** (cents for USD). \$10.50 = `1050`.
    - Requires `Idempotency-Key` header for safe retries.
    - Concurrent requests with the same key return the original response.
    """
    service = TransactionService(db)
    try:
        return await service.transfer(payload)
    except (AccountNotFound, TransactionNotFound) as exc:
        raise HTTPException(status_code=404, detail={"code": exc.error_code, "message": exc.message})
    except (AccountInactive, CurrencyMismatch, InsufficientFunds) as exc:
        raise HTTPException(status_code=422, detail={"code": exc.error_code, "message": exc.message, "detail": exc.detail})
    except PaymentGatewayError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.error_code, "message": exc.message})
