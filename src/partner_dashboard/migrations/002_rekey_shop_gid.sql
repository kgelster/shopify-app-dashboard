-- Re-key the pipeline on shop GID: live Partner API verification (2026-08-07)
-- showed AppEvent types have no appInstallation field; shop { id } is the
-- durable per-merchant key. Renames preserve constraints/indexes.
alter table raw_app_events rename column app_installation_id to shop_gid;
alter table app_events rename column app_installation_id to shop_gid;
alter table subscriptions rename column app_installation_id to shop_gid;
alter table shops rename column app_installation_id to shop_gid;

-- Charge amounts now arrive inline on subscription events (AppSubscription
-- carries amount + test); track the test flag so derivation can exclude
-- test charges from MRR.
alter table charges add column test boolean default false;
