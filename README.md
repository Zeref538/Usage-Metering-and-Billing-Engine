# Usage Metering & Billing Engine

The backend every SaaS needs: how much has this customer used, what does it cost, and have they hit their limit. It meters usage exactly once under retries, refuses over-quota requests with honest 402/429 answers, prices AI tokens with the real cached-input and reasoning rules in integer money, and keeps plans in sync with Stripe (test mode) through verified, deduplicated webhooks.

FlyRank backend track capstone. Python, FastAPI, PostgreSQL, Stripe test mode. Design: [DESIGN.md](DESIGN.md). Proof for every requirement: [EVIDENCE.md](EVIDENCE.md).

## Run it

Needs Docker. Nothing else is installed on your machine.

```bash
cp .env.example .env
docker compose up -d --build --wait                    # db + api + worker
docker compose exec api python -m app.seed --reset     # two demo tenants on Free
python scripts/probes.py                               # the brief's 5 acceptance probes
docker compose exec api python -m pytest -q            # 30 tests, in a separate billing_test database
```

The API is on http://localhost:8000, interactive docs at http://localhost:8000/docs. The demo keys are in `.env` (`DEMO_KEY_ACME`, `DEMO_KEY_GLOBEX`).

```bash
curl -X POST localhost:8000/generate \
  -H "Authorization: Bearer demo_acme_local_key" -H "Idempotency-Key: my-first-call" \
  -H "Content-Type: application/json" \
  -d '{"input_tokens": 10000, "cached_input_tokens": 4000, "output_tokens": 1500, "reasoning_tokens": 2500}'
curl localhost:8000/usage -H "Authorization: Bearer demo_acme_local_key"
```

## Architecture

```
Client --POST /generate (Bearer key, Idempotency-Key)--> main.py (validate, auth)
          |
          v
   metering.record()  ---- tenant row locked (SELECT ... FOR UPDATE) for the whole decision ----
          |  key seen before?  same body -> return the stored response, no new event
          |                    other body -> 422
          |  payment failed?   -> 402
          |  used + requested > limit?  Free -> 402 (upgrade)   Pro -> 429 + Retry-After
          |  insert usage_event (UNIQUE tenant_id + key as backstop), store the response
          v
GET /usage  <-- rollup of this month's usage_events --> { used, limit, remaining, cost }

POST /billing/checkout --> Stripe Checkout (test mode, price from config/pricing.json)
Stripe --signed webhook--> POST /webhooks/stripe
          |  verify signature on the raw body (forged -> 400, nothing stored)
          |  INSERT stripe_events ON CONFLICT (event id) DO NOTHING   (replay -> "duplicate")
          v
worker.py (background job, polls every second)
          |  apply event: tenant plan/status mirrors Stripe; older events can't undo newer ones
          |  failure -> retry after 2, 4, 8, 16 s -> after 5 tries: status failed + alerts row + ERROR log
```

| Layer | Files |
|---|---|
| HTTP | `app/main.py` |
| Logic | `app/metering.py`, `app/pricing.py`, `app/stripe_sync.py` |
| Data | `app/repo.py`, `app/db.py`, `migrations/*.sql` |
| Background job | `app/worker.py` |

## Plans

| Plan | API calls / month | AI tokens / month | Fee |
|---|---|---|---|
| Free | 1,000 | 100,000 | $0 |
| Pro | 50,000 | 5,000,000 | $20.00 |

Both live in [config/pricing.json](config/pricing.json), which also syncs the `plans` table on every start.

## The rules, stated exactly

**Idempotency.** Every `POST /generate` needs an `Idempotency-Key`. The same key with the same body returns the first response again, byte for byte, with `Idempotent-Replayed: true`, and records nothing. The same key with a different body is a 422. Keys are scoped per tenant.

**Quota boundary.** Allowed when `used + requested <= limit`. On Free, call 1,000 succeeds and call 1,001 is refused. A refused request records nothing, so retrying it with the same key after upgrading works.

**402 or 429.** Free over its limit gets 402 Payment Required, because upgrading fixes it. Pro over its limit gets 429 Too Many Requests with `Retry-After` in seconds until the month resets, because nothing higher exists. A tenant whose payment failed (`past_due`) gets 402 on every billable request. Every refusal says which limit, how much is used, and when it resets.

**Token pricing.** Prices are integers in micro-dollars (1,000,000 = $1.00):

| Category | Price per 1M tokens |
|---|---|
| fresh input (`input_tokens - cached_input_tokens`) | $0.30 |
| cached input | $0.075 |
| output and reasoning | $2.50 |
| plus each API call | $0.0002 |

`cached_input_tokens` is part of `input_tokens`, the way providers report it, and reasoning is billed as output. Worked example: 10,000 input (4,000 cached), 1,500 output, 2,500 reasoning costs 1,800 + 300 + 10,000 = **12,100 micro-dollars** for the tokens. Summing all four counts and pricing them as input would give 5,400, less than half. The token quota counts `input + output + reasoning` = 14,000 (cached once, not twice).

**Rounding.** A single event is rounded half-up to a whole micro-dollar. The monthly figure on `/usage` is priced from the month's summed token counts and rounded once, so per-event rounding can't drift the total; `per_event_sum_micros` shows the per-event sum next to it.

## Stripe (test mode)

1. Put a test secret key (`sk_test_...`) in `.env` as `STRIPE_SECRET_KEY`. The app refuses to start with a live key.
2. Run `stripe listen --forward-to localhost:8000/webhooks/stripe` and put the `whsec_...` it prints in `.env` as `STRIPE_WEBHOOK_SECRET`.
3. `docker compose up -d` to reload the env, then `POST /billing/checkout` with a tenant's key and open the returned `checkout_url`.
4. Pay with test card `4242 4242 4242 4242`, any future date, any CVC. The webhook flips the tenant to Pro within a second or two.

The Pro price is created inline from `config/pricing.json` (`price_data`), so nothing needs setting up in the Stripe dashboard first.

## Limitations

- **`customer.subscription.created` is ignored.** The plan flips on `checkout.session.completed` (and later `customer.subscription.updated`/`deleted`), which is enough for Checkout. A subscription created outside Checkout, in the dashboard, would not change a plan until its first update.
- One paid plan, so "any active subscription" means Pro. A second paid tier would need a price-to-plan map.
- Month boundaries are calendar months in UTC, not per-customer billing cycles.
- The tenant row lock serialises metering per tenant. Fine at this scale; a very hot tenant would want a counter table or sharded keys.
- Refused requests are not stored under their key, unlike Stripe's API, which also caches errors. That is deliberate (see above) but differs from Stripe.
- The worker polls every second. `LISTEN/NOTIFY` would make it instant.
- No invoices, proration or overage billing (the brief's stretch goals).
