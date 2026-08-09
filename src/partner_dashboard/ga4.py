"""Pull App Store listing traffic from GA4 into ga4_daily.

The Shopify App Store listing carries your GA4 measurement ID, and Shopify pushes
server-side events to the property via the Measurement Protocol. That gives the
one funnel the Partner API cannot: listing session -> Add App click -> install.

Verified against a live property on the 2026-07 API: the three events this reads
(`Add App button`, `shopify_app_install`, `shopify_app_ad_click`) are the names
Shopify sends. Set GA4_EARLIEST_DATA to the day your property started
collecting, or backfills will ask GA4 for dates that predate the stream.
"""

import json
import logging
from datetime import date, timedelta

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    RunReportRequest,
)
from google.oauth2 import service_account

from partner_dashboard.config import get_settings

logger = logging.getLogger(__name__)

# GA4 event name -> our column. "Add App button" is Shopify's own listing-button
# event; the other two arrive server-side via the Measurement Protocol.
EVENT_COLUMNS = {
    "Add App button": "add_app_clicks",
    "shopify_app_install": "installs",
    "shopify_app_ad_click": "ad_clicks",
}

# our dimension key -> GA4 dimension name ('total' is the daily rollup)
DIMENSIONS = {
    "total": None,
    "channel": "sessionDefaultChannelGroup",
    "source": "sessionSource",
    "country": "country",
    "language": "language",
}

# GA4 reprocesses recent days, so each sync rewrites a trailing window rather
# than trusting what it already stored.
DEFAULT_LOOKBACK_DAYS = 90


def build_client(credentials_json: str) -> BetaAnalyticsDataClient:
    info = json.loads(credentials_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=creds)


def _event_filter() -> FilterExpression:
    return FilterExpression(
        filter=Filter(
            field_name="eventName",
            in_list_filter=Filter.InListFilter(values=list(EVENT_COLUMNS)),
        )
    )


def _ga_date(raw: str) -> date:
    return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))


def fetch_rows(client, property_id: str, start: date, end: date) -> list[dict]:
    """One row per (date, dimension, value), sessions/users merged with events."""
    prop = f"properties/{property_id}"
    date_range = DateRange(start_date=start.isoformat(), end_date=end.isoformat())
    merged: dict[tuple, dict] = {}

    def slot(key):
        return merged.setdefault(key, {
            "date": key[0], "dimension": key[1], "value": key[2],
            "sessions": 0, "users": 0,
            "add_app_clicks": 0, "installs": 0, "ad_clicks": 0,
        })

    for dim_key, ga_dim in DIMENSIONS.items():
        dims = [Dimension(name="date")] + ([Dimension(name=ga_dim)] if ga_dim else [])

        traffic = client.run_report(RunReportRequest(
            property=prop, date_ranges=[date_range], dimensions=dims,
            metrics=[Metric(name="sessions"), Metric(name="totalUsers")],
            limit=100_000,
        ))
        for row in traffic.rows:
            values = [v.value for v in row.dimension_values]
            key = (_ga_date(values[0]), dim_key, values[1] if ga_dim else "")
            row_out = slot(key)
            row_out["sessions"] = int(row.metric_values[0].value)
            row_out["users"] = int(row.metric_values[1].value)

        events = client.run_report(RunReportRequest(
            property=prop, date_ranges=[date_range],
            dimensions=dims + [Dimension(name="eventName")],
            metrics=[Metric(name="eventCount")],
            dimension_filter=_event_filter(), limit=100_000,
        ))
        for row in events.rows:
            values = [v.value for v in row.dimension_values]
            event_name = values[-1]
            key = (_ga_date(values[0]), dim_key, values[1] if ga_dim else "")
            slot(key)[EVENT_COLUMNS[event_name]] = int(row.metric_values[0].value)

    return list(merged.values())


def upsert_rows(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                insert into ga4_daily
                    (date, dimension, value, sessions, users,
                     add_app_clicks, installs, ad_clicks)
                values (%(date)s, %(dimension)s, %(value)s, %(sessions)s, %(users)s,
                        %(add_app_clicks)s, %(installs)s, %(ad_clicks)s)
                on conflict (date, dimension, value) do update set
                    sessions = excluded.sessions,
                    users = excluded.users,
                    add_app_clicks = excluded.add_app_clicks,
                    installs = excluded.installs,
                    ad_clicks = excluded.ad_clicks
                """,
                row,
            )
    conn.commit()
    return len(rows)


def sync_ga4(conn, client, property_id: str, *, lookback_days=DEFAULT_LOOKBACK_DAYS,
             today=None, earliest=None) -> int:
    """Refresh a trailing window. On an empty table, pull everything instead.

    "Everything" starts at GA4_EARLIEST_DATA. Setting it earlier than the day
    the property started collecting only costs empty days in the first backfill.
    """
    today = today or date.today()
    if earliest is None:
        earliest = get_settings().ga4_earliest_data
    (existing,) = conn.execute("select count(*) from ga4_daily").fetchone()
    start = earliest if not existing else today - timedelta(days=lookback_days)
    rows = fetch_rows(client, property_id, start, today)
    written = upsert_rows(conn, rows)
    logger.info("ga4 sync wrote %s rows from %s to %s", written, start, today)
    return written
