"""
tests/conftest.py
-----------------
Shared pytest fixtures for both unit and integration tests.

Fixtures provided:
  - settings          : app settings override for test env
  - async_engine      : creates all tables against in-memory SQLite (StaticPool)
  - test_session_factory : async_sessionmaker bound to test engine
  - db_session        : clean session per test with post-test table truncation
  - redis_mock        : mock Redis client for unit testing
  - test_client       : async httpx client pointed at the FastAPI app
  - account_factory   : creates Account rows with configurable balances
"""
from __future__ import annotations

import asyncio
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import app.db.session
import app.middleware.idempotency
import app.models
from app.core.config import Settings
from app.db.session import get_db
from app.main import create_app
from app.models.account import Account
from app.models.base import Base


# ── Override settings for tests ───────────────────────────────────────────────
@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return Settings(
        DATABASE_URL="sqlite+aiosqlite://",
        REDIS_URL="redis://localhost:6379/1",
        DEBUG=True,
        LOG_LEVEL="WARNING",
        API_KEYS=["test-api-key"],
    )


# ── Async event loop (session-scoped) ─────────────────────────────────────────
@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Async engine + schema creation (session-scoped StaticPool) ────────────────
@pytest_asyncio.fixture(scope="session")
async def async_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def test_session_factory(async_engine):
    factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    # Monkey-patch session factories across the application for testing
    app.db.session.AsyncSessionFactory = factory
    app.middleware.idempotency.AsyncSessionFactory = factory
    return factory


# ── Per-test isolated session with cleanup ────────────────────────────────────
@pytest_asyncio.fixture
async def db_session(async_engine, test_session_factory) -> AsyncGenerator[AsyncSession, None]:
    async with test_session_factory() as session:
        yield session
        try:
            await session.commit()
        except Exception:
            await session.rollback()

    # Clean up all table contents after each test for clean isolation
    async with async_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


# ── FakeRedis for unit tests (no real Redis required) ────────────────────────
@pytest.fixture
def redis_mock():
    mock = AsyncMock()
    mock.set.return_value = True
    mock.get.return_value = None
    mock.delete.return_value = 1
    mock.eval.return_value = 1
    return mock


# ── FastAPI test client ───────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def test_client(test_session_factory) -> AsyncGenerator[AsyncClient, None]:
    import fakeredis.aioredis as fakeredis
    from unittest.mock import patch

    app = create_app()

    async def override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    with patch("app.middleware.idempotency.get_redis", return_value=fake_redis), \
         patch("app.db.redis_client.get_redis", return_value=fake_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


# ── Account factory fixture ───────────────────────────────────────────────────
@pytest_asyncio.fixture
async def account_factory(db_session: AsyncSession):
    async def _create(
        balance: int = 0,
        currency: str = "USD",
        account_type: str = "asset",
        is_active: bool = True,
        owner_id: uuid.UUID | None = None,
    ) -> Account:
        account = Account(
            owner_id=owner_id or uuid.uuid4(),
            currency=currency,
            account_type=account_type,
            balance=balance,
            is_active=is_active,
        )
        db_session.add(account)
        await db_session.flush()
        await db_session.commit()
        await db_session.refresh(account)
        return account

    return _create
