import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# ============================================================
# Database URL
# asyncpg driver for async PostgreSQL
# ============================================================
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://erp_user:erp_password@localhost:5432/erp_db"
).replace("postgresql://", "postgresql+asyncpg://")


# ============================================================
# Async engine
# pool_size: max connections in pool
# max_overflow: extra connections allowed above pool_size
# pool_pre_ping: verify connection before use
# ============================================================
engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=False,  # set True to log all SQL (debug only)
)


# ============================================================
# Session factory
# expire_on_commit=False: keep objects usable after commit
# ============================================================
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ============================================================
# Base class for all SQLAlchemy models
# ============================================================
class Base(DeclarativeBase):
    pass


# ============================================================
# Dependency — yields DB session per request
# Used in FastAPI route functions via Depends(get_db)
# Rolls back on exception, always closes session
# ============================================================
async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ============================================================
# Context manager for service layer transactions
# Used when we need explicit transaction control
# e.g. payment allocation (serializable isolation)
# ============================================================
async def get_db_transaction(isolation_level: str = "read_committed"):
    async with AsyncSessionLocal() as session:
        await session.execute(
            f"SET TRANSACTION ISOLATION LEVEL {isolation_level.upper()}"
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
