"""Background job: applies stored Stripe events, with retries and a failure alert.

The webhook endpoint only verifies and stores (fast, so Stripe never times out on us);
this process does the work. Run: python -m app.worker
"""
import logging
import time

import psycopg

from app import db, repo, stripe_sync

log = logging.getLogger("worker")
MAX_ATTEMPTS = 5


def run_once(con: psycopg.Connection) -> bool:
    """Process one due event. Returns False when there was nothing to do."""
    with con.transaction():
        ev = repo.claim_due_event(con)
        if ev is None:
            return False
        try:
            with con.transaction():  # a savepoint: a failing handler undoes only its own writes
                status, note = stripe_sync.apply(con, ev["payload"])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            attempt = ev["attempts"] + 1
            if attempt >= MAX_ATTEMPTS:
                repo.fail_event(con, ev["id"], error)
                repo.add_alert(con, "stripe_event_failed",
                               f"{ev['id']} ({ev['type']}) failed {attempt} times, giving up: {error}")
                log.error("ALERT %s (%s) failed %d times: %s", ev["id"], ev["type"], attempt, error)
            else:
                delay = 2 ** attempt  # 2, 4, 8, 16 seconds
                repo.retry_event_later(con, ev["id"], error, delay)
                log.warning("%s attempt %d failed, retrying in %ds: %s", ev["id"], attempt, delay, error)
        else:
            repo.finish_event(con, ev["id"], status, note)
            log.info("%s %s -> %s: %s", ev["id"], ev["type"], status, note)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    while True:
        try:
            with db.connect() as con:
                db.migrate(con)
                log.info("worker ready")
                while True:
                    if not run_once(con):
                        time.sleep(1)
        except psycopg.OperationalError as exc:
            log.warning("database unavailable (%s), reconnecting in 3s", exc)
            time.sleep(3)


if __name__ == "__main__":
    main()
