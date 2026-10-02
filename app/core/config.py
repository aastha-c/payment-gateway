"""
core/config.py
--------------
Centralised settings loaded from environment variables.
Pydantic-settings validates and coerces all values at startup.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── App ────────────────────────────────────────────────────────────────
    APP_NAME: str = "payment-gateway"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ── PostgreSQL ─────────────────────────────────────────────────────────
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/payment_gateway"
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30

    # ── Redis ──────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_LOCK_TTL_MS: int = 30_000       # 30 s — max time to hold a lock
    REDIS_CACHE_TTL_S: int = 86_400       # 24 h — idempotency response cache

    # ── Idempotency ────────────────────────────────────────────────────────
    IDEMPOTENCY_KEY_HEADER: str = "Idempotency-Key"
    IDEMPOTENCY_KEY_MAX_LEN: int = 255
    # Endpoints that require an idempotency key (method + path prefix)
    IDEMPOTENCY_REQUIRED_ROUTES: list[str] = [
        "POST:/api/v1/transfers",
        "POST:/api/v1/accounts",
    ]

    # ── Security ───────────────────────────────────────────────────────────
    API_KEY_HEADER: str = "X-API-Key"
    API_KEYS: list[str] = ["dev-key-change-in-prod"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton — import this everywhere."""
    return Settings()
