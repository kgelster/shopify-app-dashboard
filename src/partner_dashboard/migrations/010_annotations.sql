-- Why a number moved, written down where the number is.
--
-- MRR jumped in March and nothing in this product said why. That knowledge
-- lived in Slack scrollback and in two people's heads, which means it survives
-- exactly as long as those two keep working here. Mixpanel calls these
-- Annotations and draws them on the time series; this is the same thing, one
-- table wide.
--
-- The first write path in the dashboard outside /ingest/usage. Deliberately
-- thin: no edit, no delete, no threading. A note that turns out to be wrong
-- gets a second note correcting it, the same way a ledger works, because an
-- editable history of why the history changed is worse than none.
create table if not exists annotations (
    id bigserial primary key,
    -- The day the thing happened, not the day someone got round to recording
    -- it. A date rather than a timestamp: these mark months on a chart, and
    -- pretending to know the hour would be false precision.
    on_date date not null,
    note text not null,
    -- The signed-in address that wrote it. A dashboard operator, not a
    -- merchant contact, so the no-PII rule that governs shops.owner_name does
    -- not apply here.
    author text not null,
    created_at timestamptz not null default now()
);

-- The charts read this by month, newest first within a month.
create index if not exists annotations_on_date_idx on annotations (on_date desc);
