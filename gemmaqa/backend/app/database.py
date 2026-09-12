"""Database engine and session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base
from app.utils.logging import get_logger

logger = get_logger("db")

settings = get_settings()

engine = create_async_engine(
    settings.effective_database_url,
    echo=settings.debug,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Create all tables if they do not exist; apply lightweight SQLite migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_sqlite_columns)
    logger.info("Database initialized")


def _ensure_sqlite_columns(sync_conn) -> None:
    """Add new columns / indexes to existing SQLite DBs without Alembic."""
    try:
        rows = sync_conn.exec_driver_sql("PRAGMA table_info(qa_runs)").fetchall()
        cols = {r[1] for r in rows}
        if "application_json" not in cols:
            sync_conn.exec_driver_sql(
                "ALTER TABLE qa_runs ADD COLUMN application_json TEXT"
            )
            logger.info("Migrated qa_runs.application_json")
    except Exception as exc:
        logger.warning("SQLite qa_runs migration skipped: %s", type(exc).__name__)

    try:
        page_cols = {
            r[1]
            for r in sync_conn.exec_driver_sql("PRAGMA table_info(pages)").fetchall()
        }
        if page_cols:
            if "visit_count" not in page_cols:
                sync_conn.exec_driver_sql(
                    "ALTER TABLE pages ADD COLUMN visit_count INTEGER DEFAULT 1"
                )
            if "exploration_status" not in page_cols:
                sync_conn.exec_driver_sql(
                    "ALTER TABLE pages ADD COLUMN exploration_status VARCHAR(32) DEFAULT 'discovered'"
                )
            if "last_seen_at" not in page_cols:
                sync_conn.exec_driver_sql(
                    "ALTER TABLE pages ADD COLUMN last_seen_at DATETIME"
                )
            # Deduplicate before unique index (keep earliest row per run_id+url)
            sync_conn.exec_driver_sql(
                """
                DELETE FROM pages
                WHERE id NOT IN (
                    SELECT MIN(id) FROM pages GROUP BY run_id, url
                )
                """
            )
            sync_conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_pages_run_canonical_url "
                "ON pages (run_id, url)"
            )
            logger.info("Migrated pages uniqueness (run_id, url)")
    except Exception as exc:
        logger.warning("SQLite pages migration skipped: %s", type(exc).__name__)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
