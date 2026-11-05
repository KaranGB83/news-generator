import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.db import Base


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    """Let the Postgres-only JSONB column work on SQLite in tests."""
    return "JSON"


@pytest_asyncio.fixture
async def session_factory():
    """A fresh in-memory SQLite database per test, with the real tables and constraints."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
