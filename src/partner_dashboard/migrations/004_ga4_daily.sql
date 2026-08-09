-- App Store listing traffic, pulled from the configured GA4 property.
-- The Partner API exposes no listing traffic at all, so this is the only
-- source for views, sources, and listing-to-install conversion.
--
-- One row per (date, dimension, value). dimension 'total' with value '' is the
-- daily rollup; the rest are breakdowns. Storing the breakdowns as rows rather
-- than columns means a new dimension needs no migration.
create table if not exists ga4_daily (
    date date not null,
    dimension text not null,
    value text not null,
    sessions integer not null default 0,
    users integer not null default 0,
    add_app_clicks integer not null default 0,
    installs integer not null default 0,
    ad_clicks integer not null default 0,
    primary key (date, dimension, value)
);

create index if not exists ga4_daily_dimension_date_idx on ga4_daily (dimension, date);
