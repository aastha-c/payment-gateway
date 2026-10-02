"""
core/exceptions.py
------------------
Domain exception hierarchy.
Each exception carries an http_status so the global handler
can render the correct response without if/elif chains.
"""
from __future__ import annotations


class PaymentGatewayError(Exception):
    """Base for all domain errors."""
    http_status: int = 500
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


# ── Idempotency ────────────────────────────────────────────────────────────

class IdempotencyKeyMissing(PaymentGatewayError):
    http_status = 400
    error_code = "IDEMPOTENCY_KEY_MISSING"


class IdempotencyKeyTooLong(PaymentGatewayError):
    http_status = 400
    error_code = "IDEMPOTENCY_KEY_TOO_LONG"


class IdempotencyConflict(PaymentGatewayError):
    """Same key, different request body."""
    http_status = 409
    error_code = "IDEMPOTENCY_CONFLICT"


class IdempotencyProcessing(PaymentGatewayError):
    """Another worker is currently processing this key — retry later."""
    http_status = 409
    error_code = "IDEMPOTENCY_PROCESSING"


# ── Account ────────────────────────────────────────────────────────────────

class AccountNotFound(PaymentGatewayError):
    http_status = 404
    error_code = "ACCOUNT_NOT_FOUND"


class AccountInactive(PaymentGatewayError):
    http_status = 422
    error_code = "ACCOUNT_INACTIVE"


class InsufficientFunds(PaymentGatewayError):
    http_status = 422
    error_code = "INSUFFICIENT_FUNDS"


# ── Transaction ────────────────────────────────────────────────────────────

class TransactionNotFound(PaymentGatewayError):
    http_status = 404
    error_code = "TRANSACTION_NOT_FOUND"


class DoubleEntryViolation(PaymentGatewayError):
    """Debits != credits."""
    http_status = 500
    error_code = "DOUBLE_ENTRY_VIOLATION"


class CurrencyMismatch(PaymentGatewayError):
    http_status = 422
    error_code = "CURRENCY_MISMATCH"


# ── Infrastructure ─────────────────────────────────────────────────────────

class LockAcquisitionFailed(PaymentGatewayError):
    http_status = 503
    error_code = "LOCK_ACQUISITION_FAILED"
