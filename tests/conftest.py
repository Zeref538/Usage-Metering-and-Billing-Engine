"""Tests run against a real Postgres, in a separate `billing_test` database that is
rebuilt from the migrations at the start of every run. Your demo data is never touched."""
import os

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app import config  # loads .env first, so DATABASE_URL below is the real one

TEST_DB = "billing_test"
_base = os.environ.get("DATABASE_URL", config.DATABASE_URL)
TEST_URL = make_conninfo(_base, dbname=TEST_DB)
config.DATABASE_URL = TEST_URL
config.STRIPE_WEBHOOK_SECRET = "whsec_test_secret_for_tests_only"
config.STRIPE_SECRET_KEY = ""

import pytest  # noqa: E402

from app import db, repo  # noqa: E402


def pytest_sessionstart(session):
    admin = make_conninfo(_base, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        con.execute(f"CREATE DATABASE {TEST_DB}")
    with db.connect(TEST_URL) as con:
        db.migrate(con)
    print(f"\ntest database: {conninfo_to_dict(TEST_URL).get('host')}/{TEST_DB}, migrations applied")


@pytest.fixture
def con():
    with db.connect(TEST_URL) as c:
        yield c


_counter = iter(range(1, 10**9))


@pytest.fixture
def make_tenant(con):
    """A fresh tenant per test, so no test sees another's usage."""
    def make(plan: str = "free") -> tuple[dict, str]:
        key = f"test_key_{os.getpid()}_{next(_counter)}"
        t = repo.create_tenant(con, f"tenant {key}", key, plan)
        return t, key
    return make


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)
