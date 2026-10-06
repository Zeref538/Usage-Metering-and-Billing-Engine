# TODO

## Agent Tasks

- [x] Phase 1: one-page design doc committed
- [ ] Phase 2: migrations, pricing config, idempotent metering, quota 402/429
- [ ] Phase 2 gate: same request twice = one event; boundary returns 429/402 (tests)
- [ ] Phase 3: Checkout endpoint, verified + deduplicated webhooks, worker with retries + alert
- [ ] Phase 3 gate (offline): signed test events flip a tenant Free to Pro; forged = 400; replay = once
- [ ] Phase 4: /usage cost rollup with the token rules; pinned totals proven
- [ ] docker compose up + seed from a clean volume; capstone.yaml probes run
- [ ] README, EVIDENCE.md, BUILDLOG.md, .env.example
- [ ] Public repo pushed

## Zeref Tasks

## In Progress
