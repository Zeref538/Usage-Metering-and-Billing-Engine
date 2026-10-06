"""Metering logic: exactly-once usage events, the quota rule, and the monthly rollup."""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from psycopg import Connection

from app import repo
from app.pricing import PRICING, Tokens, request_cost_micros, token_cost_micros, usd


@dataclass
class Outcome:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)


def month_window(now: datetime) -> tuple[datetime, datetime]:
    """Calendar month in UTC: [first instant of this month, first instant of next)."""
    start = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tokens_used(used: dict) -> int:
    return used["input_tokens"] + used["output_tokens"] + used["reasoning_tokens"]


def record(con: Connection, tenant_id: int, key: str, request_hash: str, tokens: Tokens,
           now: datetime | None = None) -> Outcome:
    """One billable request. Same key + same body = the first answer again, and no new event."""
    now = now or datetime.now(timezone.utc)
    start, end = month_window(now)
    with con.transaction():
        # Everything below runs while this tenant's row is locked, so two copies of the
        # same request (or two requests racing for the last unit of quota) take turns.
        tenant = repo.lock_tenant(con, tenant_id)

        prior = repo.event_by_key(con, tenant_id, key)
        if prior:
            if prior["request_hash"] != request_hash:
                return Outcome(422, {
                    "error": "idempotency_key_reused",
                    "message": "This Idempotency-Key was already used with a different request body. "
                               "A retry must send the same body; a new request needs a new key."})
            return Outcome(201, prior["response"], {"Idempotent-Replayed": "true"})

        plan = PRICING.plans[tenant["plan_code"]]
        if tenant["billing_status"] == "past_due":
            return Outcome(402, {
                "error": "payment_required",
                "message": "The last payment for this subscription failed. Update the payment method "
                           "in Stripe to continue; usage is paused until then.",
                "plan": tenant["plan_code"]})

        used = repo.month_usage(con, tenant_id, start, end)
        exceeded = []
        if used["api_calls"] + 1 > plan.api_calls:
            exceeded.append({"metric": "api_calls", "used": used["api_calls"],
                             "limit": plan.api_calls, "requested": 1})
        if _tokens_used(used) + tokens.quota_units > plan.ai_tokens:
            exceeded.append({"metric": "ai_tokens", "used": _tokens_used(used),
                             "limit": plan.ai_tokens, "requested": tokens.quota_units})
        if exceeded:
            return _over_quota(tenant["plan_code"], plan.name, exceeded, now, end)

        cost = request_cost_micros(tokens)
        event = repo.insert_event(con, tenant_id, key, request_hash, tokens, cost)
        body = {
            "event_id": event["id"],
            "recorded": {"api_calls": 1, "input_tokens": tokens.input,
                         "cached_input_tokens": tokens.cached_input, "output_tokens": tokens.output,
                         "reasoning_tokens": tokens.reasoning, "quota_tokens": tokens.quota_units},
            "cost": {"micros": cost, "usd": usd(cost)},
            "usage_after": {
                "api_calls": {"used": used["api_calls"] + 1, "limit": plan.api_calls},
                "ai_tokens": {"used": _tokens_used(used) + tokens.quota_units, "limit": plan.ai_tokens}},
            "period_end": _iso(end),
        }
        repo.save_response(con, event["id"], body)  # what a retry with this key gets back
        return Outcome(201, body)


def _over_quota(plan_code: str, plan_name: str, exceeded: list[dict], now: datetime, end: datetime) -> Outcome:
    worst = exceeded[0]
    what = f"{worst['used']:,} of {worst['limit']:,} {worst['metric'].replace('_', ' ')} used this month"
    if worst["requested"] > 1:
        what += f", and this request needs {worst['requested']:,} more"
    body = {"error": "quota_exceeded", "plan": plan_code, "exceeded": exceeded, "resets_at": _iso(end)}
    if plan_code == "free":
        # 402: paying fixes it. Nothing was recorded, so the same request succeeds after upgrading.
        body["message"] = f"{plan_name} plan limit reached: {what}. Upgrade to Pro (POST /billing/checkout) " \
                          f"or wait for the reset at {_iso(end)}."
        body["upgrade"] = "POST /billing/checkout"
        return Outcome(402, body)
    # 429: already on the top plan, so only time fixes it. Retry-After says how long.
    body["message"] = f"{plan_name} plan limit reached: {what}. It resets at {_iso(end)}."
    return Outcome(429, body, {"Retry-After": str(int((end - now).total_seconds()))})


def usage_summary(con: Connection, tenant: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start, end = month_window(now)
    used = repo.month_usage(con, tenant["id"], start, end)
    plan = PRICING.plans[tenant["plan_code"]]
    month_tokens = Tokens(input=used["input_tokens"], cached_input=used["cached_input_tokens"],
                          output=used["output_tokens"], reasoning=used["reasoning_tokens"])
    # The month is priced from its summed token counts and rounded once, so per-event
    # rounding can't drift the total. per_event_sum_micros shows that difference.
    calls_micros = used["api_calls"] * PRICING.api_call_micros
    tokens_micros = token_cost_micros(month_tokens)
    total = calls_micros + tokens_micros
    tokens_used = _tokens_used(used)
    return {
        "tenant": {"id": tenant["id"], "name": tenant["name"]},
        "plan": {"code": tenant["plan_code"], "name": plan.name, "billing_status": tenant["billing_status"],
                 "monthly_fee_cents": plan.monthly_fee_cents},
        "period": {"start": _iso(start), "end": _iso(end)},
        "api_calls": {"used": used["api_calls"], "limit": plan.api_calls,
                      "remaining": max(plan.api_calls - used["api_calls"], 0)},
        "ai_tokens": {"used": tokens_used, "limit": plan.ai_tokens,
                      "remaining": max(plan.ai_tokens - tokens_used, 0),
                      "breakdown": {"input": used["input_tokens"], "cached_input": used["cached_input_tokens"],
                                    "output": used["output_tokens"], "reasoning": used["reasoning_tokens"]}},
        "cost": {"api_calls_micros": calls_micros, "ai_tokens_micros": tokens_micros,
                 "usage_micros": total, "usage_usd": usd(total),
                 "per_event_sum_micros": used["event_cost_micros"]},
    }
