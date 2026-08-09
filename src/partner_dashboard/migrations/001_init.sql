create table if not exists raw_app_events (
    id text primary key,
    type text,
    occurred_at timestamptz,
    app_installation_id text,
    charge_gid text,
    payload jsonb,
    ingested_at timestamptz default now(),
    coalesce_charge text generated always as (coalesce(charge_gid, '')) stored,
    unique (app_installation_id, type, occurred_at, coalesce_charge)
);

create table if not exists charges (
    gid text primary key,
    amount numeric(12,2),
    currency_code text,
    subscription_id text,
    plan_interval text,
    plan_amount numeric(12,2),
    flex_billing boolean default false
);

create table if not exists app_events (
    id bigserial primary key,
    platform_event_id text unique,
    type text,
    occurred_at timestamptz,
    net_change numeric(12,2),
    plan_amount numeric(12,2),
    plan_interval text,
    plan_currency_code text,
    previous_subscription_id text,
    app_installation_id text,
    organization_id text,
    deleted_at timestamptz
);

create table if not exists subscriptions (
    id text primary key,
    app_installation_id text,
    monthly_amount numeric(12,2),
    billing_type text,
    trial_started_at timestamptz,
    converted_at timestamptz,
    churned_at timestamptz,
    paused boolean default false
);

create table if not exists shops (
    app_installation_id text primary key,
    shop_domain text,
    shop_name text,
    owner_name text,
    email text,
    country text,
    industry text,
    install_state text,
    installed_at timestamptz,
    uninstalled_at timestamptz,
    organization_id text,
    updated_at timestamptz default now()
);

create table if not exists tracking_events (
    id bigserial primary key,
    anonymous_id text,
    session_id text,
    event_name text,
    utm_source text,
    utm_medium text,
    utm_campaign text,
    page_location text,
    page_referrer text,
    occurred_at timestamptz
);

create table if not exists sync_state (
    source text primary key,
    cursor text,
    last_synced_at timestamptz
);
