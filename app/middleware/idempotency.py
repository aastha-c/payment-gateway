"""
middleware/idempotency.py
-------------------------
FastAPI middleware that intercepts ALL mutating requests and enforces
exactly-once processing semantics.

Flow:
  1. Skip non-idempotency routes (GET, HEAD, etc.)
  2. Validate Idempotency-Key header exists and is well-formed
  3. Check Redis cache → HIT: return cached response immediately
  4. Acquire Redis distributed lock (SET NX PX)
  5. Check idempotency_keys table (DB source of truth)
     → completed: seed Redis cache, return response
     → processing: another worker is running — 409
     → not found: insert row (ON CONFLICT handles race at DB level)
  6. Call downstream handler
  7. On success: update DB record, write to Redis cache, release lock
  8. On failure: mark DB record failed, release lock

Security note:
  request_hash = SHA-256(canonical_body) prevents a client from sending
  a different payload with the same idempotency key (fraud vector).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.exceptions import (
    IdempotencyConflict,
    IdempotencyKeyMissing,
    IdempotencyKeyTooLong,
    IdempotencyProcessing,
    LockAcquisitionFailed,
)
from app.db.redis_client import IdempotencyCache, RedisLock, get_redis
from app.db.session import AsyncSessionFactory
from app.models.idempotency_key import IdempotencyKey

settings = get_settings()
logger = structlog.get_logger(__name__)


def _requires_idempotency(method: str, path: str) -> bool:
    route_key = f"{method}:{path}"
    return any(
        route_key.startswith(r) for r in settings.IDEMPOTENCY_REQUIRED_ROUTES
    )


def _canonical_body(body: bytes) -> str:
    """
    Produce a stable hash of the request body.
    We sort JSON keys to handle clients that send fields in different order.
    """
    try:
        parsed = json.loads(body)
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        canonical = body.decode("utf-8", errors="replace")
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """
    Starlette-compatible ASGI middleware for idempotency enforcement.
    Must be added BEFORE authentication middleware so we can return
    cached responses without re-authenticating.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # ── Only enforce on mutating, designated routes ────────────────────
        if not _requires_idempotency(request.method, request.url.path):
            return await call_next(request)

        # ── 1. Extract & validate key ──────────────────────────────────────
        idem_key = request.headers.get(settings.IDEMPOTENCY_KEY_HEADER)
        if not idem_key:
            return _error_response(
                400, "IDEMPOTENCY_KEY_MISSING",
                f"Requests to {request.url.path} require an '{settings.IDEMPOTENCY_KEY_HEADER}' header."
            )
        if len(idem_key) > settings.IDEMPOTENCY_KEY_MAX_LEN:
            return _error_response(
                400, "IDEMPOTENCY_KEY_TOO_LONG",
                f"Idempotency key must be ≤ {settings.IDEMPOTENCY_KEY_MAX_LEN} characters."
            )

        path = request.url.path
        body = await request.body()
        request_hash = _canonical_body(body)

        # Bind context vars for structured logging downstream
        structlog.contextvars.bind_contextvars(
            idempotency_key=idem_key,
            request_path=path,
        )

        redis = None
        cache = None
        try:
            redis = await get_redis()
            cache = IdempotencyCache(redis)
        except Exception as exc:
            logger.warning("redis_connect_failed", error=str(exc))

        # ── 2. Redis cache fast-path ───────────────────────────────────────
        if cache:
            try:
                cached = await cache.get(idem_key, path)
                if cached:
                    payload = json.loads(cached)
                    if payload.get("request_hash") and payload["request_hash"] != request_hash:
                        logger.warning("idempotency_conflict_cache", key=idem_key)
                        return _error_response(
                            409, "IDEMPOTENCY_CONFLICT",
                            "Idempotency key reused with a different request body.",
                        )
                    logger.info("idempotency_cache_hit", key=idem_key)
                    return JSONResponse(
                        content=payload["body"],
                        status_code=payload["status_code"],
                        headers={"X-Idempotent-Replayed": "true"},
                    )
            except Exception as exc:
                logger.warning("redis_cache_get_failed", error=str(exc))

        # ── 3. Acquire distributed lock ────────────────────────────────────
        lock = None
        if redis:
            lock = RedisLock(redis, idem_key, path)
            try:
                acquired = await lock.acquire()
            except Exception as exc:
                logger.error("redis_lock_error", error=str(exc))
                acquired = False

            if not acquired:
                # Lock held by another worker — check if it's nearly done
                logger.info("lock_contended", key=idem_key)
                return _error_response(
                    409, "IDEMPOTENCY_PROCESSING",
                    "This request is currently being processed. Retry in 1–2 seconds.",
                    headers={"Retry-After": "2"},
                )

        try:
            async with AsyncSessionFactory() as db:
                # ── 4. DB deduplication guard (authoritative) ────────────
                now = datetime.now(tz=timezone.utc)
                expires = now + timedelta(seconds=settings.REDIS_CACHE_TTL_S)

                stmt = pg_insert(IdempotencyKey).values(
                    id=uuid.uuid4(),
                    key=idem_key,
                    request_path=path,
                    request_hash=request_hash,
                    status="processing",
                    locked_at=now,
                    expires_at=expires,
                )
                # ON CONFLICT DO NOTHING — if another worker already inserted,
                # we get 0 rows affected and fetch the existing record below.
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["key", "request_path"]
                )
                result = await db.execute(stmt)
                await db.flush()

                if result.rowcount == 0:
                    # Row existed — fetch it
                    existing = await db.scalar(
                        select(IdempotencyKey).where(
                            IdempotencyKey.key == idem_key,
                            IdempotencyKey.request_path == path,
                        )
                    )

                    # ── Conflict: same key, different body ──────────────
                    if existing.request_hash != request_hash:
                        logger.warning("idempotency_conflict", key=idem_key)
                        return _error_response(
                            409, "IDEMPOTENCY_CONFLICT",
                            "Idempotency key reused with a different request body.",
                        )

                    # ── Already completed — return cached result ─────────
                    if existing.is_completed and existing.response_body:
                        # Backfill Redis (may have expired from cache)
                        if cache:
                            try:
                                await cache.set(
                                    idem_key, path,
                                    json.dumps({
                                        "status_code": existing.response_status,
                                        "body": existing.response_body,
                                        "request_hash": existing.request_hash,
                                    }),
                                )
                            except Exception:
                                pass
                        return JSONResponse(
                            content=existing.response_body,
                            status_code=existing.response_status,
                            headers={"X-Idempotent-Replayed": "true"},
                        )

                    # ── Still processing (shouldn't happen often) ────────
                    if existing.is_processing:
                        return _error_response(
                            409, "IDEMPOTENCY_PROCESSING",
                            "This request is currently being processed. Retry in 1–2 seconds.",
                            headers={"Retry-After": "2"},
                        )

                await db.commit()

            # ── 5. Process the actual request ────────────────────────────
            response = await call_next(request)

            # Read body (Starlette streams it, we need bytes to cache)
            response_body_bytes = b""
            async for chunk in response.body_iterator:
                response_body_bytes += chunk

            try:
                response_body = json.loads(response_body_bytes)
            except json.JSONDecodeError:
                response_body = {"raw": response_body_bytes.decode("utf-8", errors="replace")}

            # ── 6. Persist result & cache ─────────────────────────────────
            async with AsyncSessionFactory() as db:
                await db.execute(
                    update(IdempotencyKey)
                    .where(
                        IdempotencyKey.key == idem_key,
                        IdempotencyKey.request_path == path,
                    )
                    .values(
                        status="completed" if response.status_code < 500 else "failed",
                        response_status=response.status_code,
                        response_body=response_body,
                        completed_at=datetime.now(tz=timezone.utc),
                    )
                )
                await db.commit()

            if response.status_code < 500 and cache:
                try:
                    await cache.set(
                        idem_key, path,
                        json.dumps({
                            "status_code": response.status_code,
                            "body": response_body,
                            "request_hash": request_hash,
                        }),
                    )
                except Exception:
                    pass

            logger.info(
                "idempotency_completed",
                key=idem_key,
                status_code=response.status_code,
            )
            return Response(
                content=response_body_bytes,
                status_code=response.status_code,
                media_type=response.media_type,
                headers=dict(response.headers),
            )

        except Exception as exc:
            logger.exception("idempotency_middleware_error", error=str(exc))
            # Mark as failed in DB so it can be retried with same key
            try:
                async with AsyncSessionFactory() as db:
                    await db.execute(
                        update(IdempotencyKey)
                        .where(
                            IdempotencyKey.key == idem_key,
                            IdempotencyKey.request_path == path,
                        )
                        .values(status="failed", completed_at=datetime.now(tz=timezone.utc))
                    )
                    await db.commit()
            except Exception:
                pass
            raise

        finally:
            if lock:
                try:
                    await lock.release()
                except Exception:
                    pass


def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    headers: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        content={"error": {"code": error_code, "message": message}},
        status_code=status_code,
        headers=headers,
    )
