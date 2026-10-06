from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app import config
from app.pricing import PRICING

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def connect(url: str | None = None) -> psycopg.Connection:
    # autocommit, so every multi-step write is an explicit `with con.transaction():`
    # ponytail: one connection per request; add psycopg_pool if traffic grows
    return psycopg.connect(url or config.DATABASE_URL, autocommit=True, row_factory=dict_row)


def migrate(con: psycopg.Connection) -> list[str]:
    """Apply new migrations/*.sql in name order, once each. Returns the ones applied."""
    con.execute("SELECT pg_advisory_lock(726001)")  # api and worker both start by migrating
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                           name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
        done = {r["name"] for r in con.execute("SELECT name FROM schema_migrations")}
        applied = []
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            with con.transaction():
                con.execute(f.read_text(encoding="utf-8"))
                con.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
        sync_plans(con)
        return applied
    finally:
        con.execute("SELECT pg_advisory_unlock(726001)")


def sync_plans(con: psycopg.Connection) -> None:
    """config/pricing.json is the source of truth for plans; the table mirrors it."""
    for code, p in PRICING.plans.items():
        con.execute(
            """INSERT INTO plans (code, name, api_calls_limit, ai_tokens_limit, monthly_fee_cents)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name,
                   api_calls_limit = EXCLUDED.api_calls_limit,
                   ai_tokens_limit = EXCLUDED.ai_tokens_limit,
                   monthly_fee_cents = EXCLUDED.monthly_fee_cents""",
            (code, p.name, p.api_calls, p.ai_tokens, p.monthly_fee_cents))


if __name__ == "__main__":
    with connect() as c:
        print("applied:", migrate(c) or "nothing new")
