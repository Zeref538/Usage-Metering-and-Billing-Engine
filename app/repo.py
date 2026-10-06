"""Data layer: every SQL statement lives here. Every tenant query filters on tenant_id."""
import hashlib
from datetime import datetime

from psycopg import Connection
from psycopg.types.json import Jsonb


def hash_key(api_key: str) -> str:
    # API keys are long random strings, so a fast hash is enough (no password stretching needed)
    return hashlib.sha256(api_key.encode()).hexdigest()


# --- tenants -------------------------------------------------------------------

def tenant_by_key(con: Connection, api_key: str) -> dict | None:
    return con.execute("SELECT * FROM tenants WHERE api_key_hash = %s", (hash_key(api_key),)).fetchone()


def lock_tenant(con: Connection, tenant_id: int) -> dict:
    """Row lock held until the transaction ends: one metering decision per tenant at a time."""
    return con.execute("SELECT * FROM tenants WHERE id = %s FOR UPDATE", (tenant_id,)).fetchone()


def create_tenant(con: Connection, name: str, api_key: str, plan_code: str = "free") -> dict:
    return con.execute(
        """INSERT INTO tenants (name, api_key_hash, plan_code) VALUES (%s, %s, %s)
           ON CONFLICT (api_key_hash) DO UPDATE SET name = EXCLUDED.name
           RETURNING *""", (name, hash_key(api_key), plan_code)).fetchone()


def tenant_by_customer(con: Connection, customer_id: str) -> dict | None:
    return con.execute("SELECT * FROM tenants WHERE stripe_customer_id = %s", (customer_id,)).fetchone()


def tenant_by_id(con: Connection, tenant_id: int) -> dict | None:
    return con.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,)).fetchone()


def set_tenant_billing(con: Connection, tenant_id: int, *, plan_code: str | None = None,
                       billing_status: str | None = None, customer_id: str | None = None) -> None:
    con.execute(
        """UPDATE tenants SET plan_code = COALESCE(%s, plan_code),
                              billing_status = COALESCE(%s, billing_status),
                              stripe_customer_id = COALESCE(%s, stripe_customer_id)
           WHERE id = %s""", (plan_code, billing_status, customer_id, tenant_id))


# --- usage ---------------------------------------------------------------------

def event_by_key(con: Connection, tenant_id: int, key: str) -> dict | None:
    return con.execute("SELECT * FROM usage_events WHERE tenant_id = %s AND idempotency_key = %s",
                       (tenant_id, key)).fetchone()


def month_usage(con: Connection, tenant_id: int, start: datetime, end: datetime) -> dict:
    """Uses the (tenant_id, created_at) index."""
    return con.execute(
        """-- SUM(bigint) returns numeric (a Decimal in Python); cast back so money stays int
           SELECT COUNT(*)                                   AS events,
                  COALESCE(SUM(api_calls), 0)::bigint         AS api_calls,
                  COALESCE(SUM(input_tokens), 0)::bigint      AS input_tokens,
                  COALESCE(SUM(cached_input_tokens), 0)::bigint AS cached_input_tokens,
                  COALESCE(SUM(output_tokens), 0)::bigint     AS output_tokens,
                  COALESCE(SUM(reasoning_tokens), 0)::bigint  AS reasoning_tokens,
                  COALESCE(SUM(cost_micros), 0)::bigint       AS event_cost_micros
           FROM usage_events
           WHERE tenant_id = %s AND created_at >= %s AND created_at < %s""",
        (tenant_id, start, end)).fetchone()


def insert_event(con: Connection, tenant_id: int, key: str, request_hash: str, tokens, cost_micros: int) -> dict:
    return con.execute(
        """INSERT INTO usage_events (tenant_id, idempotency_key, request_hash, api_calls, input_tokens,
                                     cached_input_tokens, output_tokens, reasoning_tokens, cost_micros)
           VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s) RETURNING *""",
        (tenant_id, key, request_hash, tokens.input, tokens.cached_input, tokens.output,
         tokens.reasoning, cost_micros)).fetchone()


def save_response(con: Connection, event_id: int, body: dict) -> None:
    con.execute("UPDATE usage_events SET response = %s WHERE id = %s", (Jsonb(body), event_id))


def list_events(con: Connection, tenant_id: int, limit: int) -> list[dict]:
    return con.execute(
        """SELECT id, idempotency_key, api_calls, input_tokens, cached_input_tokens, output_tokens,
                  reasoning_tokens, cost_micros, created_at
           FROM usage_events WHERE tenant_id = %s ORDER BY id DESC LIMIT %s""",
        (tenant_id, limit)).fetchall()


# --- stripe events (the job queue) ---------------------------------------------

def store_stripe_event(con: Connection, event: dict) -> bool:
    """True if new. A replayed event id hits the primary key and inserts nothing."""
    row = con.execute(
        """INSERT INTO stripe_events (id, type, created, payload) VALUES (%s, %s, %s, %s)
           ON CONFLICT (id) DO NOTHING RETURNING id""",
        (event["id"], event["type"], event["created"], Jsonb(event))).fetchone()
    return row is not None


def claim_due_event(con: Connection) -> dict | None:
    """SKIP LOCKED: two workers never take the same event."""
    return con.execute(
        """SELECT * FROM stripe_events WHERE status = 'pending' AND next_attempt_at <= now()
           ORDER BY received_at LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()


def finish_event(con: Connection, event_id: str, status: str, note: str | None) -> None:
    con.execute(
        """UPDATE stripe_events SET status = %s, note = %s, attempts = attempts + 1,
                                    processed_at = now(), last_error = NULL
           WHERE id = %s""", (status, note, event_id))


def retry_event_later(con: Connection, event_id: str, error: str, delay_s: int) -> None:
    con.execute(
        """UPDATE stripe_events SET attempts = attempts + 1, last_error = %s,
                                    next_attempt_at = now() + make_interval(secs => %s)
           WHERE id = %s""", (error, delay_s, event_id))


def fail_event(con: Connection, event_id: str, error: str) -> None:
    con.execute(
        """UPDATE stripe_events SET status = 'failed', attempts = attempts + 1, last_error = %s,
                                    processed_at = now() WHERE id = %s""", (error, event_id))


def add_alert(con: Connection, kind: str, message: str) -> None:
    con.execute("INSERT INTO alerts (kind, message) VALUES (%s, %s)", (kind, message))


# --- subscriptions -------------------------------------------------------------

def subscription_for_update(con: Connection, stripe_subscription_id: str) -> dict | None:
    return con.execute("SELECT * FROM subscriptions WHERE stripe_subscription_id = %s FOR UPDATE",
                       (stripe_subscription_id,)).fetchone()


def upsert_subscription(con: Connection, tenant_id: int, stripe_subscription_id: str, status: str,
                        plan_code: str, event_created: int) -> None:
    con.execute(
        """INSERT INTO subscriptions (tenant_id, stripe_subscription_id, status, plan_code, last_event_created)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (stripe_subscription_id) DO UPDATE SET status = EXCLUDED.status,
               plan_code = EXCLUDED.plan_code, last_event_created = EXCLUDED.last_event_created,
               updated_at = now()""",
        (tenant_id, stripe_subscription_id, status, plan_code, event_created))
