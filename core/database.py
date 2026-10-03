"""
core/database.py
----------------
Async SQLAlchemy engine + session factory.
Supports both SQLite (dev) and PostgreSQL (prod) transparently.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, AsyncAdaptedQueuePool

from sqlalchemy import event
from core.config import settings

# ── Engine ──────────────────────────────────────────────────────────────────
# NullPool for SQLite (no connection pooling needed for file-based DB).
# AsyncAdaptedQueuePool for PostgreSQL for production performance.
_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_async_engine(
    settings.database_url,
    echo=settings.app_env == "development",
    poolclass=NullPool if _is_sqlite else AsyncAdaptedQueuePool,
    connect_args={"check_same_thread": False, "timeout": 60} if _is_sqlite else {},
)

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        try:
            cursor.execute("PRAGMA table_info(videos)")
            cols = [row[1] for row in cursor.fetchall()]
            if cols and "thumbnail_id" not in cols:
                cursor.execute(
                    "ALTER TABLE videos ADD COLUMN thumbnail_id INTEGER REFERENCES thumbnails(id) ON DELETE SET NULL"
                )
                dbapi_connection.commit()
        except Exception:
            pass
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager variant for use in scheduler/services."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a DB session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def create_all_tables() -> None:
    """Create all tables on startup (dev mode). In production use Alembic."""
    from models.base import Base  # avoid circular at module level
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _migrate(sync_conn):
            try:
                res = sync_conn.exec_driver_sql("PRAGMA table_info(videos)")
                cols = [row[1] for row in res.fetchall()]
                if cols and "thumbnail_id" not in cols:
                    sync_conn.exec_driver_sql("ALTER TABLE videos ADD COLUMN thumbnail_id INTEGER REFERENCES thumbnails(id) ON DELETE SET NULL")
            except Exception:
                pass


        if _is_sqlite:
            await conn.run_sync(_migrate)

