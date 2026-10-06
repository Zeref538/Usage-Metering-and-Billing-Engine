# Design: Usage Metering & Billing Engine

## Problem

A SaaS has to answer three questions for every customer: how much have they used, what does it cost, and have they hit their plan's limit. Getting it wrong in either direction costs money: a retried request counted twice overcharges a customer, a missed limit gives the product away.

## Data model

| Table | Holds | Key constraints |
|---|---|---|
| `plans` | Free and Pro, synced from `config/pricing.json` | `code` primary key |
| `tenants` | one customer organisation, its hashed API key, current plan | `api_key_hash` unique, `stripe_customer_id` unique |
| `subscriptions` | the mirror of a Stripe subscription | `stripe_subscription_id` unique |
| `usage_events` | one row per billable request: 1 API call plus its token counts and cost | **`UNIQUE (tenant_id, idempotency_key)`**, index `(tenant_id, created_at)` |
| `stripe_events` | every verified webhook, also the background job queue | `id` (Stripe's event id) primary key |
| `alerts` | jobs that failed for good | |

Money is stored as integers: **micro-dollars** (1,000,000 = $1.00) for usage, cents for plan fees. Never floats.

## API surface

| Method | Path | Does |
|---|---|---|
| POST | `/generate` | the dummy billable action. Needs `Authorization: Bearer <key>` and `Idempotency-Key` |
| GET | `/usage` | this month: used, limit and cost, for the caller's tenant only |
| GET | `/usage/events` | the caller's own usage events |
| POST | `/billing/checkout` | starts a Stripe Checkout (test mode) for Pro |
| POST | `/webhooks/stripe` | verified, deduplicated Stripe events |
| GET | `/health` | asks the database |

## The three hard rules

**Exactly-once metering.** The tenant row is locked (`SELECT ... FOR UPDATE`) for the whole decision: look up the key, check the quota, insert. A retry with the same key finds the first event and gets its stored response back, byte for byte. The same key with a different body is a 422, because it is a client bug, not a retry. The unique constraint is the backstop if the lock is ever removed.

**Boundary.** A request is allowed when `used + requested <= limit`. With 1,000 calls, the 1,000th is allowed and the 1,001st is refused. Free over its limit gets **402** (upgrading fixes it). Pro over its limit gets **429** with `Retry-After` until the month resets (there is nothing to upgrade to). A tenant whose payment failed gets **402** on every billable request. Refused requests record nothing, so a retry after upgrading succeeds.

**Token pricing.** The client reports `input_tokens` (all of them, cached included, the way providers report it), `cached_input_tokens` (a subset of input, priced lower), `output_tokens` and `reasoning_tokens` (billed at the output price). Adding all four together would count cached tokens twice and price reasoning as input.

## Layers

```
app/main.py         HTTP: routes, auth, validation, status codes
app/metering.py     logic: idempotency, quota, the 402/429 rule
app/pricing.py      logic: pure money math, no database
app/stripe_sync.py  logic: what each Stripe event does to a tenant
app/repo.py         data: every SQL statement
app/worker.py       background job: processes stripe_events with retries
```

## Non-goal

No invoices, proration or overage billing. Over the limit means refused, not charged extra.
