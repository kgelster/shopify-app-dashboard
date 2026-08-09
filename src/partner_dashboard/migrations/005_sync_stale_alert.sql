-- De-dupe flag for the stale-sync warning. Kept next to the cursor rather than
-- in the scheduler's memory: a stalled sync is usually a stalled or restarted
-- machine, and in-memory state would turn one outage into an alert every poll.
-- Cleared when a sync succeeds again, so the next episode alerts once more.
alter table sync_state add column if not exists stale_alerted_at timestamptz;
