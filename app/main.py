"""HTTP layer: routes, auth, validation and status codes. The decisions live in metering.py."""
import hashlib
import json
import re

import stripe
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app import config, db, metering, repo
from app.pricing import PRICING, Tokens

app = FastAPI(title="Usage Metering & Billing Engine", version="1.0.0")
KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,255}$")  # printable ASCII, no spaces
MAX_TOKENS = 10_000_000


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str):
        self.status, self.error, self.message = status, error, message


@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError):
    return JSONResponse({"error": exc.error, "message": exc.message}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()]
    return JSONResponse({"error": "invalid_request", "message": "The request is invalid.", "details": details},
                        status_code=422)


def get_con():
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


def current_tenant(authorization: str | None = Header(None), con=Depends(get_con)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "unauthorized", "Send your API key as: Authorization: Bearer <key>")
    tenant = repo.tenant_by_key(con, authorization.removeprefix("Bearer ").strip())
    if tenant is None:
        raise ApiError(401, "unauthorized", "Unknown API key.")
    return tenant


class GenerateIn(BaseModel):
    """Simulated AI usage. StrictInt refuses 1.5 and "100": counts are whole numbers."""
    model_config = ConfigDict(extra="forbid")
    input_tokens: StrictInt = Field(ge=0, le=MAX_TOKENS, description="All input tokens, cached ones included")
    cached_input_tokens: StrictInt = Field(0, ge=0, le=MAX_TOKENS, description="The cached part of input_tokens")
    output_tokens: StrictInt = Field(ge=0, le=MAX_TOKENS)
    reasoning_tokens: StrictInt = Field(0, ge=0, le=MAX_TOKENS, description="Billed at the output price")

    @model_validator(mode="after")
    def cached_is_part_of_input(self):
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens is part of input_tokens, so it cannot be larger")
        return self


@app.get("/health")
def health(con=Depends(get_con)):
    con.execute("SELECT 1")
    return {"status": "ok", "db": "ok"}


@app.post("/generate", status_code=201, summary="The dummy billable action: meters 1 API call + its tokens")
def generate(body: GenerateIn, tenant=Depends(current_tenant), con=Depends(get_con),
             idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    if idempotency_key is None:
        raise ApiError(400, "idempotency_key_required",
                       "Send an Idempotency-Key header (any unique string, e.g. a UUID). "
                       "Retrying with the same key can never be charged twice.")
    if not KEY_PATTERN.match(idempotency_key):
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key must be 1 to 255 printable characters.")
    request_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    tokens = Tokens(input=body.input_tokens, cached_input=body.cached_input_tokens,
                    output=body.output_tokens, reasoning=body.reasoning_tokens)
    out = metering.record(con, tenant["id"], idempotency_key, request_hash, tokens)
    return JSONResponse(out.body, status_code=out.status, headers=out.headers)


@app.get("/usage", summary="This month's usage, limits and cost for the caller's tenant")
def usage(tenant=Depends(current_tenant), con=Depends(get_con)):
    return metering.usage_summary(con, tenant)


@app.get("/usage/events", summary="The caller's own usage events, newest first")
def usage_events(tenant=Depends(current_tenant), con=Depends(get_con), limit: int = Query(50, ge=1, le=500)):
    return {"events": repo.list_events(con, tenant["id"], limit)}


@app.post("/billing/checkout", summary="Start a Stripe Checkout (test mode) for the Pro plan")
def checkout(tenant=Depends(current_tenant)):
    if not config.STRIPE_SECRET_KEY:
        raise ApiError(503, "stripe_not_configured", "Set STRIPE_SECRET_KEY (a sk_test_ key) in .env.")
    if tenant["plan_code"] == "pro" and tenant["billing_status"] == "ok":
        raise ApiError(409, "already_pro", "This tenant is already on Pro.")
    pro = PRICING.plans["pro"]
    params = {
        "mode": "subscription",
        # The price comes from config/pricing.json, so there is nothing to create in the dashboard first.
        "line_items": [{"quantity": 1, "price_data": {
            "currency": PRICING.currency, "unit_amount": pro.monthly_fee_cents,
            "recurring": {"interval": "month"}, "product_data": {"name": f"{pro.name} plan"}}}],
        "client_reference_id": str(tenant["id"]),
        "metadata": {"tenant_id": str(tenant["id"])},
        "subscription_data": {"metadata": {"tenant_id": str(tenant["id"])}},
        "success_url": f"{config.PUBLIC_BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{config.PUBLIC_BASE_URL}/billing/cancel",
    }
    if tenant["stripe_customer_id"]:
        params["customer"] = tenant["stripe_customer_id"]
    try:
        session = stripe.checkout.Session.create(api_key=config.STRIPE_SECRET_KEY, **params)
    except stripe.StripeError as exc:
        raise ApiError(502, "stripe_error", f"Stripe refused the checkout: {exc.user_message or type(exc).__name__}")
    return {"checkout_url": session.url, "session_id": session.id}


@app.get("/billing/success", include_in_schema=False)
def billing_success(session_id: str = ""):
    return {"message": "Payment received by Stripe. Your plan switches to Pro when the webhook arrives "
                       "(usually within seconds). Check GET /usage.", "session_id": session_id}


@app.get("/billing/cancel", include_in_schema=False)
def billing_cancel():
    return {"message": "Checkout cancelled. Nothing was charged and your plan is unchanged."}


@app.post("/webhooks/stripe", summary="Stripe events: signature-verified, deduplicated, applied by the worker")
async def stripe_webhook(request: Request, con=Depends(get_con),
                         stripe_signature: str | None = Header(None, alias="Stripe-Signature")):
    if not config.STRIPE_WEBHOOK_SECRET:
        raise ApiError(503, "webhook_not_configured", "Set STRIPE_WEBHOOK_SECRET (whsec_...) in .env.")
    payload = await request.body()  # the raw bytes: re-serialised JSON would break the signature
    try:
        stripe.Webhook.construct_event(payload, stripe_signature, config.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        raise ApiError(400, "invalid_signature", "Stripe-Signature does not match this payload. Nothing was changed.")
    event = json.loads(payload)
    is_new = repo.store_stripe_event(con, event)
    return {"received": True, "event_id": event["id"], "duplicate": not is_new}
