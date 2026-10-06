"""Demo data: two tenants on the Free plan. Safe to run twice.

    python -m app.seed            add the demo tenants if missing
    python -m app.seed --reset    also wipe all usage, subscriptions and events (a fresh demo)
"""
import os
import sys

from app import db, repo

# Local demo keys. Their values come from .env (see .env.example); only their hash is stored.
DEMO_TENANTS = [("Acme (demo)", "DEMO_KEY_ACME", "demo_acme_local_key"),
                ("Globex (demo)", "DEMO_KEY_GLOBEX", "demo_globex_local_key")]


def main(reset: bool) -> None:
    with db.connect() as con:
        db.migrate(con)
        with con.transaction():
            if reset:
                con.execute("TRUNCATE usage_events, subscriptions, stripe_events, alerts")
                con.execute("UPDATE tenants SET plan_code = 'free', billing_status = 'ok', stripe_customer_id = NULL")
            for name, env, default in DEMO_TENANTS:
                t = repo.create_tenant(con, name, os.environ.get(env, default))
                print(f"tenant {t['id']}: {name}, plan {t['plan_code']}, API key = ${env}")
    print("reset: usage, subscriptions and events cleared" if reset else "seeded")


if __name__ == "__main__":
    main("--reset" in sys.argv)
