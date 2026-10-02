"""
main.py — FastAPI application factory
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import accounts, transfers
from app.core.config import get_settings
from app.core.exceptions import PaymentGatewayError
from app.core.logging import configure_logging, get_logger
from app.db.redis_client import close_redis, get_redis
from app.middleware.idempotency import IdempotencyMiddleware

settings = get_settings()
configure_logging(settings.LOG_LEVEL)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle handler."""
    logger.info("startup", app=settings.APP_NAME, version=settings.APP_VERSION)
    try:
        await get_redis()
    except Exception as exc:
        logger.warning("redis_warm_skipped", error=str(exc))
    yield
    logger.info("shutdown")
    try:
        await close_redis()
    except Exception:
        pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="Payment Gateway API",
        version=settings.APP_VERSION,
        description=(
            "High-throughput idempotent payment gateway with "
            "double-entry bookkeeping and distributed locking."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── Middleware (order matters: outermost runs first) ──────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(IdempotencyMiddleware)

    # ── Global exception handler ──────────────────────────────────────────
    @app.exception_handler(PaymentGatewayError)
    async def domain_error_handler(request: Request, exc: PaymentGatewayError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.error_code, "message": exc.message, "detail": exc.detail}},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("unhandled_error", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}},
        )

    # ── Routers ───────────────────────────────────────────────────────────
    prefix = "/api/v1"
    app.include_router(accounts.router, prefix=prefix)
    app.include_router(transfers.router, prefix=prefix)

    # ── Health check ──────────────────────────────────────────────────────
    @app.get("/health", tags=["Health"])
    async def health():
        return {"status": "ok", "version": settings.APP_VERSION}

    return app


app = create_app()
