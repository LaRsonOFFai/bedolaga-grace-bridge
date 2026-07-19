BEGIN;

CREATE SCHEMA IF NOT EXISTS grace_bridge;

CREATE TABLE IF NOT EXISTS grace_bridge.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum text NOT NULL
);

CREATE TABLE IF NOT EXISTS grace_bridge.access_state (
    subscription_id bigint PRIMARY KEY,
    user_id bigint NOT NULL,
    remnawave_uuid uuid NOT NULL,
    state text NOT NULL DEFAULT 'inactive',
    generation bigint NOT NULL DEFAULT 0,
    paid_cycle_marker text,
    started_at timestamptz,
    expires_at timestamptz,
    traffic_limit_bytes bigint,
    grace_squad_uuid uuid,
    previous_panel_state jsonb,
    last_verified_panel_state jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT grace_bridge_state_valid CHECK (
        state IN (
            'inactive', 'pending_activate', 'active', 'pending_deactivate',
            'exhausted', 'suppressed', 'failed'
        )
    ),
    CONSTRAINT grace_bridge_generation_nonnegative CHECK (generation >= 0),
    CONSTRAINT grace_bridge_traffic_positive CHECK (
        traffic_limit_bytes IS NULL OR traffic_limit_bytes > 0
    )
);

CREATE TABLE IF NOT EXISTS grace_bridge.commands (
    id uuid PRIMARY KEY,
    subscription_id bigint NOT NULL,
    user_id bigint NOT NULL,
    generation bigint NOT NULL,
    action text NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    worker_id text,
    last_error text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT grace_bridge_command_action_valid CHECK (action IN ('activate', 'deactivate', 'reconcile')),
    CONSTRAINT grace_bridge_command_state_valid CHECK (
        state IN ('pending', 'processing', 'done', 'stale', 'failed', 'cancelled')
    ),
    CONSTRAINT grace_bridge_command_attempts_nonnegative CHECK (attempts >= 0),
    CONSTRAINT grace_bridge_command_unique UNIQUE (subscription_id, generation, action)
);

CREATE TABLE IF NOT EXISTS grace_bridge.events (
    id bigserial PRIMARY KEY,
    subscription_id bigint,
    user_id bigint,
    generation bigint,
    event_type text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS grace_bridge_access_user_idx
    ON grace_bridge.access_state (user_id);
CREATE INDEX IF NOT EXISTS grace_bridge_access_state_idx
    ON grace_bridge.access_state (state, subscription_id);
CREATE INDEX IF NOT EXISTS grace_bridge_commands_ready_idx
    ON grace_bridge.commands (state, next_attempt_at, created_at)
    WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS grace_bridge_commands_processing_lease_idx
    ON grace_bridge.commands (claimed_at, created_at)
    WHERE state = 'processing';
CREATE INDEX IF NOT EXISTS grace_bridge_events_subscription_idx
    ON grace_bridge.events (subscription_id, created_at DESC);

COMMIT;
