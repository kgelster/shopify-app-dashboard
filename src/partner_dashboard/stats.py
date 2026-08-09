"""Read-side aggregates for the dashboard pages. All pure SQL/Python over
app_events / shops / subscriptions; no external data sources."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg

from partner_dashboard.config import get_settings
from partner_dashboard.uninstall_reasons import (
    bucket_counts,
    classify,
    language_counts,
    split_reasons,
)

# Every paying subscription joins back to the charge it came from, which is
# where the billing interval lives.
_ACTIVE_PAYING = """
    from subscriptions sub join shops s on s.shop_gid = sub.shop_gid
    where sub.churned_at is null and s.install_state = 'installed'
"""


def overview_stats(conn: psycopg.Connection) -> dict:
    def one(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    installed = one("select count(*) from shops where install_state = 'installed'")
    active_mrr = one(f"select coalesce(sum(sub.monthly_amount), 0) {_ACTIVE_PAYING}")
    paying = one(f"select count(distinct sub.shop_gid) {_ACTIVE_PAYING}")
    uninstalls_30d = one(
        """select count(*) from app_events
           where type = 'uninstalled' and occurred_at >= now() - interval '30 days'"""
    )
    # Logo churn over the window: uninstalls as a share of the shops that were
    # installed when the window opened (today's installed base plus everyone who
    # left during it).
    exposed = installed + uninstalls_30d

    return {
        "installed": installed,
        "active_mrr": active_mrr,
        "paying": paying,
        "arpu": (active_mrr / paying) if paying else Decimal("0"),
        "installs_30d": one(
            """select count(*) from app_events
               where type in ('installed', 'reinstalled')
               and occurred_at >= now() - interval '30 days'"""
        ),
        "uninstalls_30d": uninstalls_30d,
        "churn_30d": round(100 * uninstalls_30d / exposed, 1) if exposed else 0.0,
    }


COMPARED = ("installed", "active_mrr", "paying", "installs_30d",
            "uninstalls_30d", "net_30d")


def installed_at_time(conn: psycopg.Connection, t) -> int:
    """How many shops had the app installed at an instant.

    Replayed from app_events rather than read off shops.install_state, which
    only ever describes now. Same rule the customer page uses to decide a shop's
    current state: whatever its last lifecycle event says. A shop that
    uninstalled and reinstalled before `t` counts as installed.
    """
    return conn.execute(
        """
        select count(*) from (
            select distinct on (shop_gid) shop_gid, type
            from app_events
            where type in ('installed', 'reinstalled', 'uninstalled')
              and occurred_at <= %s
            order by shop_gid, occurred_at desc, id desc
        ) last where type in ('installed', 'reinstalled')
        """,
        (t,),
    ).fetchone()[0]


def overview_comparison(conn: psycopg.Connection, current: dict,
                        days: int = 30) -> dict:
    """Each headline figure against what it was, so a number reads as a signal.

    A separate function rather than a window parameter on `overview_stats`:
    every caller of that function today expects exactly what it returns, and the
    money figures are the last place to introduce a signature that could be
    called two ways. This one adds; it changes nothing.

    Point-in-time metrics (installed base, MRR, paying shops) are compared to
    their own value `days` ago. Windowed counts (installs, uninstalls, cash
    collected) are compared to the window immediately before this one. Mixing
    those two up is the easiest way to render a comparison that is confidently
    wrong, which is why the kind lives on the metric in partner_dashboard.metrics rather than
    being guessed here.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    prior_start = cutoff - timedelta(days=days)

    def events_between(types, start, end) -> int:
        return conn.execute(
            """select count(*) from app_events
               where type = any(%s) and occurred_at >= %s and occurred_at < %s""",
            (list(types), start, end),
        ).fetchone()[0]

    (prior_net,) = conn.execute(
        """select coalesce(sum(net_amount), 0) from transactions
           where created_at >= %s and created_at < %s""",
        (prior_start, cutoff),
    ).fetchone()

    prior = {
        "installed": installed_at_time(conn, cutoff),
        "active_mrr": mrr_at(conn, cutoff),
        "paying": paying_at(conn, cutoff),
        "installs_30d": events_between(("installed", "reinstalled"),
                                       prior_start, cutoff),
        "uninstalls_30d": events_between(("uninstalled",), prior_start, cutoff),
        "net_30d": prior_net,
    }

    out = {}
    for key in COMPARED:
        was, is_now = prior[key], current[key]
        change = is_now - was
        out[key] = {
            "prior": was,
            "change": change,
            # No percentage from a zero base. "up 100%" from nothing is a
            # division by zero dressed as a fact.
            "pct": round(100 * float(change) / float(was), 1) if was else None,
            "days": days,
        }
    return out


def unit_economics(conn: psycopg.Connection, days: int = 90) -> dict:
    """ARPU and LTV.

    LTV = ARPU / subscription churn rate, the standard definition (and the one
    forsbergplustwo/partner-metrics implements). It says: at the rate merchants
    currently leave, the average paying merchant is worth this much before they
    do.

    The churn rate is measured over 90 days rather than 30 and then divided down
    to a month. On a small paying base a 30-day window is one or two events, and
    a single departure would swing LTV by thousands of dollars. It is still a
    small sample -- `subs_at_start` ships alongside so the page can say so -- and
    LTV is None rather than infinity when nobody churned, because "nobody left
    this quarter" is not evidence that nobody ever will.
    """
    start = datetime.now(timezone.utc) - timedelta(days=days)
    at_start = paying_at(conn, start)
    (churned,) = conn.execute(
        """select count(*) from subscriptions
           where churned_at >= %s and converted_at < %s""",
        (start, start),
    ).fetchone()

    active_mrr = conn.execute(f"select coalesce(sum(sub.monthly_amount), 0) {_ACTIVE_PAYING}").fetchone()[0]
    paying = conn.execute(f"select count(distinct sub.shop_gid) {_ACTIVE_PAYING}").fetchone()[0]
    arpu = (active_mrr / paying) if paying else Decimal("0")

    monthly_churn = (churned / at_start) * (30 / days) if at_start else 0.0
    return {
        "arpu": arpu,
        "paying": paying,
        "window_days": days,
        "subs_at_start": at_start,
        "churned_in_window": churned,
        "monthly_churn_pct": round(100 * monthly_churn, 1),
        "ltv": (arpu / Decimal(str(monthly_churn))) if monthly_churn else None,
    }


def mrr_trend(conn: psycopg.Connection, months: int = 12) -> list[dict]:
    """MRR at each month end: every subscription converted by then and not yet
    churned. Computed from subscriptions rather than app_events.net_change,
    because net_change on historical rows was recorded before the annual-plan
    interval fix and still carries the old inflated figures."""
    rows = conn.execute(
        """
        with bounds as (
            select generate_series(
                date_trunc('month', now()) - make_interval(months => %s - 1),
                date_trunc('month', now()),
                interval '1 month'
            ) as month_start
        )
        select to_char(b.month_start, 'Mon YYYY') as label,
               b.month_start,
               coalesce(sum(sub.monthly_amount) filter (
                   where sub.converted_at < b.month_start + interval '1 month'
                     and (sub.churned_at is null
                          or sub.churned_at >= b.month_start + interval '1 month')
               ), 0) as mrr
        from bounds b left join subscriptions sub on true
        group by 1, 2
        order by 2
        """,
        (months,),
    ).fetchall()
    return [{"label": label, "mrr": mrr} for label, _, mrr in rows]


MOVEMENT_KINDS = ("new", "reactivation", "expansion", "contraction", "churned")


def _attribute(bucket: dict, prev: Decimal, curr: Decimal, returning: bool) -> None:
    """Put one shop's change in MRR into exactly one bucket.

    Shared by the monthly waterfall and the weekly digest so the two can never
    label the same movement differently. Every branch moves the full delta, so
    the buckets always sum back to the total change.
    """
    if curr == prev:
        return
    if prev == 0:
        bucket["reactivation" if returning else "new"] += curr
    elif curr == 0:
        bucket["churned"] -= prev
    elif curr > prev:
        bucket["expansion"] += curr - prev
    else:
        bucket["contraction"] += curr - prev


def mrr_movement_between(conn: psycopg.Connection, start, end) -> dict:
    """The same five buckets as `mrr_movements`, over an arbitrary span.

    Used for the weekly digest. `start` and `end` are instants, not month ends,
    so a subscription counts if it had converted by then and had not churned.
    """
    rows = conn.execute(
        """select shop_gid, coalesce(monthly_amount, 0), converted_at, churned_at
           from subscriptions where converted_at is not null"""
    ).fetchall()

    def at(t):
        totals: dict[str, Decimal] = {}
        for shop_gid, amount, converted_at, churned_at in rows:
            if converted_at <= t and (churned_at is None or churned_at > t):
                totals[shop_gid] = totals.get(shop_gid, Decimal("0")) + amount
        return totals

    before, after = at(start), at(end)
    # Anyone who had already converted before the window opened and is coming
    # back inside it is a reactivation, not a new customer.
    ever_before = {gid for gid, _, converted_at, _ in rows if converted_at <= start}

    bucket = {k: Decimal("0") for k in MOVEMENT_KINDS}
    for shop_gid in set(before) | set(after):
        _attribute(bucket, before.get(shop_gid, Decimal("0")),
                   after.get(shop_gid, Decimal("0")), shop_gid in ever_before)
    bucket["net"] = sum(bucket.values())
    return bucket


def mrr_at(conn: psycopg.Connection, t) -> Decimal:
    """Total MRR at an instant. Same basis as `mrr_trend`."""
    return conn.execute(
        """select coalesce(sum(monthly_amount), 0) from subscriptions
           where converted_at <= %s and (churned_at is null or churned_at > %s)""",
        (t, t),
    ).fetchone()[0]


def paying_at(conn: psycopg.Connection, t) -> int:
    return conn.execute(
        """select count(distinct shop_gid) from subscriptions
           where converted_at <= %s and (churned_at is null or churned_at > %s)""",
        (t, t),
    ).fetchone()[0]


def _month_index(dt) -> int:
    """Months since year 0, so month arithmetic is plain integer arithmetic."""
    return dt.year * 12 + dt.month - 1


def mrr_movements(conn: psycopg.Connection, months: int = 12) -> list[dict]:
    """Why MRR moved each month: new, reactivation, expansion, contraction, churn.

    Computed as the month-over-month delta of each *shop's* MRR, not from
    app_events.net_change (those rows predate the annual-plan fix and are
    immutable by design). Because every bucket is a piece of a per-shop delta,
    the five buckets sum exactly to the change in `mrr_trend` between the same
    two months: the waterfall can never disagree with the trend line above it.

    A shop going 0 -> positive is new, unless it was paying in some earlier
    month, in which case it is a reactivation.
    """
    rows = conn.execute(
        """select shop_gid, coalesce(monthly_amount, 0), converted_at, churned_at
           from subscriptions where converted_at is not null"""
    ).fetchall()

    now = datetime.now(timezone.utc)
    last = _month_index(now)
    first = last - months + 1

    # Per shop, its MRR at the end of every month back to the first conversion
    # in the data. The history before the window is what separates a real
    # reactivation from a first-time subscriber; only the window is rendered.
    earliest = min((_month_index(r[2]) for r in rows), default=first)
    span = range(min(earliest, first) - 1, last + 1)
    by_shop: dict[str, list[Decimal]] = {}
    for shop_gid, amount, converted_at, churned_at in rows:
        conv = _month_index(converted_at)
        churn = _month_index(churned_at) if churned_at is not None else None
        series = by_shop.setdefault(shop_gid, [Decimal("0")] * len(span))
        for i, m in enumerate(span):
            if conv <= m and (churn is None or churn > m):
                series[i] += amount

    out = []
    for i, m in enumerate(span):
        if i == 0 or m < first:
            continue  # baseline / pre-window months carry history, not output
        bucket = {k: Decimal("0") for k in MOVEMENT_KINDS}
        for series in by_shop.values():
            # Paid before this gap? Then the money is coming back, not new.
            _attribute(bucket, series[i - 1], series[i],
                       returning=any(v > 0 for v in series[:i]))
        out.append({
            "label": f"{MONTH_NAMES[m % 12]} {m // 12}",
            **{k: bucket[k] for k in MOVEMENT_KINDS},
            "net": sum(bucket.values()),
        })
    return out


MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# AppSaleAdjustment / AppSaleCredit are money coming back out: a refund, a
# downgrade adjustment, or a credit. They arrive with negative amounts, so they
# net out of revenue automatically and are counted separately only so the page
# can say how much was returned.
REFUND_TYPES = ("AppSaleAdjustment", "AppSaleCredit")


def collected_revenue(conn: psycopg.Connection) -> dict:
    """Cash, not projection: what was actually billed and what actually landed.

    MRR answers "what are we owed each month at today's subscription state".
    This answers "what moved". They are different numbers and both are right,
    which is why the page shows both and says so.

    `taken` is grossed minus netted, NOT the shopify_fee column. shopify_fee is
    Shopify's revenue share, which is 0% below $1M of lifetime revenue and so
    reads 0.00 on most rows; the billing processing fee only ever
    appears in the gap between gross and net. That gap is not a fixed rate
    either -- identically priced charges can settle at 2.895%, 4.895% and 5.895%
    depending on the merchant -- so it is measured, never modelled.
    """
    row = conn.execute(
        """
        select coalesce(sum(gross_amount), 0),
               coalesce(sum(net_amount), 0),
               coalesce(sum(gross_amount) filter (where type = any(%s)), 0),
               count(*) filter (where type = any(%s)),
               min(created_at), max(created_at), count(*)
        from transactions
        """,
        (list(REFUND_TYPES), list(REFUND_TYPES)),
    ).fetchone()
    gross, net, refunded, refund_count, first, last, count = row

    (net_30d,) = conn.execute(
        """select coalesce(sum(net_amount), 0) from transactions
           where created_at >= now() - interval '30 days'"""
    ).fetchone()

    return {
        "gross": gross, "net": net, "taken": gross - net,
        "taken_pct": round(100 * (gross - net) / gross, 2) if gross else 0.0,
        # Refunds are already inside gross as negatives; this is the absolute
        # value of what went back, so it reads as an amount rather than a sign.
        "refunded": -refunded, "refund_count": refund_count,
        "net_30d": net_30d,
        "first_at": first, "last_at": last, "count": count,
    }


def revenue_by_month(conn: psycopg.Connection, months: int = 12) -> list[dict]:
    """Collected revenue per calendar month, on the same 12-month window as
    mrr_trend so the two series can be read side by side."""
    rows = conn.execute(
        """
        with bounds as (
            select generate_series(
                date_trunc('month', now()) - make_interval(months => %s - 1),
                date_trunc('month', now()),
                interval '1 month'
            ) as month_start
        )
        select to_char(b.month_start, 'Mon YYYY') as label,
               b.month_start,
               coalesce(sum(t.gross_amount), 0) as gross,
               coalesce(sum(t.net_amount), 0) as net,
               count(t.id) as charges
        from bounds b
        left join transactions t
               on t.created_at >= b.month_start
              and t.created_at < b.month_start + interval '1 month'
        group by 1, 2
        order by 2
        """,
        (months,),
    ).fetchall()
    return [
        {"label": label, "gross": gross, "net": net, "charges": charges}
        for label, _, gross, net, charges in rows
    ]


def country_breakdown(conn: psycopg.Connection, top: int = 10) -> list[dict]:
    """Shops by country: currently installed, with the all-time count beside it.

    country is backfilled by the CSV importer and is missing for any shop that
    installed after that import ran; the Partner API
    itself exposes no merchant location.
    """
    rows = conn.execute(
        """
        select country,
               count(*) filter (where install_state = 'installed') as installed,
               count(*) as ever
        from shops where country is not null
        group by country
        order by installed desc, ever desc, country
        """
    ).fetchall()
    out = [{"country": c, "installed": i, "ever": e} for c, i, e in rows[:top]]
    rest = rows[top:]
    if rest:
        out.append({
            "country": f"Other ({len(rest)})",
            "installed": sum(r[1] for r in rest),
            "ever": sum(r[2] for r in rest),
            # Flagged rather than sniffed out of the label: this row is a sum of
            # many countries and there is no single /customers filter that
            # reproduces it, so it is the one row that must not become a link.
            "other": True,
        })
    return out


# No prices: they vary per deployment and the MRR column already carries money.
PLAN_LABELS = {"EVERY_30_DAYS": "Monthly", "ANNUAL": "Annual"}


def plan_mix(conn: psycopg.Connection) -> list[dict]:
    """Active paying subscriptions split by billing interval."""
    rows = conn.execute(
        """
        select c.plan_interval, count(*), coalesce(sum(sub.monthly_amount), 0)
        from subscriptions sub
        join charges c on c.gid = sub.id
        join shops s on s.shop_gid = sub.shop_gid
        where sub.churned_at is null
          and s.install_state = 'installed'
          and sub.monthly_amount > 0
        group by c.plan_interval
        order by 3 desc
        """
    ).fetchall()
    return [
        {"label": PLAN_LABELS.get(interval, interval or "Unknown"),
         # The raw interval, so the bar can link to the /customers filter that
         # shows exactly these shops. Null when no charge row was captured, in
         # which case there is nothing to filter by and the row stays unlinked.
         "interval": interval,
         "count": count, "mrr": mrr}
        for interval, count, mrr in rows
    ]


def _coverage(raw: list) -> dict:
    given = [r for r in raw if r]
    return {
        "total": len(raw),
        "with_reason": len(given),
        "coverage_pct": round(100 * len(given) / len(raw), 1) if raw else 0.0,
    }


def uninstall_reasons(conn: psycopg.Connection) -> dict:
    """Canonical uninstall-reason buckets, drawn from the mandatory era only.

    Shopify made the exit question required on REASON_MANDATORY_FROM, and
    coverage either side of that date is not comparable: after it a reason is
    near-universal, before it only a self-selected minority answered. `buckets`
    counts only the mandatory era, because pooling the two buries the good data
    in an almost-empty denominator. `era` carries both halves so the page can
    say so.

    A merchant can pick more than one reason, so the bucket counts do not sum to
    the uninstall count.
    """
    # Denominator is real merchant uninstalls only. app_events folds
    # RELATIONSHIP_DEACTIVATED (store closed or frozen by Shopify) into the same
    # 'uninstalled' type, and those merchants are never shown the survey, so
    # counting them would understate how often the question gets answered.
    rows = conn.execute(
        """
        select e.uninstall_reason, e.occurred_at
        from app_events e
        join raw_app_events r on r.id = e.platform_event_id
        where e.type = 'uninstalled' and r.type = 'RELATIONSHIP_UNINSTALLED'
        """
    ).fetchall()
    mandatory_from = get_settings().reason_mandatory_from
    raw = [r[0] for r in rows]
    post = [r for r, at in rows if at.date() >= mandatory_from]
    pre = [r for r, at in rows if at.date() < mandatory_from]

    counts = bucket_counts([r for r in post if r])
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ordered[0][1] if ordered else 1
    return {
        "buckets": [
            {"label": label, "count": count, "pct": round(100 * count / top)}
            for label, count in ordered
        ],
        "mandatory_from": mandatory_from.isoformat(),
        "era": {"pre": _coverage(pre), "post": _coverage(post)},
        # All-time, for anything that wants the whole feed rather than the era
        # the bars are drawn from.
        **_coverage(raw),
        # Language is a property of the merchant base, not of the survey rules,
        # so it keeps every answer ever given rather than the mandatory era only.
        "languages": sorted(
            ({"lang": k, "count": v}
             for k, v in language_counts([r for r in raw if r]).items()),
            key=lambda d: -d["count"],
        ),
    }


TIME_BUCKETS = (
    ("Same day", 0, 1),
    ("1-7 days", 1, 8),
    ("8-30 days", 8, 31),
    ("31-90 days", 31, 91),
    ("Over 90 days", 91, None),
)


def time_to_uninstall(conn: psycopg.Connection) -> dict:
    """How long uninstalled shops kept the app before leaving."""
    rows = conn.execute(
        """
        select extract(epoch from (uninstalled_at - installed_at)) / 86400.0
        from shops
        where install_state = 'uninstalled'
          and installed_at is not null and uninstalled_at is not null
          and uninstalled_at >= installed_at
        """
    ).fetchall()
    days = sorted(float(r[0]) for r in rows)
    if not days:
        return {"median": None, "count": 0, "buckets": []}

    mid = len(days) // 2
    median = days[mid] if len(days) % 2 else (days[mid - 1] + days[mid]) / 2

    counts = []
    for label, lo, hi in TIME_BUCKETS:
        n = sum(1 for d in days if d >= lo and (hi is None or d < hi))
        counts.append({"label": label, "count": n})
    top = max((c["count"] for c in counts), default=1) or 1
    for c in counts:
        c["pct"] = round(100 * c["count"] / top)
    return {"median": round(median, 1), "count": len(days), "buckets": counts}


def churn_composition(conn: psycopg.Connection) -> list[dict]:
    """Uninstalled shops split by whether they ever paid, so trial tourists
    don't get counted as lost customers."""
    rows = conn.execute(
        """
        select case when sub.shop_gid is null then 'Never subscribed'
                    else 'Had a subscription' end as kind,
               count(*)
        from shops s
        left join lateral (
            select shop_gid from subscriptions where shop_gid = s.shop_gid limit 1
        ) sub on true
        where s.install_state = 'uninstalled'
        group by 1 order by 2 desc
        """
    ).fetchall()
    total = sum(r[1] for r in rows) or 1
    return [
        {"label": kind, "count": count, "pct": round(100 * count / total)}
        for kind, count in rows
    ]


def churn_rows(conn: psycopg.Connection, *, paid: str | None = None,
               gave_reason: str | None = None, bucket: str | None = None,
               since_days: int | None = None, limit: int = 500) -> list[dict]:
    """One row per merchant-chosen uninstall, newest first.

    Only RELATIONSHIP_UNINSTALLED. app_events folds RELATIONSHIP_DEACTIVATED
    (Shopify closed or froze the store) into the same 'uninstalled' type, and
    those merchants never chose to leave and were never shown the survey;
    mixing them in would make churn look worse and reason coverage look thinner
    than they are. They get their own count via `store_deaths`.

    `installed_at` is the last install *before this uninstall*, not
    shops.installed_at, so a shop that installed twice reports each stay
    separately.

    `bucket` filters on the canonical reason bucket, and is applied in Python
    rather than in SQL because the buckets do not exist in SQL: the raw column
    holds the merchant's own words in their own admin language and is
    classified after the query. It is what the reason bars on Overview and on
    this page link to.
    """
    filters, params = [], []
    if paid == "yes":
        filters.append("paid_amount is not null")
    elif paid == "no":
        filters.append("paid_amount is null")
    if gave_reason == "yes":
        filters.append("reason is not null and reason <> ''")
    elif gave_reason == "no":
        filters.append("(reason is null or reason = '')")
    if since_days is not None:
        filters.append("occurred_at >= now() - make_interval(days => %s)")
        params.append(since_days)
    where = f"where {' and '.join(filters)}" if filters else ""

    rows = conn.execute(
        f"""
        with uninstalls as (
            select e.occurred_at,
                   e.uninstall_reason as reason,
                   e.uninstall_description as note,
                   coalesce(s.shop_name, s.shop_domain, e.shop_gid) as shop,
                   s.shop_domain, s.country,
                   (select max(i.occurred_at) from app_events i
                     where i.shop_gid = e.shop_gid
                       and i.type in ('installed', 'reinstalled')
                       and i.occurred_at <= e.occurred_at) as installed_at,
                   (select max(sub.monthly_amount) from subscriptions sub
                     where sub.shop_gid = e.shop_gid
                       and sub.converted_at <= e.occurred_at) as paid_amount
            from app_events e
            join raw_app_events r on r.id = e.platform_event_id
            left join shops s on s.shop_gid = e.shop_gid
            where e.type = 'uninstalled' and r.type = 'RELATIONSHIP_UNINSTALLED'
        )
        select * from uninstalls {where} order by occurred_at desc limit %s
        """,
        (*params, limit),
    ).fetchall()

    out = []
    for at, reason, note, shop, domain, country, installed_at, paid_amount in rows:
        days = (at - installed_at).days if installed_at else None
        buckets = [classify(r)[0] for r in split_reasons(reason)]
        if bucket and bucket not in buckets:
            continue
        out.append({
            "at": at, "shop": shop, "domain": domain, "country": country,
            "installed_at": installed_at, "days": days,
            "paid": paid_amount is not None, "monthly_amount": paid_amount,
            "buckets": buckets,
            "note": note,
        })
    return out


def uninstall_verbatims(conn: psycopg.Connection, limit: int = 60) -> list[dict]:
    """The free-text notes on their own, grouped by canonical reason bucket.

    Every one of these already appears in the "Every uninstall" table below, one
    per row. Reading them there means scanning a seven-column table of hundreds
    of rows and eye-matching the last column. This is the same data as a short
    read: a bucket bar says twelve merchants said "too expensive", and this says
    what those twelve actually wrote.

    Merchant-chosen uninstalls only, same as `churn_rows` and for the same
    reason: a store Shopify closed was never shown the question.
    """
    rows = conn.execute(
        """
        select e.occurred_at, e.uninstall_reason, e.uninstall_description,
               coalesce(s.shop_name, s.shop_domain, e.shop_gid), s.shop_domain
        from app_events e
        join raw_app_events r on r.id = e.platform_event_id
        left join shops s on s.shop_gid = e.shop_gid
        where e.type = 'uninstalled' and r.type = 'RELATIONSHIP_UNINSTALLED'
          and e.uninstall_description is not null
          and btrim(e.uninstall_description) <> ''
        order by e.occurred_at desc
        limit %s
        """,
        (limit,),
    ).fetchall()

    grouped: dict[str, list[dict]] = {}
    for at, reason, note, shop, domain in rows:
        # A merchant can pick several reasons; the note belongs to the whole
        # exit, so file it under the first one rather than duplicating it.
        buckets = [classify(r)[0] for r in split_reasons(reason)]
        label = buckets[0] if buckets else "No reason selected"
        grouped.setdefault(label, []).append(
            {"at": at, "note": note, "shop": shop, "domain": domain}
        )
    return [
        {"label": label, "notes": notes}
        for label, notes in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]


def review_candidates(conn: psycopg.Connection, min_days: int = 30) -> list[dict]:
    """Installed, paying, past the honeymoon, and not already a reviewer: the
    merchants worth asking for an App Store review. Longest-tenured first,
    because they have the most to say and the least reason to say something bad.

    `shops.reviewed_at` is hand-maintained from the public listing (migration
    007 explains why). A merchant who already reviewed and gets asked again
    learns that nobody is reading, so an unmaintained column is worse than the
    ask itself.

    Deliberately carries no contact details. The only source we ever had for
    them was a vendor export's "Contact N" columns, which list every staff account on
    the shop, agencies and our own team included; see migration 008.
    """
    rows = conn.execute(
        """
        select coalesce(s.shop_name, s.shop_domain, s.shop_gid) as shop,
               s.shop_domain, s.country,
               min(sub.converted_at) as paying_since,
               max(s.installed_at) as installed_at,
               sum(sub.monthly_amount) as mrr
        from subscriptions sub join shops s on s.shop_gid = sub.shop_gid
        where sub.churned_at is null and s.install_state = 'installed'
          and s.reviewed_at is null
        group by s.shop_gid, s.shop_name, s.shop_domain, s.country
        having min(sub.converted_at) <= now() - make_interval(days => %s)
        order by paying_since
        """,
        (min_days,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    return [
        {"shop": shop, "domain": domain, "country": country,
         "installed_at": installed_at, "since": since,
         "days": (now - since).days, "mrr": mrr}
        for shop, domain, country, since, installed_at, mrr in rows
    ]


def annual_upgrade_candidates(conn: psycopg.Connection, min_months: int = 3) -> list[dict]:
    """Monthly subscribers who have stuck around long enough to be worth pitching
    the annual plan, which converts a recurring monthly risk into a year of
    prepaid cash."""
    rows = conn.execute(
        """
        select coalesce(s.shop_name, s.shop_domain, s.shop_gid) as shop,
               s.shop_domain, s.country, sub.converted_at, sub.monthly_amount
        from subscriptions sub
        join charges c on c.gid = sub.id
        join shops s on s.shop_gid = sub.shop_gid
        where sub.churned_at is null
          and s.install_state = 'installed'
          and c.plan_interval = 'EVERY_30_DAYS'
          and sub.converted_at <= now() - make_interval(months => %s)
        order by sub.converted_at
        """,
        (min_months,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    return [
        {"shop": shop, "domain": domain, "country": country, "since": since,
         "days": (now - since).days, "mrr": mrr}
        for shop, domain, country, since, mrr in rows
    ]


def trial_watch(conn: psycopg.Connection, days: int = 14) -> list[dict]:
    """Recently installed, still not paying. Oldest first, because the ones
    closest to the end of the window are the ones about to be lost.

    A proxy for activation risk until real usage events exist: the Partner API
    has no product-usage data at all, so "installed and silent" is the only
    signal available today.
    """
    rows = conn.execute(
        """
        select coalesce(s.shop_name, s.shop_domain, s.shop_gid) as shop,
               s.shop_domain, s.country, s.installed_at
        from shops s
        where s.install_state = 'installed'
          and s.installed_at >= now() - make_interval(days => %s)
          and not exists (select 1 from subscriptions sub where sub.shop_gid = s.shop_gid)
        order by s.installed_at
        """,
        (days,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    return [
        {"shop": shop, "domain": domain, "country": country, "installed_at": at,
         "days": (now - at).days}
        for shop, domain, country, at in rows
    ]


def store_deaths(conn: psycopg.Connection, limit: int = 25) -> dict:
    """Stores Shopify closed or froze. Never surveyed, never a product decision,
    so they are counted apart from churn rather than folded into it."""
    rows = conn.execute(
        """
        select e.occurred_at, coalesce(s.shop_name, s.shop_domain, e.shop_gid)
        from app_events e
        join raw_app_events r on r.id = e.platform_event_id
        left join shops s on s.shop_gid = e.shop_gid
        where e.type = 'uninstalled' and r.type = 'RELATIONSHIP_DEACTIVATED'
        order by e.occurred_at desc
        """
    ).fetchall()
    return {
        "count": len(rows),
        "rows": [{"at": at, "shop": shop} for at, shop in rows[:limit]],
    }


def monthly_activity(conn: psycopg.Connection, months: int = 6) -> list[dict]:
    """Installs vs uninstalls per calendar month, oldest first."""
    rows = conn.execute(
        """
        select to_char(date_trunc('month', occurred_at), 'Mon YYYY') as label,
               date_trunc('month', occurred_at) as month,
               count(*) filter (where type in ('installed', 'reinstalled')) as installs,
               count(*) filter (where type = 'uninstalled') as uninstalls
        from app_events
        where occurred_at >= date_trunc('month', now()) - make_interval(months => %s - 1)
          and type in ('installed', 'reinstalled', 'uninstalled')
        group by 2, 1
        order by 2
        """,
        (months,),
    ).fetchall()
    return [
        {"label": label, "installs": installs, "uninstalls": uninstalls}
        for label, _, installs, uninstalls in rows
    ]


def recent_events(conn: psycopg.Connection, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        """
        select e.type, coalesce(s.shop_name, s.shop_domain, e.shop_gid), e.occurred_at
        from app_events e left join shops s on s.shop_gid = e.shop_gid
        order by e.occurred_at desc
        limit %s
        """,
        (limit,),
    ).fetchall()
    return [{"type": t, "shop": shop, "at": at} for t, shop, at in rows]


def funnel_stats(conn: psycopg.Connection) -> list[dict]:
    """All-time lifecycle funnel: ever installed -> ever subscribed ->
    currently installed -> currently paying."""
    def one(sql):
        return conn.execute(sql).fetchone()[0]

    stages = [
        ("Ever installed", one("select count(*) from shops")),
        ("Ever subscribed", one("select count(distinct shop_gid) from subscriptions")),
        ("Currently installed", one("select count(*) from shops where install_state = 'installed'")),
        ("Currently paying", one(
            """select count(distinct sub.shop_gid)
               from subscriptions sub join shops s on s.shop_gid = sub.shop_gid
               where sub.churned_at is null and s.install_state = 'installed'"""
        )),
    ]
    top = stages[0][1] or 1
    return [
        {"label": label, "count": count, "pct": round(100 * count / top)}
        for label, count in stages
    ]


def monthly_conversion(conn: psycopg.Connection, months: int = 6) -> list[dict]:
    """Per install-month: installs and how many of those shops ever subscribed."""
    rows = conn.execute(
        """
        with installs as (
            select shop_gid, min(occurred_at) as first_install
            from app_events where type = 'installed'
            group by shop_gid
        )
        select to_char(date_trunc('month', i.first_install), 'Mon YYYY'),
               date_trunc('month', i.first_install) as month,
               count(*) as installs,
               count(sub.shop_gid) as converted
        from installs i
        left join lateral (
            select shop_gid from subscriptions where shop_gid = i.shop_gid limit 1
        ) sub on true
        where i.first_install >= date_trunc('month', now()) - make_interval(months => %s - 1)
        group by 2, 1
        order by 2
        """,
        (months,),
    ).fetchall()
    return [
        {
            "label": label,
            "installs": installs,
            "converted": converted,
            "rate": round(100 * converted / installs) if installs else 0,
        }
        for label, _, installs, converted in rows
    ]


def retention_cohorts(conn: psycopg.Connection, max_offset: int = 8) -> dict:
    """Monthly subscription cohorts: % of each converted_at-month cohort still
    active N months after converting. Computed in Python; the table is small."""
    rows = conn.execute(
        "select converted_at, churned_at from subscriptions where converted_at is not null"
    ).fetchall()
    now = datetime.now(timezone.utc)
    this_month = now.year * 12 + (now.month - 1)

    cohorts: dict[int, dict] = {}
    for converted_at, churned_at in rows:
        cm = converted_at.year * 12 + (converted_at.month - 1)
        churn_offset = None
        if churned_at is not None:
            churn_offset = (churned_at.year * 12 + churned_at.month - 1) - cm
        cohorts.setdefault(cm, []).append(churn_offset)

    out = []
    for cm in sorted(cohorts):
        subs = cohorts[cm]
        observable = min(this_month - cm, max_offset)
        cells = []
        for offset in range(0, observable + 1):
            active = sum(1 for c in subs if c is None or c > offset)
            cells.append(round(100 * active / len(subs)))
        out.append({
            "label": f"{1 + (cm % 12):02d}/{cm // 12}",
            "size": len(subs),
            "cells": cells,
        })
    return {"cohorts": out, "max_offset": max_offset}


def install_retention_cohorts(conn: psycopg.Connection, max_offset: int = 8) -> dict:
    """Monthly install cohorts: % of each cohort still installed N months on.

    The leaky-bucket check. `retention_cohorts` only sees merchants who paid,
    which is a small and self-selected slice; this one covers everyone who ever
    installed, so it shows how much of the top of the funnel actually stays.

    A shop that uninstalled and came back counts as retained: its install_state
    is 'installed' again, and shops.uninstalled_at is only honoured when the
    shop is currently gone.
    """
    rows = conn.execute(
        """
        select i.first_install, s.install_state, s.uninstalled_at
        from (
            -- First install of any kind. A shop whose earliest event is a
            -- reactivation still belongs to that month's cohort; keying only on
            -- 'installed' would silently drop it from every total.
            select shop_gid, min(occurred_at) as first_install
            from app_events where type in ('installed', 'reinstalled')
            group by shop_gid
        ) i
        join shops s on s.shop_gid = i.shop_gid
        """
    ).fetchall()

    now = datetime.now(timezone.utc)
    this_month = _month_index(now)

    cohorts: dict[int, list] = {}
    for first_install, install_state, uninstalled_at in rows:
        cm = _month_index(first_install)
        gone = None
        if install_state == "uninstalled" and uninstalled_at is not None:
            gone = _month_index(uninstalled_at) - cm
        cohorts.setdefault(cm, []).append(gone)

    out = []
    for cm in sorted(cohorts):
        shops = cohorts[cm]
        observable = min(this_month - cm, max_offset)
        cells = [
            round(100 * sum(1 for g in shops if g is None or g > offset) / len(shops))
            for offset in range(0, observable + 1)
        ]
        out.append({
            "label": f"{1 + (cm % 12):02d}/{cm // 12}",
            "size": len(shops),
            "cells": cells,
        })
    return {"cohorts": out, "max_offset": max_offset}


def traffic_summary(conn, days: int = 90) -> dict:
    """Listing funnel over the window: sessions -> Add App clicks -> installs.

    Conversion is computed on the same window for every stage, so it is a rate
    over comparable traffic rather than two independent counts divided.
    """
    row = conn.execute(
        """
        select coalesce(sum(sessions), 0), coalesce(sum(users), 0),
               coalesce(sum(add_app_clicks), 0), coalesce(sum(installs), 0),
               coalesce(sum(ad_clicks), 0), min(date), max(date)
        from ga4_daily
        where dimension = 'total' and date >= current_date - %s
        """,
        (days,),
    ).fetchone()
    sessions, users, clicks, installs, ad_clicks, first, last = row
    return {
        "sessions": sessions, "users": users, "add_app_clicks": clicks,
        "installs": installs, "ad_clicks": ad_clicks,
        "first_date": first, "last_date": last,
        "click_rate": round(100 * clicks / sessions, 1) if sessions else 0.0,
        "install_rate": round(100 * installs / sessions, 1) if sessions else 0.0,
        "click_to_install": round(100 * installs / clicks, 1) if clicks else 0.0,
    }


def install_reconciliation(conn, days: int = 90) -> dict:
    """GA4's install count against the Partner API's, over the same window.

    Both numbers already live in this database and had never been compared. GA4
    counts `shopify_app_install`, a browser-side event on the listing page; the
    Partner API records the install itself. The Partner side is the truth, so
    any gap is GA4 undercounting -- consent banners, ad and tracking blockers,
    and EU traffic all suppress the event while the install still happens.

    Stating the gap turns "the funnel numbers look low" into a known quantity.
    Without it the listing conversion rate on this page reads as a product
    problem when it is a measurement one.
    """
    (ga4_installs,) = conn.execute(
        """select coalesce(sum(installs), 0) from ga4_daily
           where dimension = 'total' and date >= current_date - %s""",
        (days,),
    ).fetchone()
    (partner_installs,) = conn.execute(
        """select count(*) from app_events
           where type in ('installed', 'reinstalled')
             and occurred_at >= current_date - make_interval(days => %s)""",
        (days,),
    ).fetchone()

    gap = partner_installs - ga4_installs
    return {
        "days": days,
        "ga4_installs": ga4_installs,
        "partner_installs": partner_installs,
        "gap": gap,
        # Share of real installs GA4 never saw. Negative would mean GA4 counted
        # more than actually happened, which is possible (a click that fires the
        # event and then abandons the permissions screen) and worth not hiding.
        "missed_pct": round(100 * gap / partner_installs, 1) if partner_installs else 0.0,
    }


def traffic_monthly(conn, months: int = 12) -> list[dict]:
    rows = conn.execute(
        """
        select to_char(date_trunc('month', date), 'Mon YYYY') as label,
               date_trunc('month', date) as month,
               sum(sessions) as sessions, sum(installs) as installs
        from ga4_daily
        where dimension = 'total'
          and date >= date_trunc('month', current_date) - make_interval(months => %s - 1)
        group by 1, 2 order by 2
        """,
        (months,),
    ).fetchall()
    return [
        {"label": label, "sessions": sessions, "installs": installs,
         "rate": round(100 * installs / sessions, 1) if sessions else 0.0}
        for label, _, sessions, installs in rows
    ]


def traffic_breakdown(conn, dimension: str, days: int = 90, top: int = 10) -> list[dict]:
    """Top values for one GA4 dimension. `dimension` is whitelisted by the caller
    against partner_dashboard.ga4.DIMENSIONS, never interpolated from user input."""
    rows = conn.execute(
        """
        select value, sum(sessions) as sessions, sum(installs) as installs
        from ga4_daily
        where dimension = %s and date >= current_date - %s
        group by value having sum(sessions) > 0
        order by sessions desc limit %s
        """,
        (dimension, days, top),
    ).fetchall()
    return [
        {"value": value, "sessions": sessions, "installs": installs,
         "rate": round(100 * installs / sessions, 1) if sessions else 0.0}
        for value, sessions, installs in rows
    ]
