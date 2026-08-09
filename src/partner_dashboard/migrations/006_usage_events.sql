-- Product-usage events, pushed by the app itself.
--
-- The Partner API carries zero usage data: it knows a shop installed and that
-- it pays, and nothing about whether anyone ever built an offer. Activation
-- therefore has to be reported by the app. See docs/usage-events-integration.md.
--
-- The primary key is (shop_gid, event_id), not event_id alone, on purpose.
-- Ingestion is ON CONFLICT DO NOTHING so a retried POST is free and a stored
-- event can never be rewritten. Scoping the key to the shop means a caller
-- cannot suppress another shop's future event by pre-claiming its id.
create table if not exists usage_events (
    shop_gid text not null,
    event_id text not null,
    event_type text not null,
    occurred_at timestamptz not null,
    properties jsonb not null default '{}'::jsonb,
    received_at timestamptz not null default now(),
    primary key (shop_gid, event_id)
);

-- Both read patterns: "did this shop ever do X, and when first" (activation
-- cohorts) and "which shops did X recently" (the at-risk list).
create index if not exists usage_events_shop_type_time_idx
    on usage_events (shop_gid, event_type, occurred_at);
create index if not exists usage_events_type_time_idx
    on usage_events (event_type, occurred_at);
-- Supports the per-shop flood cap, which counts a shop's recent arrivals.
create index if not exists usage_events_shop_received_idx
    on usage_events (shop_gid, received_at);
