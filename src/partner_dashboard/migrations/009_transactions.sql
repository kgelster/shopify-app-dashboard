-- Money that actually moved, from the Partner API's root `transactions`
-- connection.
--
-- Everything else in this database is derived from the app *events* feed, which
-- describes subscription state: what a shop agreed to pay. That makes every
-- money figure a forward-looking projection. It cannot answer "how much did we
-- collect last month", and it is blind to refunds, downgrades and credits,
-- which arrive only as APP_SALE_ADJUSTMENT / APP_SALE_CREDIT transactions and
-- never as an event.
--
-- The Partner API id is a real, stable gid here (unlike AppEvent, which has
-- none and forces a composed dedupe key), so it is the primary key directly.
--
-- Field semantics, verified live against the 2026-07 API on 2026-08-08:
--   gross_amount  what the merchant was billed.
--   shopify_fee   Shopify's REVENUE SHARE only. 0% on the first $1M of lifetime
--                 revenue since 2025-01-01, so it reads 0.00 on most rows.
--                 It is NOT the processing fee, and gross - shopify_fee <> net.
--   net_amount    what lands in the payout, i.e. gross minus the 2.9% billing
--                 processing fee minus any revenue share.
-- So the real deduction is (gross_amount - net_amount). Reading shopify_fee as
-- "what Shopify took" would report zero forever.
create table if not exists transactions (
    id text primary key,
    type text not null,
    created_at timestamptz not null,
    shop_gid text,
    charge_gid text,
    -- ANNUAL or EVERY_30_DAYS, and only on AppSubscriptionSale. This is the one
    -- place the Partner API states the billing interval outright; everywhere
    -- else it has to be inferred from the price (see ingest_raw.plan_interval_for).
    billing_interval text,
    gross_amount numeric(12,2),
    shopify_fee numeric(12,2),
    net_amount numeric(12,2),
    currency_code text,
    ingested_at timestamptz default now()
);

-- The two read patterns: revenue over time, and one merchant's payment history.
create index if not exists transactions_created_at_idx on transactions (created_at);
create index if not exists transactions_shop_created_idx on transactions (shop_gid, created_at);
