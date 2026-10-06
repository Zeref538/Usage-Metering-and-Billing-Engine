"""What each verified Stripe event does to a tenant. Stripe is the source of truth; this mirrors it."""
from psycopg import Connection

from app import repo

ACTIVE = {"active", "trialing"}
PAYMENT_PROBLEM = {"past_due", "unpaid"}


class NotReady(Exception):
    """Retry later. Stripe does not promise order: a subscription.updated can arrive before
    the checkout.session.completed that tells us which customer it belongs to."""


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def apply(con: Connection, event: dict) -> tuple[str, str]:
    """Returns (status, note) where status is 'done' or 'ignored'. Raises to ask for a retry."""
    kind, obj, created = event["type"], event["data"]["object"], event["created"]

    if kind == "checkout.session.completed":
        if obj.get("mode") != "subscription":
            return "ignored", "not a subscription checkout"
        tenant_id = _int(obj.get("client_reference_id") or (obj.get("metadata") or {}).get("tenant_id"))
        if tenant_id is None or repo.tenant_by_id(con, tenant_id) is None:
            return "ignored", "no tenant reference (a checkout not started by this app)"
        repo.set_tenant_billing(con, tenant_id, customer_id=obj.get("customer"))
        paid = obj.get("payment_status") in ("paid", "no_payment_required")
        return _apply_subscription(con, tenant_id, obj["subscription"], "active" if paid else "incomplete", created)

    if kind in ("customer.subscription.updated", "customer.subscription.deleted"):
        tenant = repo.tenant_by_customer(con, obj.get("customer") or "")
        if tenant is None:
            meta_id = _int((obj.get("metadata") or {}).get("tenant_id"))
            if meta_id is None:
                return "ignored", "subscription not created by this app"
            tenant = repo.tenant_by_id(con, meta_id)
            if tenant is None:
                raise NotReady(f"tenant {meta_id} unknown")
            repo.set_tenant_billing(con, meta_id, customer_id=obj.get("customer"))
        status = "canceled" if kind.endswith("deleted") else obj["status"]
        return _apply_subscription(con, tenant["id"], obj["id"], status, created)

    return "ignored", f"{kind} is not handled"


def _apply_subscription(con: Connection, tenant_id: int, subscription_id: str, status: str,
                        created: int) -> tuple[str, str]:
    current = repo.subscription_for_update(con, subscription_id)
    if current and current["last_event_created"] > created:
        # An older event arriving late must not undo a newer one (e.g. "active" after "canceled").
        return "ignored", f"stale: an event from {current['last_event_created']} was already applied"
    plan = "pro" if status in ACTIVE | PAYMENT_PROBLEM else "free"
    billing = "past_due" if status in PAYMENT_PROBLEM else "ok"
    repo.upsert_subscription(con, tenant_id, subscription_id, status, plan, created)
    repo.set_tenant_billing(con, tenant_id, plan_code=plan, billing_status=billing)
    return "done", f"subscription {status}: tenant {tenant_id} is now {plan}"
