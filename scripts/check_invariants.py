#!/usr/bin/env python3
"""Run the data invariants against a live database. Read-only.

`tests/test_invariants.py` asserts these against a seeded fixture on every
pytest run. This runs the same questions against real data, which the fixture
cannot do: a live app carries years of feed quirks nobody thought to seed.

Usage, against whatever database you point it at:

    DATABASE_URL='postgres://...' uv run python scripts/check_invariants.py

If the database is not directly reachable, tunnel to it and point DATABASE_URL
at the local end of the tunnel.

Exits non-zero if any invariant fails, so it can gate a deploy.
"""

import os
import sys
from decimal import Decimal

import psycopg

from partner_dashboard import stats

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "", scope: int | None = None) -> None:
    """Report one invariant.

    `scope` is how many rows the check actually examined. A check that finds no
    violations because it had nothing to look at is not evidence of anything,
    and printing it as a bare PASS is how a misconfigured deployment gets a
    clean bill of health. The annual-plan check is the live example: with
    ANNUAL_PLAN_AMOUNTS unset, nothing is labelled ANNUAL, so it inspects zero
    rows and passes while every annual subscriber is counted at 12x.
    """
    label = "PASS" if ok else "FAIL"
    suffix = ""
    if scope is not None:
        suffix = f"  ({scope} rows in scope)" if scope else "  (0 rows in scope -- proves nothing)"
    print(f"{label}  {name}{suffix}")
    if not ok:
        if detail:
            print(f"      {detail}")
        FAILURES.append(name)


def rows(conn, sql):
    return conn.execute(sql).fetchall()


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    # Same TimeZone pin as partner_dashboard.db.connect: month buckets resolve in the
    # session's timezone, so checking under a different one would compare
    # different months to the ones the dashboard renders.
    conn = psycopg.connect(url, autocommit=True, options="-c TimeZone=UTC")

    tile = stats.overview_stats(conn)["active_mrr"]
    chart = stats.mrr_trend(conn)[-1]["mrr"]
    mix = sum((p["mrr"] for p in stats.plan_mix(conn)), Decimal("0"))
    check("Active MRR tile == last bucket of the MRR chart", tile == chart,
          f"tile {tile}, chart {chart}")
    check("Active MRR tile == sum of the plan mix", tile == mix,
          f"tile {tile}, mix {mix}")

    trend = stats.mrr_trend(conn)
    movements = stats.mrr_movements(conn)
    bad = []
    for i, m in enumerate(movements):
        parts = sum(m[k] for k in stats.MOVEMENT_KINDS)
        if parts != m["net"]:
            bad.append(f"{m['label']}: buckets {parts} != net {m['net']}")
        if i and parts != trend[i]["mrr"] - trend[i - 1]["mrr"]:
            bad.append(f"{m['label']}: waterfall {parts} != trend step "
                       f"{trend[i]['mrr'] - trend[i - 1]['mrr']}")
    check("Movement buckets decompose the trend line exactly", not bad, "; ".join(bad))

    s = stats.overview_stats(conn)
    funnel = next(f for f in stats.funnel_stats(conn) if f["label"] == "Currently paying")
    check("Paying-shop count agrees across every path",
          s["paying"] == stats.unit_economics(conn)["paying"] == funnel["count"])

    check("No shop has two simultaneously-live subscriptions",
          not rows(conn, """select shop_gid from subscriptions where churned_at is null
                            group by shop_gid having count(*) > 1"""))

    check("No uninstalled shop has a live subscription",
          not rows(conn, """select sub.id from subscriptions sub
                            join shops s on s.shop_gid = sub.shop_gid
                            where sub.churned_at is null and s.install_state <> 'installed'"""))

    check("No subscription churns before it converts",
          not rows(conn, """select id from subscriptions
                            where churned_at is not null and churned_at < converted_at"""))

    # Not "never null": an expiry whose activation predates the Partner API's
    # retention window has no conversion to record, so such rows legitimately
    # exist. The rule is that they must be inert -- no amount, already churned.
    # A *live*
    # subscription without a converted_at is the bug, because it counts toward
    # the Active MRR tile while being invisible to the chart.
    check("A subscription without a converted_at is inert (no amount, churned)",
          not rows(conn, """select id from subscriptions where converted_at is null
                            and (churned_at is null or coalesce(monthly_amount, 0) <> 0)"""))

    check("install_state matches each shop's last lifecycle event",
          not rows(conn, """
            select s.shop_gid from shops s
            join lateral (
                select type from app_events e
                where e.shop_gid = s.shop_gid
                  and e.type in ('installed', 'reinstalled', 'uninstalled')
                order by e.occurred_at desc, e.id desc limit 1
            ) last on true
            where s.install_state <> case when last.type = 'uninstalled'
                                          then 'uninstalled' else 'installed' end"""))

    check("No test charge contributes to any figure",
          not rows(conn, """select sub.id from subscriptions sub
                            join charges c on c.gid = sub.id where c.test"""))

    # Scope is reported because this check is silently disarmed by the exact
    # misconfiguration it exists to catch. ANNUAL_PLAN_AMOUNTS is what labels a
    # charge ANNUAL; leave it empty and there are no annual rows to disagree
    # with, so this passes over nothing while MRR reads twelve times high.
    annual_scope = len(rows(conn, """select sub.id from subscriptions sub
                                     join charges c on c.gid = sub.id
                                     where c.plan_interval = 'ANNUAL'"""))
    check("Every annual subscription counts at one twelfth of its price",
          not rows(conn, """select sub.id from subscriptions sub
                            join charges c on c.gid = sub.id
                            where c.plan_interval = 'ANNUAL'
                              and sub.monthly_amount
                                  <> round(coalesce(c.plan_amount, c.amount) / 12, 2)"""),
          detail="", scope=annual_scope)

    money = stats.collected_revenue(conn)
    check("Collected revenue: gross - taken == net",
          money["gross"] - money["taken"] == money["net"])

    check("No orphaned shop_gid in subscriptions or app_events",
          not rows(conn, """
            select 'subscriptions' from subscriptions t
             where not exists (select 1 from shops s where s.shop_gid = t.shop_gid)
            union all
            select 'app_events' from app_events t
             where not exists (select 1 from shops s where s.shop_gid = t.shop_gid)"""))

    check("Every app_event traces back to a raw event",
          not rows(conn, """select e.id from app_events e where not exists
                            (select 1 from raw_app_events r where r.id = e.platform_event_id)"""))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} invariant(s) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All invariants hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
