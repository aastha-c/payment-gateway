"""
db/session.py
-------------
Async SQLAlchemy engine + session factory.
One engine shared across the whole process (connection pooling).
"""
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()

# NullPool is used in tests to avoid event-loop issues.
# In production, use default pool with DB_POOL_SIZE.
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=True,         # evict stale connections
    echo=settings.DEBUG,
)

AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,     # avoid lazy-load after commit
    autoflush=False,
)


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency-injectable async session context manager.

    Usage in FastAPI:
        async def endpoint(db: AsyncSession = Depends(get_db)):
            ...

    Usage in services (non-FastAPI context):
        async with get_db_session() as db:
            ...
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends()-compatible version."""
    async with get_db_session() as session:
        yield session
