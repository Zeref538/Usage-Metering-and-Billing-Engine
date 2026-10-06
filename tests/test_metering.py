"""Exactly-once metering, the quota boundary, and tenant isolation, through the real HTTP API."""
import uuid
from concurrent.futures import ThreadPoolExecutor

BODY = {"input_tokens": 1_000, "cached_input_tokens": 200, "output_tokens": 300, "reasoning_tokens": 100}


def auth(key, idem=None):
    h = {"Authorization": f"Bearer {key}"}
    if idem:
        h["Idempotency-Key"] = idem
    return h


def events(con, tenant_id):
    return con.execute("SELECT COUNT(*) AS n FROM usage_events WHERE tenant_id = %s", (tenant_id,)).fetchone()["n"]


def fill(con, tenant_id, n, input_tokens=0):
    """n past usage events in one statement, to put a tenant next to its limit."""
    con.execute("""INSERT INTO usage_events (tenant_id, idempotency_key, request_hash, input_tokens, cost_micros)
                   SELECT %s, 'fill-' || g, 'fill', %s, 0 FROM generate_series(1, %s) g""",
                (tenant_id, input_tokens, n))


# --- exactly once ------------------------------------------------------------

def test_same_request_twice_records_one_event_and_mirrors_the_response(client, con, make_tenant):
    t, key = make_tenant()
    first = client.post("/generate", json=BODY, headers=auth(key, "retry-me"))
    second = client.post("/generate", json=BODY, headers=auth(key, "retry-me"))
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert events(con, t["id"]) == 1


def test_ten_simultaneous_retries_still_record_one_event(client, con, make_tenant):
    t, key = make_tenant()
    with ThreadPoolExecutor(10) as pool:
        replies = list(pool.map(lambda _: client.post("/generate", json=BODY, headers=auth(key, "burst")), range(10)))
    assert {r.status_code for r in replies} == {201}
    assert len({r.json()["event_id"] for r in replies}) == 1
    assert events(con, t["id"]) == 1


def test_same_key_with_a_different_body_is_refused(client, con, make_tenant):
    t, key = make_tenant()
    client.post("/generate", json=BODY, headers=auth(key, "k1"))
    r = client.post("/generate", json={**BODY, "output_tokens": 999}, headers=auth(key, "k1"))
    assert r.status_code == 422 and r.json()["error"] == "idempotency_key_reused"
    assert events(con, t["id"]) == 1


def test_different_keys_are_different_requests(client, con, make_tenant):
    t, key = make_tenant()
    client.post("/generate", json=BODY, headers=auth(key, "a"))
    client.post("/generate", json=BODY, headers=auth(key, "b"))
    assert events(con, t["id"]) == 2


# --- validation at the boundary ------------------------------------------------

def test_bad_input_is_a_clean_4xx(client, make_tenant):
    _, key = make_tenant()
    assert client.post("/generate", json=BODY, headers=auth(key)).status_code == 400          # no key
    assert client.post("/generate", json=BODY, headers=auth(key, "has space")).status_code == 400
    assert client.post("/generate", json=BODY).status_code == 401                             # no auth
    assert client.post("/generate", json=BODY, headers=auth("wrong", "x")).status_code == 401
    for bad in ({**BODY, "cached_input_tokens": 2_000},       # cached > input
                {**BODY, "input_tokens": 1.5},                # not a whole number
                {**BODY, "input_tokens": "100"},              # a string
                {**BODY, "output_tokens": -1},
                {**BODY, "surprise": 1},                      # unknown field
                {"input_tokens": 5}):                         # missing output_tokens
        r = client.post("/generate", json=bad, headers=auth(key, str(uuid.uuid4())))
        assert r.status_code == 422, bad
        assert r.json()["error"] == "invalid_request"


# --- the quota boundary ----------------------------------------------------------

def test_free_plan_allows_call_1000_and_refuses_1001_with_402(client, con, make_tenant):
    t, key = make_tenant("free")
    fill(con, t["id"], 999)
    at_limit = client.post("/generate", json=BODY, headers=auth(key, "call-1000"))
    assert at_limit.status_code == 201
    assert at_limit.json()["usage_after"]["api_calls"] == {"used": 1000, "limit": 1000}
    over = client.post("/generate", json=BODY, headers=auth(key, "call-1001"))
    assert over.status_code == 402
    body = over.json()
    assert body["error"] == "quota_exceeded" and body["upgrade"] == "POST /billing/checkout"
    assert body["exceeded"][0] == {"metric": "api_calls", "used": 1000, "limit": 1000, "requested": 1}
    assert "1,000 of 1,000 api calls" in body["message"]
    assert events(con, t["id"]) == 1000                       # the refused call recorded nothing


def test_pro_plan_over_its_limit_gets_429_with_retry_after(client, con, make_tenant):
    t, key = make_tenant("pro")
    fill(con, t["id"], 50_000)
    r = client.post("/generate", json=BODY, headers=auth(key, "pro-over"))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert r.json()["exceeded"][0]["metric"] == "api_calls"


def test_token_quota_boundary_is_exact(client, con, make_tenant):
    t, key = make_tenant("free")
    fill(con, t["id"], 1, input_tokens=99_000)
    exact = {"input_tokens": 1_000, "output_tokens": 0}       # lands on exactly 100,000
    assert client.post("/generate", json=exact, headers=auth(key, "to-100k")).status_code == 201
    one_more = {"input_tokens": 1, "output_tokens": 0}
    r = client.post("/generate", json=one_more, headers=auth(key, "past-100k"))
    assert r.status_code == 402
    assert r.json()["exceeded"][0] == {"metric": "ai_tokens", "used": 100_000, "limit": 100_000, "requested": 1}


def test_a_failed_payment_blocks_with_402(client, con, make_tenant):
    t, key = make_tenant("pro")
    repo_update = "UPDATE tenants SET billing_status = 'past_due' WHERE id = %s"
    con.execute(repo_update, (t["id"],))
    r = client.post("/generate", json=BODY, headers=auth(key, "unpaid"))
    assert r.status_code == 402 and r.json()["error"] == "payment_required"


def test_a_refused_request_succeeds_with_the_same_key_after_upgrading(client, con, make_tenant):
    t, key = make_tenant("free")
    fill(con, t["id"], 1000)
    assert client.post("/generate", json=BODY, headers=auth(key, "after-upgrade")).status_code == 402
    con.execute("UPDATE tenants SET plan_code = 'pro' WHERE id = %s", (t["id"],))
    assert client.post("/generate", json=BODY, headers=auth(key, "after-upgrade")).status_code == 201


# --- tenant isolation --------------------------------------------------------------

def test_tenants_never_see_each_others_usage(client, make_tenant):
    _, key_a = make_tenant()
    _, key_b = make_tenant()
    client.post("/generate", json=BODY, headers=auth(key_a, "shared-key-name"))
    # the same idempotency key in another tenant is a different request, not a replay
    r = client.post("/generate", json=BODY, headers=auth(key_b, "shared-key-name"))
    assert r.status_code == 201 and "Idempotent-Replayed" not in r.headers
    client.post("/generate", json=BODY, headers=auth(key_a, "a-only"))
    assert client.get("/usage", headers=auth(key_a)).json()["api_calls"]["used"] == 2
    assert client.get("/usage", headers=auth(key_b)).json()["api_calls"]["used"] == 1
    assert len(client.get("/usage/events", headers=auth(key_b)).json()["events"]) == 1


# --- the rollup --------------------------------------------------------------------

def test_usage_cost_matches_the_pinned_prices(client, make_tenant):
    _, key = make_tenant()
    worked = {"input_tokens": 10_000, "cached_input_tokens": 4_000, "output_tokens": 1_500, "reasoning_tokens": 2_500}
    client.post("/generate", json=worked, headers=auth(key, "w1"))
    client.post("/generate", json=worked, headers=auth(key, "w2"))
    u = client.get("/usage", headers=auth(key)).json()
    assert u["ai_tokens"]["used"] == 28_000
    assert u["cost"]["api_calls_micros"] == 400                # 2 calls x 200
    assert u["cost"]["ai_tokens_micros"] == 24_200             # 2 x 12,100
    assert u["cost"]["usage_micros"] == 24_600 and u["cost"]["usage_usd"] == "0.024600"
