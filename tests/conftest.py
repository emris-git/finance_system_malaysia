"""Test setup.

Parser tests are pure. Ledger/API tests need Postgres: TEST_DATABASE_URL
(default: the docker container from the README) and are skipped without it.
"""

import os

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://finance:finance@localhost:55432/finance_test")
os.environ["DATABASE_URL"] = TEST_DB
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("API_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_OWNER_ID", "42")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["OWNER_NAME"] = "ALEX MORGAN"

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from finance.db import Base, get_engine, get_sessionmaker  # noqa: E402
from finance.seed import seed  # noqa: E402


@pytest.fixture(scope="session")
async def db_ready():
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except OSError as exc:
        pytest.skip(f"Postgres unavailable: {exc}")
    yield
    await engine.dispose()


@pytest.fixture
async def session(db_ready):
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    async with get_sessionmaker()() as s:
        await seed(s)
        yield s
