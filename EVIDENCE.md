# Evidence

One proof per requirement in Section 6 of the brief. Everything here was run on 6 Oct 2026 against the Docker stack (`docker compose up`), Postgres 16. Full outputs: [docs/test-run.txt](docs/test-run.txt) (30 tests, run inside the api container), [docs/probes-run.txt](docs/probes-run.txt) (the 5 acceptance probes over HTTP), [docs/curl-retry.txt](docs/curl-retry.txt), [docs/schema.txt](docs/schema.txt).

## Metering

- [x] **A billable action creates exactly one usage event, even under retries.**
  `test_ten_simultaneous_retries_still_record_one_event PASSED`: ten threads send the same request with the same key at once; all ten get 201 with the same `event_id`, and the table holds one row.

- [x] **Proof that double-counting cannot happen: the same request sent twice.**
  From [docs/curl-retry.txt](docs/curl-retry.txt), two identical curls with `Idempotency-Key: curl-demo-1`:
  ```
  HTTP/1.1 201 Created
  {"event_id":3008,"recorded":{"api_calls":1,"input_tokens":2000,...},"cost":{"micros":2050,"usd":"0.002050"},...}
  HTTP/1.1 201 Created
  idempotent-replayed: true
  {"event_id":3008,"recorded":{"api_calls":1,"input_tokens":2000,...},"cost":{"micros":2050,"usd":"0.002050"},...}

  $ psql -tAc "SELECT count(*) FROM usage_events WHERE idempotency_key='curl-demo-1'"
  1
  ```
  The two bodies are identical byte for byte (`test_same_request_twice_records_one_event_and_mirrors_the_response` asserts `second.content == first.content`).

## Quotas

- [x] **Usage is checked against the tenant's plan; requests over the limit are rejected.**
  Probe 2, over real HTTP on a fresh Free tenant:
  ```
  PASS  P2 call 1,000 (exactly at the limit) is allowed
  PASS  P2 call 1,001 is 402 with a clear message
  ```
  The token quota is exact too: `test_token_quota_boundary_is_exact PASSED` (99,000 used + 1,000 = exactly 100,000 allowed; one more token refused).

- [x] **Responses carry the correct status codes (429 / 402) and a message explaining why.**
  ```
  402 message: Free plan limit reached: 1,000 of 1,000 api calls used this month. Upgrade to Pro
  (POST /billing/checkout) or wait for the reset at 2026-11-01T00:00:00Z.
  ```
  `test_pro_plan_over_its_limit_gets_429_with_retry_after PASSED` (Pro over its limit: 429 and a positive `Retry-After`), `test_a_failed_payment_blocks_with_402 PASSED`.

## Cost calculation

- [x] **Monthly usage rolls up into a cost figure per tenant.**
  `GET /usage` after probe 5: `"cost": {"api_calls_micros": 200, "ai_tokens_micros": 12100, "usage_micros": 12300, "usage_usd": "0.012300"}`. `test_usage_cost_matches_the_pinned_prices PASSED` checks two requests roll up to 24,600 micros.

- [x] **AI token pricing handles cached input tokens, reasoning tokens, and output pricing correctly.**
  ```
  PASS  P5 one request: cost 12,300 micros ($0.012300)
  PASS  P5 GET /usage matches: 200 (call) + 12,100 (tokens) = 12,300
  PASS  P5 token quota counts 14,000 (cached counted once, reasoning included)
  ```
  Plus `test_cached_tokens_are_cheaper_than_fresh_ones`, `test_reasoning_tokens_cost_the_same_as_output`, `test_naive_sum_of_categories_gives_the_wrong_answer` (the naive sum gives 5,400 instead of 12,100), all PASSED.

- [x] **Pricing constants are pinned in config, with proof of correct totals.**
  [config/pricing.json](config/pricing.json): input 300,000, cached input 75,000, output 2,500,000 micro-dollars per million tokens, 200 per call. `test_the_pinned_constants_are_the_ones_these_tests_assume PASSED` fails if anyone edits them without updating the expected totals. `test_a_float_price_in_config_is_refused PASSED`: a price of `0.3` stops the app at load.

  Hand calculation for 10,000 input (4,000 cached), 1,500 output, 2,500 reasoning:

  | Part | Tokens | Price / 1M | Micro-dollars |
  |---|---|---|---|
  | fresh input | 6,000 | 300,000 | 1,800 |
  | cached input | 4,000 | 75,000 | 300 |
  | output + reasoning | 4,000 | 2,500,000 | 10,000 |
  | one API call | | | 200 |
  | **total** | | | **12,300** |

## Stripe integration

- [ ] **Subscription checkout works end-to-end in Stripe test mode.** *Not run yet.* `POST /billing/checkout` builds the session (code in `app/main.py`), but no real test-mode Checkout has been paid through a browser, because that needs a Stripe account's test key. Probe 3 proves our side with an event signed the way Stripe signs:
  ```
  P3 note: event signed by this script with STRIPE_WEBHOOK_SECRET (not by Stripe)
  PASS  P3 signed webhook accepted
  PASS  P3 worker flipped Free to Pro; /usage shows the Pro limits
  PASS  P3 the call refused at 1,001 now succeeds
  ```
  Worker log: `INFO worker evt_probe_69f200ee2edf checkout.session.completed -> done: subscription active: tenant 1 is now pro`

- [x] **Webhooks verify signatures, ignore duplicate events, and update tenant plan/status.**
  ```
  PASS  P4 forged signature is 400
  PASS  P4 the forgery changed nothing
  PASS  P4 replaying a real event is marked duplicate (processed once)
  ```
  `test_a_forged_webhook_is_400_and_changes_nothing` (wrong secret, tampered body, a capture over an hour old, garbage, no header: all 400, nothing stored), `test_a_replayed_event_is_processed_once` (three deliveries, `attempts = 1`), `test_an_old_event_arriving_late_cannot_undo_a_newer_one`, `test_cancel_drops_back_to_free_and_a_failed_payment_marks_past_due`, all PASSED.

## Data model, tests & documentation

- [x] **Database includes tenants, plans, subscriptions, and usage events; customer data isolated per tenant.**
  From [docs/schema.txt](docs/schema.txt): tables `alerts, plans, schema_migrations, stripe_events, subscriptions, tenants, usage_events`; indexes include `usage_events (tenant_id, idempotency_key) UNIQUE` and `usage_events (tenant_id, created_at)`. Every query in `app/repo.py` filters on the tenant id that comes from the API key. `test_tenants_never_see_each_others_usage PASSED` (and the same idempotency key in two tenants is two requests).

- [x] **README + architecture diagram + setup instructions; the required files present.**
  `README.md` (diagram, run + seed, limitations), `capstone.yaml`, `EVIDENCE.md`, `BUILDLOG.md`, `.env.example`, `DESIGN.md`, `LICENSE`.

## Shared requirements

| # | Requirement | Where |
|---|---|---|
| 1 | Layered architecture | `main.py` (HTTP) / `metering.py`, `pricing.py`, `stripe_sync.py` (logic) / `repo.py` (data) |
| 2 | Bad input is a clean 4xx | `test_bad_input_is_a_clean_4xx PASSED`: missing key 400, no auth 401, cached > input, float, string, negative, unknown field, missing field all 422 |
| 3 | Background job with retries and a failure alert | `app/worker.py`; `test_a_failing_event_is_retried_then_alerted PASSED` (5 attempts, then `failed` + an `alerts` row + an ERROR log line), `test_retry_waits_with_backoff PASSED` |
| 4 | Schema as migrations, indexes, isolated tenants | `migrations/001_init.sql`, `002_response_as_json.sql`, applied once each under an advisory lock |
| 5 | Idempotency | metering keys + Stripe event ids (above) |
| 6 | Secrets clean | `.env` git-ignored and docker-ignored; API keys stored as sha256; live Stripe keys refused at start |
| 7 | Cost tracked, if AI is used | No AI is called: token counts are simulated, as the brief allows |
