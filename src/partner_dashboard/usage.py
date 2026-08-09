"""Product-usage events pushed by the app itself, and the activation reports
built on them.

Everything the Partner API knows is lifecycle: installed, paid, left. It has no
idea whether a merchant ever configured anything, so "installed but never
activated" is invisible from that side. This module is the receiving half of the fix; the
app-side contract is `docs/usage-events-integration.md`.

The endpoint is fed by an external caller holding a shared secret, so validation
here is deliberately strict and total: unknown event types are rejected rather
than stored, every string is length-capped, and `properties` is capped and must
be a flat JSON object. A caller that is buggy or compromised should be able to
lose its own data, not fill the disk or poison a report.
"""

import json
import logging
import math
import re
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from partner_dashboard.config import get_settings

logger = logging.getLogger(__name__)

# The join key to everything else. Anchored, so a caller cannot smuggle a
# free-form bucket key past it.
SHOP_GID_RE = re.compile(r"^gid://shopify/Shop/\d{1,32}$")

# The only event names that exist, from USAGE_EVENT_TYPES. A name outside the
# set is a bug in the app or an attacker probing, and either way storing it
# would quietly corrupt the reports. Teaching the dashboard a new name is a
# config change the operator has to make first, on purpose.
#
# Two of them carry meaning beyond being counted: the activation event is what
# "the merchant built something" means, and the live event is what proves it is
# running for shoppers. Onboarding-completed alone is not activation.


def event_types() -> frozenset[str]:
    return get_settings().usage_event_types_set


def activation_event() -> str:
    return get_settings().usage_activation_event


def live_event() -> str:
    return get_settings().usage_live_event


# No usage event can predate the Shopify App Store. An event older than this is
# a broken clock or a forged backfill, never a real one.
EARLIEST_EVENT = datetime(2009, 1, 1, tzinfo=timezone.utc)

MAX_BODY_BYTES = 1_048_576      # 1 MB, comfortably above a full 500-event batch
MAX_BATCH = 500
MAX_ID_LEN = 200
MAX_PROPERTIES_BYTES = 4096
MAX_PROPERTY_KEYS = 25
# Furthest into the future a client clock may drift before we stop believing it.
MAX_CLOCK_SKEW_SECONDS = 300
# Per-shop flood ceiling over a rolling day. A real shop generates impressions,
# not tens of thousands of them; anything past this is a loop or an attack, and
# dropping it protects both the disk and the activation percentages.
PER_SHOP_DAILY_CAP = 20_000


class UsageError(Exception):
    """A rejected payload. `status` is the HTTP status the route should return.

    The message is intentionally about the payload's shape, never about stored
    data: a caller learns that its own request was malformed, nothing else.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _text(value, field: str, limit: int = MAX_ID_LEN) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError(422, f"{field} must be a non-empty string")
    if len(value) > limit:
        raise UsageError(422, f"{field} exceeds {limit} characters")
    return value


def _timestamp(value, field: str, now: datetime) -> datetime:
    if not isinstance(value, str):
        raise UsageError(422, f"{field} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise UsageError(422, f"{field} is not a valid ISO 8601 timestamp") from None
    if parsed.tzinfo is None:
        raise UsageError(422, f"{field} must carry a timezone offset")
    if (parsed - now).total_seconds() > MAX_CLOCK_SKEW_SECONDS:
        raise UsageError(422, f"{field} is in the future")
    # A floor as well as a ceiling. Without one, a backdated event rewrites the
    # activation reports: time_to_activation happily returns a negative median
    # when an "activation" predates the install it is measured from, and the
    # cohort rates jump to 100%. The endpoint takes a shared secret rather than
    # a per-shop credential, so anyone holding it could otherwise fabricate the
    # headline number for every merchant.
    if parsed < EARLIEST_EVENT:
        raise UsageError(422, f"{field} is before {EARLIEST_EVENT.date().isoformat()}")
    return parsed


def _properties(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UsageError(422, "properties must be an object")
    if len(value) > MAX_PROPERTY_KEYS:
        raise UsageError(422, f"properties has more than {MAX_PROPERTY_KEYS} keys")
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64:
            raise UsageError(422, "properties keys must be strings under 64 characters")
        # Scalars only: nesting is where unbounded payloads hide, and nothing in
        # the spec needs it.
        if not isinstance(item, (str, int, float, bool, type(None))):
            raise UsageError(422, f"properties.{key} must be a string, number, or boolean")
        # NaN and Infinity are floats, so they pass the check above and then die
        # in Postgres as an InvalidTextRepresentation, turning a malformed
        # payload into a 500. json.loads accepts all three by default.
        if isinstance(item, float) and not math.isfinite(item):
            raise UsageError(422, f"properties.{key} must be a finite number")
        # int has no width limit in Python, but str() on a huge one raises
        # above 4300 digits (CVE-2020-10735 mitigation), so rendering it later
        # would be the crash instead.
        if isinstance(item, int) and not isinstance(item, bool) and abs(item) >= 10 ** 38:
            raise UsageError(422, f"properties.{key} is out of range")
        if isinstance(item, str) and len(item) > 500:
            raise UsageError(422, f"properties.{key} exceeds 500 characters")
    if len(json.dumps(value)) > MAX_PROPERTIES_BYTES:
        raise UsageError(422, f"properties exceeds {MAX_PROPERTIES_BYTES} bytes")
    return value


def parse_batch(raw: bytes, now: datetime | None = None) -> list[dict]:
    """Turn a request body into validated events, or raise `UsageError`.

    Pure, so the whole validation surface is testable without a request or a
    database. Nothing partially valid gets through: one bad event rejects the
    batch, which keeps the caller's retry semantics simple (fix and resend).
    """
    now = now or datetime.now(timezone.utc)
    try:
        payload = json.loads(raw)
    except Exception:
        # Deliberately broad. Beyond JSONDecodeError and UnicodeDecodeError, a
        # deeply nested body raises RecursionError and a 100k-digit number
        # raises ValueError from the int-str conversion limit. Every one of
        # those is a malformed request, and letting any of them escape turns a
        # 400 into a 500 on the one route an anonymous caller can reach.
        raise UsageError(400, "body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise UsageError(422, "body must be a JSON object")

    events = payload.get("events")
    if not isinstance(events, list):
        raise UsageError(422, "events must be an array")
    if not events:
        raise UsageError(422, "events is empty")
    if len(events) > MAX_BATCH:
        raise UsageError(413, f"batch exceeds {MAX_BATCH} events")

    parsed, seen = [], set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise UsageError(422, f"events[{index}] must be an object")
        event_type = _text(event.get("event_type"), f"events[{index}].event_type")
        if event_type not in event_types():
            raise UsageError(422, f"events[{index}].event_type is not a known event")
        shop_gid = _text(event.get("shop_gid"), f"events[{index}].shop_gid")
        # Shape-checked, not existence-checked: the shop may legitimately not be
        # in `shops` yet, because usage can arrive before the Partner API poll
        # that creates the row. But an arbitrary string is a free per-shop
        # rate-limit bucket and an unbounded set of junk keys, so require the
        # GID form the contract asks for.
        if not SHOP_GID_RE.match(shop_gid):
            raise UsageError(
                422,
                f"events[{index}].shop_gid must be a Shopify shop GID, "
                "e.g. gid://shopify/Shop/12345678",
            )
        event_id = _text(event.get("event_id"), f"events[{index}].event_id")
        key = (shop_gid, event_id)
        if key in seen:
            raise UsageError(422, f"events[{index}].event_id is repeated in this batch")
        seen.add(key)
        parsed.append({
            "shop_gid": shop_gid,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": _timestamp(event.get("occurred_at"),
                                      f"events[{index}].occurred_at", now),
            "properties": _properties(event.get("properties")),
        })
    return parsed


def _over_cap(conn: psycopg.Connection, shop_gids: set[str]) -> set[str]:
    rows = conn.execute(
        """select shop_gid, count(*) from usage_events
           where shop_gid = any(%s) and received_at >= now() - interval '1 day'
           group by shop_gid""",
        (list(shop_gids),),
    ).fetchall()
    return {gid for gid, count in rows if count >= PER_SHOP_DAILY_CAP}


def ingest(conn: psycopg.Connection, events: list[dict]) -> dict:
    """Store validated events. Idempotent, and never destructive.

    ON CONFLICT DO NOTHING rather than DO UPDATE: a retry after a timeout is
    free, and no caller can rewrite an event that is already recorded. The
    caller gets back what actually happened, so a client that keeps re-sending
    the same batch can tell.
    """
    capped = _over_cap(conn, {e["shop_gid"] for e in events})
    if capped:
        logger.warning("usage ingest: %d shop(s) over the daily cap, dropping their events",
                       len(capped))

    accepted = [e for e in events if e["shop_gid"] not in capped]
    stored = 0
    # One transaction for the batch. The connection is autocommit, so without
    # this each insert commits on its own and a failure part-way through leaves
    # a partially stored batch behind -- which contradicts the all-or-nothing
    # contract the caller retries against.
    with conn.transaction():
        for event in accepted:
            stored += conn.execute(
                """insert into usage_events
                       (shop_gid, event_id, event_type, occurred_at, properties)
                   values (%s, %s, %s, %s, %s)
                   on conflict (shop_gid, event_id) do nothing""",
                (event["shop_gid"], event["event_id"], event["event_type"],
                 event["occurred_at"], Jsonb(event["properties"])),
            ).rowcount
    return {
        "received": len(events),
        "stored": stored,
        "duplicates": len(accepted) - stored,
        "rate_limited": len(events) - len(accepted),
    }


# --- reports ---------------------------------------------------------------

def has_usage_data(conn: psycopg.Connection) -> bool:
    """Whether an ACTIVATION event has ever arrived.

    Drives the empty state, so a page says "waiting for the app" rather than
    reporting 0% activation as fact.

    Scoped to the activation event specifically, not to any event at all. Those
    are the same question only when the app is wired correctly. If
    USAGE_ACTIVATION_EVENT names something the app never sends, every other
    event still flows, so "any event exists" is True, the empty state is
    skipped, and the funnel states 0% activation for merchants who all
    activated. Asking the narrower question makes that configuration read as
    unknown, which is what it is.
    """
    return conn.execute(
        "select exists (select 1 from usage_events where event_type = %s)",
        (activation_event(),),
    ).fetchone()[0]


def activation_cohorts(conn: psycopg.Connection, months: int = 6) -> list[dict]:
    """Per install-month: what share of the cohort built their first offer
    within 48 hours, and within 7 days.

    Only counts shops that installed after usage tracking started, since a shop
    that installed in 2025 has no `offer_created` event and would otherwise read
    as a merchant who never activated.
    """
    rows = conn.execute(
        """
        with tracking_start as (select min(received_at) as at from usage_events),
        installs as (
            select shop_gid, min(occurred_at) as first_install
            from app_events where type = 'installed'
            group by shop_gid
        ),
        activation as (
            select shop_gid, min(occurred_at) as first_offer
            from usage_events where event_type = %s
            group by shop_gid
        )
        select to_char(date_trunc('month', i.first_install), 'Mon YYYY'),
               date_trunc('month', i.first_install) as month,
               count(*) as cohort,
               count(*) filter (
                   where a.first_offer <= i.first_install + interval '48 hours') as within_48h,
               count(*) filter (
                   where a.first_offer <= i.first_install + interval '7 days') as within_7d
        from installs i
        cross join tracking_start t
        left join activation a on a.shop_gid = i.shop_gid
        where i.first_install >= greatest(
                  t.at, date_trunc('month', now()) - make_interval(months => %s - 1))
        group by 2, 1
        order by 2
        """,
        (activation_event(), months),
    ).fetchall()
    return [
        {
            "label": label,
            "cohort": cohort,
            "within_48h": within_48h,
            "within_7d": within_7d,
            "rate_48h": round(100 * within_48h / cohort) if cohort else 0,
            "rate_7d": round(100 * within_7d / cohort) if cohort else 0,
        }
        for label, _, cohort, within_48h, within_7d in rows
    ]


def time_to_activation(conn: psycopg.Connection) -> dict:
    """Median hours from install to first offer, and the share who never got
    there. Median rather than mean: one merchant who activates a year later
    would drag an average into meaninglessness."""
    row = conn.execute(
        """
        with tracking_start as (select min(received_at) as at from usage_events),
        installs as (
            select shop_gid, min(occurred_at) as first_install
            from app_events where type = 'installed'
            group by shop_gid
        ),
        activation as (
            select shop_gid, min(occurred_at) as first_offer
            from usage_events where event_type = %s
            group by shop_gid
        ),
        eligible as (
            select i.shop_gid, i.first_install, a.first_offer
            from installs i cross join tracking_start t
            left join activation a on a.shop_gid = i.shop_gid
            where i.first_install >= t.at
        )
        select count(*),
               count(first_offer),
               percentile_cont(0.5) within group (
                   order by extract(epoch from (first_offer - first_install)) / 3600)
                   filter (where first_offer is not null)
        from eligible
        """,
        (activation_event(),),
    ).fetchone()
    eligible, activated, median_hours = row
    return {
        "eligible": eligible,
        "activated": activated,
        "never": eligible - activated,
        "rate": round(100 * activated / eligible) if eligible else 0,
        "median_hours": round(median_hours, 1) if median_hours is not None else None,
    }


def at_risk_shops(conn: psycopg.Connection, days: int = 14) -> list[dict]:
    """Paying shops whose offers have shown to nobody in `days`.

    A merchant paying every month for an app that is serving nothing will
    notice eventually; the point is to get there first. Only shops whose app has ever reported an impression
    are eligible, so a shop that predates tracking is not accused of silence.
    """
    rows = conn.execute(
        """
        select coalesce(s.shop_name, s.shop_domain, s.shop_gid) as shop,
               s.shop_domain, sub.monthly_amount, max(u.occurred_at) as last_seen
        from shops s
        join subscriptions sub
          on sub.shop_gid = s.shop_gid and sub.churned_at is null
        join usage_events u
          on u.shop_gid = s.shop_gid and u.event_type = %s
        where s.install_state = 'installed'
        group by 1, 2, 3
        having max(u.occurred_at) < now() - make_interval(days => %s)
        order by max(u.occurred_at)
        """,
        (live_event(), days),
    ).fetchall()
    now = datetime.now(timezone.utc)
    return [
        {"shop": shop, "domain": domain, "monthly_amount": amount,
         "last_seen": last_seen, "days_quiet": (now - last_seen).days}
        for shop, domain, amount, last_seen in rows
    ]
