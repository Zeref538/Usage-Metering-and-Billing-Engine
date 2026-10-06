# TODO

## Agent Tasks

- [x] Phase 1: one-page design doc committed
- [x] Phase 2: migrations, pricing config, idempotent metering, quota 402/429
- [x] Phase 2 gate: same request twice = one event; boundary returns 429/402 (tests)
- [x] Phase 3: Checkout endpoint, verified + deduplicated webhooks, worker with retries + alert
- [x] Phase 3 gate (offline): signed test events flip a tenant Free to Pro; forged = 400; replay = once
- [x] Phase 4: /usage cost rollup with the token rules; pinned totals proven
- [ ] docker compose up + seed from a clean volume; capstone.yaml probes run
- [ ] README, EVIDENCE.md, BUILDLOG.md, .env.example
- [ ] Public repo pushed

## Zeref Tasks

## In Progress
