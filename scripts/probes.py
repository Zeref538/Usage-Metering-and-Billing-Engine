"""The brief's acceptance probes, run against the live stack over real HTTP.

    docker compose up -d --build --wait
    docker compose exec api python -m app.seed --reset
    python scripts/probes.py

Probe 3 needs a webhook signed by Stripe. Without `stripe listen` running, this script signs
the event itself with STRIPE_WEBHOOK_SECRET from .env, which proves the pipeline but not
Stripe's side; it says which one it did. The real Stripe run is in EVIDENCE.md.
"""
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()
BASE = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
ACME = os.environ.get("DEMO_KEY_ACME", "demo_acme_local_key")
GLOBEX = os.environ.get("DEMO_KEY_GLOBEX", "demo_globex_local_key")
SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
http = httpx.Client(base_url=BASE, timeout=10)
failed = 0


def check(name: str, ok: bool, detail="") -> None:
    global failed
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  {detail}"))
    failed += not ok


def gen(key: str, idem: str, body: dict | None = None) -> httpx.Response:
    return http.post("/generate", json=body or {"input_tokens": 10, "output_tokens": 5},
                     headers={"Authorization": f"Bearer {key}", "Idempotency-Key": idem})


def usage(key: str) -> dict:
    return http.get("/usage", headers={"Authorization": f"Bearer {key}"}).json()


def signed_post(event: dict, secret: str) -> httpx.Response:
    payload = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return http.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": f"t={t},v1={sig}"})


print(f"probing {BASE}\n")
if usage(ACME)["api_calls"]["used"] or usage(GLOBEX)["api_calls"]["used"]:
    sys.exit("demo tenants already have usage: run  docker compose exec api python -m app.seed --reset  first")

# PROBE 1: same request twice, one key
a = gen(ACME, "probe-1")
b = gen(ACME, "probe-1")
check("P1 same request twice: both 201", a.status_code == b.status_code == 201, f"{a.status_code} {b.status_code}")
check("P1 second response mirrors the first", a.json() == b.json() and b.headers.get("Idempotent-Replayed") == "true")
check("P1 exactly one usage event", usage(ACME)["api_calls"]["used"] == 1)

# PROBE 2: drive Acme (Free, 1,000 calls) to its exact quota
print("      sending 998 more calls to reach exactly 999 used...")
for i in range(998):
    gen(ACME, f"probe-2-{i}")
at = gen(ACME, "probe-2-call-1000")
check("P2 call 1,000 (exactly at the limit) is allowed", at.status_code == 201, at.text)
over = gen(ACME, "probe-2-call-1001")
check("P2 call 1,001 is 402 with a clear message", over.status_code == 402 and "1,000 of 1,000" in over.json()["message"],
      over.text)
print(f"      402 message: {over.json()['message']}")

# PROBE 3: checkout flips Free to Pro via the webhook
tenant_id = usage(ACME)["tenant"]["id"]
event = {"id": f"evt_probe_{uuid.uuid4().hex[:12]}", "object": "event", "type": "checkout.session.completed",
         "created": int(time.time()), "data": {"object": {
             "id": "cs_probe", "object": "checkout.session", "mode": "subscription",
             "client_reference_id": str(tenant_id), "customer": "cus_probe", "subscription": "sub_probe",
             "payment_status": "paid", "metadata": {"tenant_id": str(tenant_id)}}}}
print("      P3 note: event signed by this script with STRIPE_WEBHOOK_SECRET (not by Stripe)")
r = signed_post(event, SECRET)
check("P3 signed webhook accepted", r.status_code == 200 and r.json()["duplicate"] is False, r.text)
for _ in range(20):                               # the worker polls once a second
    if usage(ACME)["plan"]["code"] == "pro":
        break
    time.sleep(0.5)
u = usage(ACME)
check("P3 worker flipped Free to Pro; /usage shows the Pro limits",
      u["plan"]["code"] == "pro" and u["api_calls"]["limit"] == 50_000, u["plan"])
check("P3 the call refused at 1,001 now succeeds", gen(ACME, "probe-2-call-1001").status_code == 201)

# PROBE 4: forged webhook, then a replay
forged = dict(event, id=f"evt_forged_{uuid.uuid4().hex[:8]}")
f = signed_post(forged, "whsec_not_the_real_secret")
check("P4 forged signature is 400", f.status_code == 400, f.text)
check("P4 the forgery changed nothing", usage(ACME)["plan"]["code"] == "pro")
again = signed_post(event, SECRET)
check("P4 replaying a real event is marked duplicate (processed once)",
      again.status_code == 200 and again.json()["duplicate"] is True, again.text)

# PROBE 5: the pinned pricing rules
worked = {"input_tokens": 10_000, "cached_input_tokens": 4_000, "output_tokens": 1_500, "reasoning_tokens": 2_500}
w = gen(GLOBEX, "probe-5", worked)
check("P5 one request: cost 12,300 micros ($0.012300)",
      w.status_code == 201 and w.json()["cost"] == {"micros": 12_300, "usd": "0.012300"}, w.text)
g = usage(GLOBEX)
check("P5 GET /usage matches: 200 (call) + 12,100 (tokens) = 12,300",
      (g["cost"]["api_calls_micros"], g["cost"]["ai_tokens_micros"], g["cost"]["usage_micros"]) == (200, 12_100, 12_300),
      g["cost"])
check("P5 token quota counts 14,000 (cached counted once, reasoning included)", g["ai_tokens"]["used"] == 14_000)

print(f"\n{'all probes passed' if not failed else f'{failed} check(s) failed'}")
sys.exit(1 if failed else 0)
