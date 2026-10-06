"""Stripe webhooks: signature check, dedup, plan sync, ordering, and the worker's retries.

Events are signed here exactly the way Stripe signs them (HMAC-SHA256 over "timestamp.payload"
with the whsec_ secret), so the real stripe.Webhook.construct_event does the verifying."""
import hashlib
import hmac
import json
import time

from app import config, worker

_n = iter(range(1, 10**9))


def sign(payload: bytes, secret: str | None = None, t: int | None = None) -> str:
    t = t or int(time.time())
    mac = hmac.new((secret or config.STRIPE_WEBHOOK_SECRET).encode(), f"{t}.".encode() + payload, hashlib.sha256)
    return f"t={t},v1={mac.hexdigest()}"


def event(kind: str, obj: dict, created: int | None = None) -> dict:
    return {"id": f"evt_test_{next(_n)}_{time.time_ns()}", "object": "event", "type": kind,
            "created": created or int(time.time()), "data": {"object": obj}}


def checkout_completed(tenant_id: int, sub: str, customer: str, created=None) -> dict:
    return event("checkout.session.completed", {
        "id": f"cs_test_{sub}", "object": "checkout.session", "mode": "subscription",
        "client_reference_id": str(tenant_id), "customer": customer, "subscription": sub,
        "payment_status": "paid", "metadata": {"tenant_id": str(tenant_id)}}, created)


def subscription(kind: str, sub: str, customer: str, status: str, tenant_id=None, created=None) -> dict:
    meta = {"tenant_id": str(tenant_id)} if tenant_id else {}
    return event(f"customer.subscription.{kind}", {"id": sub, "object": "subscription", "customer": customer,
                                                   "status": status, "metadata": meta}, created)


def post(client, ev: dict, signature: str | None = None):
    payload = json.dumps(ev).encode()
    return client.post("/webhooks/stripe", content=payload,
                       headers={"Stripe-Signature": signature or sign(payload), "Content-Type": "application/json"})


def drain(con) -> None:
    while worker.run_once(con):
        pass


def plan_of(con, tenant_id):
    return con.execute("SELECT plan_code, billing_status, stripe_customer_id FROM tenants WHERE id = %s",
                       (tenant_id,)).fetchone()


# --- signature -----------------------------------------------------------------

def test_a_forged_webhook_is_400_and_changes_nothing(client, con, make_tenant):
    t, _ = make_tenant()
    ev = checkout_completed(t["id"], "sub_forged", "cus_forged")
    payload = json.dumps(ev).encode()
    for bad in (sign(payload, secret="whsec_attacker"),            # wrong secret
                sign(b'{"tampered": true}'),                         # signed a different body
                sign(payload, t=int(time.time()) - 3600),            # too old: a replayed capture
                "garbage"):
        r = client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": bad})
        assert r.status_code == 400 and r.json()["error"] == "invalid_signature"
    assert client.post("/webhooks/stripe", content=payload).status_code == 400   # no header at all
    drain(con)
    assert plan_of(con, t["id"])["plan_code"] == "free"
    assert con.execute("SELECT COUNT(*) AS n FROM stripe_events WHERE id = %s", (ev["id"],)).fetchone()["n"] == 0


# --- the happy path and dedup ---------------------------------------------------------

def test_checkout_flips_free_to_pro_and_usage_shows_the_new_limits(client, con, make_tenant):
    t, key = make_tenant()
    r = post(client, checkout_completed(t["id"], "sub_happy", "cus_happy"))
    assert r.status_code == 200 and r.json()["duplicate"] is False
    drain(con)
    assert plan_of(con, t["id"]) == {"plan_code": "pro", "billing_status": "ok", "stripe_customer_id": "cus_happy"}
    usage = client.get("/usage", headers={"Authorization": f"Bearer {key}"}).json()
    assert usage["plan"]["code"] == "pro" and usage["api_calls"]["limit"] == 50_000


def test_a_replayed_event_is_processed_once(client, con, make_tenant):
    t, _ = make_tenant()
    ev = checkout_completed(t["id"], "sub_replay", "cus_replay")
    assert post(client, ev).json()["duplicate"] is False
    assert post(client, ev).json()["duplicate"] is True
    assert post(client, ev).json()["duplicate"] is True
    drain(con)
    row = con.execute("SELECT status, attempts FROM stripe_events WHERE id = %s", (ev["id"],)).fetchone()
    assert row == {"status": "done", "attempts": 1}


def test_cancel_drops_back_to_free_and_a_failed_payment_marks_past_due(client, con, make_tenant):
    t, _ = make_tenant()
    now = int(time.time())
    post(client, checkout_completed(t["id"], "sub_life", "cus_life", created=now))
    post(client, subscription("updated", "sub_life", "cus_life", "past_due", created=now + 1))
    drain(con)
    assert plan_of(con, t["id"])["billing_status"] == "past_due"
    post(client, subscription("deleted", "sub_life", "cus_life", "canceled", created=now + 2))
    drain(con)
    assert plan_of(con, t["id"])["plan_code"] == "free"


# --- ordering ------------------------------------------------------------------------

def test_an_old_event_arriving_late_cannot_undo_a_newer_one(client, con, make_tenant):
    t, _ = make_tenant()
    now = int(time.time())
    post(client, checkout_completed(t["id"], "sub_order", "cus_order", created=now))
    post(client, subscription("deleted", "sub_order", "cus_order", "canceled", created=now + 10))
    drain(con)
    late = subscription("updated", "sub_order", "cus_order", "active", created=now + 5)
    post(client, late)
    drain(con)
    assert plan_of(con, t["id"])["plan_code"] == "free"
    note = con.execute("SELECT status, note FROM stripe_events WHERE id = %s", (late["id"],)).fetchone()
    assert note["status"] == "ignored" and note["note"].startswith("stale")


def test_subscription_update_before_checkout_completed_still_lands(client, con, make_tenant):
    t, _ = make_tenant()
    now = int(time.time())
    # Stripe doesn't promise order: the subscription event can come first. Its metadata
    # carries the tenant id (set by our checkout), so it applies without waiting.
    post(client, subscription("updated", "sub_early", "cus_early", "active", tenant_id=t["id"], created=now))
    drain(con)
    assert plan_of(con, t["id"]) == {"plan_code": "pro", "billing_status": "ok", "stripe_customer_id": "cus_early"}


def test_events_this_app_did_not_start_are_ignored(client, con):
    stray = event("checkout.session.completed", {"id": "cs_x", "mode": "payment", "customer": "cus_x"})
    other = event("invoice.paid", {"id": "in_x"})
    post(client, stray)
    post(client, other)
    drain(con)
    statuses = {r["status"] for r in con.execute("SELECT status FROM stripe_events WHERE id IN (%s, %s)",
                                                 (stray["id"], other["id"]))}
    assert statuses == {"ignored"}


# --- the background job: retries and the alert --------------------------------------------

def test_a_failing_event_is_retried_then_alerted(client, con, make_tenant):
    # The subscription names tenant 999999999, which doesn't exist yet: NotReady, retry.
    ev = subscription("updated", "sub_ghost", "cus_ghost", "active", tenant_id=999_999_999)
    post(client, ev)
    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        con.execute("UPDATE stripe_events SET next_attempt_at = now() WHERE id = %s", (ev["id"],))  # skip the wait
        worker.run_once(con)
        row = con.execute("SELECT status, attempts, last_error FROM stripe_events WHERE id = %s",
                          (ev["id"],)).fetchone()
        assert row["attempts"] == attempt
    assert row["status"] == "failed" and "NotReady" in row["last_error"]
    alert = con.execute("SELECT message FROM alerts WHERE message LIKE %s", (f"{ev['id']}%",)).fetchone()
    assert alert and "failed 5 times" in alert["message"]


def test_retry_waits_with_backoff(client, con):
    ev = subscription("updated", "sub_wait", "cus_wait", "active", tenant_id=999_999_998)
    post(client, ev)
    worker.run_once(con)
    row = con.execute("SELECT next_attempt_at - now() AS wait FROM stripe_events WHERE id = %s",
                      (ev["id"],)).fetchone()
    assert 1 < row["wait"].total_seconds() <= 2      # first retry after 2 seconds
    worker.run_once(con)                              # not due yet: the claim skips it
    still = con.execute("SELECT attempts FROM stripe_events WHERE id = %s", (ev["id"],)).fetchone()
    assert still["attempts"] == 1
