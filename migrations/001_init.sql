-- Money is integers everywhere: micro-dollars (1,000,000 = $1.00) or cents.

CREATE TABLE plans (
    code              text PRIMARY KEY,
    name              text NOT NULL,
    api_calls_limit   bigint NOT NULL CHECK (api_calls_limit >= 0),
    ai_tokens_limit   bigint NOT NULL CHECK (ai_tokens_limit >= 0),
    monthly_fee_cents integer NOT NULL CHECK (monthly_fee_cents >= 0)
);

CREATE TABLE tenants (
    id                 bigserial PRIMARY KEY,
    name               text NOT NULL,
    api_key_hash       text NOT NULL UNIQUE,          -- sha256 of the key, never the key
    plan_code          text NOT NULL DEFAULT 'free' REFERENCES plans (code),
    billing_status     text NOT NULL DEFAULT 'ok' CHECK (billing_status IN ('ok', 'past_due')),
    stripe_customer_id text UNIQUE,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE subscriptions (
    id                     bigserial PRIMARY KEY,
    tenant_id              bigint NOT NULL REFERENCES tenants (id),
    stripe_subscription_id text NOT NULL UNIQUE,
    status                 text NOT NULL,
    plan_code              text NOT NULL REFERENCES plans (code),
    last_event_created     bigint NOT NULL,           -- Stripe's event.created; older events are ignored
    updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX subscriptions_tenant ON subscriptions (tenant_id);

CREATE TABLE usage_events (
    id                  bigserial PRIMARY KEY,
    tenant_id           bigint NOT NULL REFERENCES tenants (id),
    idempotency_key     text NOT NULL,
    request_hash        text NOT NULL,
    api_calls           integer NOT NULL DEFAULT 1 CHECK (api_calls >= 0),
    input_tokens        bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cached_input_tokens bigint NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    output_tokens       bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens    bigint NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cost_micros         bigint NOT NULL CHECK (cost_micros >= 0),
    response            jsonb,                        -- replayed as-is for a retried key
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (cached_input_tokens <= input_tokens),      -- cached is a part of input, not extra
    UNIQUE (tenant_id, idempotency_key)               -- the backstop against double-counting
);
CREATE INDEX usage_events_tenant_time ON usage_events (tenant_id, created_at);

-- Every verified Stripe webhook. Also the background job queue.
CREATE TABLE stripe_events (
    id              text PRIMARY KEY,                 -- Stripe's evt_ id: a replay cannot insert twice
    type            text NOT NULL,
    created         bigint NOT NULL,
    payload         jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'done', 'ignored', 'failed')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    note            text,
    received_at     timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz
);
CREATE INDEX stripe_events_due ON stripe_events (next_attempt_at) WHERE status = 'pending';

CREATE TABLE alerts (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL,
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
