# Build log

I built this with an AI coding assistant. It wrote most of the first drafts; I set the rules, read every file, and the tests and probes below decided what stayed. This log records where it was wrong.

## Where it helped

- Drafting the schema, the pricing module and the webhook handler from the brief and DESIGN.md.
- Writing tests that sign webhooks exactly the way Stripe does, so the real `stripe` library verifies them without a Stripe account.
- A probe script that runs the brief's five acceptance probes over real HTTP.

## Where it was wrong, and what changed

1. **Money came back as `Decimal`.** The first test run failed 9 of 21: `Object of type Decimal is not JSON serializable`. Postgres returns `SUM()` of a `bigint` column as `numeric`, and the driver turns that into Python `Decimal`. Every sum in `repo.month_usage` is now cast back with `::bigint`, so money stays a plain integer.

2. **"Byte for byte" replay was not true.** The design said a retried key returns the first response byte for byte. A curl transcript showed the same data with the keys in a different order, because `jsonb` stores keys in its own order. Migration `002_response_as_json.sql` changes the column to `json`, which keeps the text, and the test now compares raw bytes. With the migration removed, that test fails.

3. **A test that proved nothing.** One line in `test_retry_waits_with_backoff` was `assert worker.run_once(con) is False or True`, which can never fail. Removed.

4. **Checking the tests can fail.** All 30 passed on the first run, so I broke the code on purpose twice. Removing the stale-event check failed `test_an_old_event_arriving_late_cannot_undo_a_newer_one`. Removing the tenant lock and the key lookup (leaving only the unique constraint) failed 3 metering tests: retries got a 500 instead of the stored answer.

## Decisions I made

- **402 vs 429.** Free over quota is 402 (paying fixes it), Pro over quota is 429 with `Retry-After` (only time fixes it). A failed payment is 402 on everything.
- **Refusals are not stored under their key.** A tenant refused at the limit, who then upgrades, can retry with the same key and succeed. Stripe's API caches errors too; I chose not to, and the README says so.
- **The Pro price is created inline** from `config/pricing.json` with `price_data`, so the price lives in one pinned file instead of also in the Stripe dashboard.
- **The webhook endpoint only verifies and stores.** The worker does the work, so a slow database never makes Stripe time out and retry.
