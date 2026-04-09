"""Database engine and session factory — auto-selects SQLite or PostgreSQL."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all models."""


def _build_engine():
    """Build the async SQLAlchemy engine based on configuration."""
    settings = get_settings()
    url = settings.effective_database_url

    # Convert sync postgres URL to async variant if needed
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)

    connect_args = {}
    if settings.is_sqlite:
        connect_args["check_same_thread"] = False

    return create_async_engine(
        url,
        echo=False,
        connect_args=connect_args,
    )


engine = _build_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db():
    """FastAPI dependency that yields a database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables if they do not exist, and add any missing columns (idempotent)."""
    import app.models  # noqa: F401 — ensure models are registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns that may be missing from existing databases
        await _migrate_add_columns(conn)


async def _migrate_add_columns(conn) -> None:
    """Add any columns that were introduced after initial table creation."""
    from sqlalchemy import text

    # List of (table, column, column_def) tuples to ensure exist
    new_columns = [
        ("companies", "resultat_efter_finansnetto", "BIGINT"),
        ("batches", "list_name", "VARCHAR(255)"),
        ("batches", "list_description", "TEXT"),
    ]

    settings = get_settings()
    for table, col, col_def in new_columns:
        try:
            if settings.is_sqlite:
                # SQLite: check via PRAGMA
                result = await conn.execute(text(f"PRAGMA table_info({table})"))
                existing = {row[1] for row in result.fetchall()}
                if col not in existing:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"))
            else:
                # PostgreSQL: check information_schema
                result = await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{table}' AND column_name='{col}'"
                ))
                if not result.fetchone():
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_def}"))
        except Exception:
            pass  # Column likely already exists or table doesn't exist yet
