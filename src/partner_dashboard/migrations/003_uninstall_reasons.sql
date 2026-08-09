-- Uninstall feedback from RelationshipUninstalled (reason + description).
-- Both fields are optional and often absent: Shopify only made the question
-- mandatory partway through 2026, and free text is rarer still. Stored raw;
-- bucketing happens at read time in
-- partner_dashboard.uninstall_reasons so a mapping change never needs a backfill.
alter table app_events add column if not exists uninstall_reason text;
alter table app_events add column if not exists uninstall_description text;
alter table shops add column if not exists uninstall_reason text;
alter table shops add column if not exists uninstall_description text;

-- These tables have had no indexes beyond PKs/uniques. Every dashboard
-- aggregate filters app_events on type and occurred_at, or joins on shop_gid.
create index if not exists app_events_type_occurred_at_idx on app_events (type, occurred_at);
create index if not exists app_events_shop_gid_idx on app_events (shop_gid);
create index if not exists shops_install_state_idx on shops (install_state);
