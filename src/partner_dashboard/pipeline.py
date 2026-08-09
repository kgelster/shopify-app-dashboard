import time
from datetime import datetime, timedelta, timezone

import psycopg

from partner_dashboard.derive import derive_installations
from partner_dashboard.ingest_raw import upsert_charges, upsert_raw_events, upsert_transactions
from partner_dashboard.partner_api import fetch_app_events, fetch_transactions
from partner_dashboard.slack import notify_events

SOURCE = "partner_api"
TRANSACTIONS_SOURCE = "partner_transactions"

# The Partner API rate-limits hard enough to 429 an introspection burst, and the
# transactions feed is the only place we page in a tight loop. Same 0.3s the
# forsbergplustwo/partner-metrics importer settled on.
THROTTLE_SECONDS = 0.3


def run_sync(conn: psycopg.Connection, client, settings, http_post) -> dict:
    """Poll the Partner API, derive clean events, and Slack-notify fresh installs.

    poll_overlap_minutes is not applied here: fetch_app_events' cursor (Task 7)
    is an opaque Partner API pagination token with no time semantics to rewind,
    and upsert_raw_events already dedups on conflict, so replaying the last
    page on the next poll (which happens naturally, since a fully-drained
    cursor has nothing further to advance to) is the safety margin in practice.
    """
    start_ts = datetime.now(timezone.utc)

    row = conn.execute(
        "select cursor from sync_state where source = %s", (SOURCE,)
    ).fetchone()
    cursor = row[0] if row else None

    # Snapshot before deriving: ON CONFLICT (platform_event_id) DO NOTHING
    # means already-seen events never get a new id, so any app_events row with
    # id > snapshot after this run is a genuinely new lifecycle event.
    (snapshot,) = conn.execute("select coalesce(max(id), 0) from app_events").fetchone()

    raw_inserted = 0
    touched_ids: set[str] = set()
    while True:
        events, next_cursor = fetch_app_events(
            client, app_id=settings.partner_app_id, after_cursor=cursor
        )
        raw_inserted += upsert_raw_events(conn, events)
        upsert_charges(conn, events)
        touched_ids.update(e["shop_gid"] for e in events)
        if next_cursor is None:
            break
        cursor = next_cursor

    # Derive exactly the installs touched by this poll, not derive_all_dirty's
    # since=start_ts: that compares an app-clock timestamp against DB-clock
    # ingested_at, so with the app and database on separate machines clock skew can
    # permanently skip a freshly-ingested install.
    event_counts = derive_installations(conn, touched_ids)

    alertable = conn.execute(
        """
        select shop_gid, type from app_events
        where id > %s and type in ('installed', 'reinstalled', 'uninstalled')
        order by id
        """,
        (snapshot,),
    ).fetchall()
    alerts_sent = notify_events(conn, alertable, settings.slack_webhook_url,
                                http_post=http_post,
                                base_url=settings.public_base_url)

    conn.execute(
        """
        insert into sync_state (source, cursor, last_synced_at)
        values (%s, %s, %s)
        on conflict (source) do update set
            cursor = excluded.cursor,
            last_synced_at = excluded.last_synced_at
        """,
        (SOURCE, cursor, start_ts),
    )
    conn.commit()

    return {
        "raw_inserted": raw_inserted,
        "events_emitted": sum(event_counts.values()),
        "alerts_sent": alerts_sent,
    }


def sync_transactions(conn: psycopg.Connection, client, settings,
                      sleep=time.sleep) -> dict:
    """Poll the money feed into `transactions`.

    Its own sync_state row, keyed apart from the events cursor, so replaying
    payments never forces an event replay (and a broken money sync never stalls
    the lifecycle one).

    No stored cursor. `transactions` accepts createdAtMin, so the window is
    derived from the data itself -- the newest transaction we hold, rewound by
    poll_overlap_minutes. That is what makes the overlap idea work here at all:
    on the events feed the cursor is an opaque token with no time semantics and
    nothing to rewind (see run_sync). An empty table means no bound, i.e. pull
    the whole history.
    """
    start_ts = datetime.now(timezone.utc)

    (latest,) = conn.execute("select max(created_at) from transactions").fetchone()
    created_at_min = None
    if latest is not None:
        # Normalized to UTC before formatting: psycopg hands back timestamptz in
        # the session's timezone, so an un-normalized isoformat would send
        # Shopify a local offset that reads correctly but is needlessly
        # ambiguous next to every other timestamp in this codebase.
        created_at_min = (
            latest - timedelta(minutes=settings.poll_overlap_minutes)
        ).astimezone(timezone.utc).isoformat()

    cursor = None
    inserted = seen = pages = 0
    while True:
        rows, next_cursor = fetch_transactions(
            client, app_id=settings.partner_app_id, after_cursor=cursor,
            created_at_min=created_at_min,
        )
        inserted += upsert_transactions(conn, rows)
        seen += len(rows)
        pages += 1
        if next_cursor is None:
            break
        cursor = next_cursor
        sleep(THROTTLE_SECONDS)

    conn.execute(
        """
        insert into sync_state (source, cursor, last_synced_at)
        values (%s, null, %s)
        on conflict (source) do update set last_synced_at = excluded.last_synced_at
        """,
        (TRANSACTIONS_SOURCE, start_ts),
    )
    conn.commit()

    return {"transactions_seen": seen, "transactions_inserted": inserted,
            "pages": pages, "since": created_at_min}
