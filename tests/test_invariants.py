"""Things that must be true of any consistent dataset, not golden numbers.

Every figure on this dashboard is computed by more than one code path -- the
Active MRR tile, the MRR chart and the plan-mix table each write their own SQL
over the same rows. When those paths disagree, a human notices two tiles
contradicting each other on the same screen, which is how the scalar-MRR bug was
found. These assertions are that comparison, run on every `pytest` instead of by
eye.

They are deliberately written against a seeded *world* rather than fixed
expected values, so they keep holding as the pipeline changes. `scripts/
check_invariants.py` runs the same checks against production.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from partner_dashboard import stats
from partner_dashboard.derive import derive_installation
from partner_dashboard.ingest_raw import upsert_raw_events

NOW = datetime.now(timezone.utc)


def months_ago(n: int, day_offset: int = 0) -> str:
    """An instant n whole months back, safely inside the month.

    Anchored to day 15 so a month-length difference can never push the value
    into a neighbouring month, which would silently move a row between buckets
    and make an invariant fail for a calendar reason rather than a real one.
    """
    m = NOW.year * 12 + NOW.month - 1 - n
    at = datetime(m // 12, m % 12 + 1, 15, 12, 0, tzinfo=timezone.utc)
    return (at + timedelta(days=day_offset)).isoformat()


def _charge(db, gid, amount, interval="EVERY_30_DAYS", test=False):
    db.execute(
        """insert into charges(gid, amount, currency_code, subscription_id,
                               plan_interval, plan_amount, flex_billing, test)
           values (%s, %s, 'USD', %s, %s, %s, false, %s)
           on conflict (gid) do nothing""",
        (gid, amount, gid, interval, amount, test),
    )


def _ev(id, type, shop, at, charge=None, **payload):
    return dict(id=id, type=type, occurred_at=at, shop_gid=shop,
                charge_gid=charge, payload=payload)


@pytest.fixture
def world(db):
    """One of every lifecycle shape the pipeline has to survive.

    Includes the awkward ones on purpose: a plan change (two subscription ids
    for one shop), a win-back, a store that uninstalled without Shopify ever
    sending a cancel, and a test charge that must contribute nothing.
    """
    for gid, amount, interval in [
        ("c-monthly", "19.00", "EVERY_30_DAYS"),
        ("c-annual", "190.00", "ANNUAL"),
        ("c-up-old", "19.00", "EVERY_30_DAYS"),
        ("c-up-new", "49.00", "EVERY_30_DAYS"),
        ("c-churned", "19.00", "EVERY_30_DAYS"),
        ("c-react-1", "19.00", "EVERY_30_DAYS"),
        ("c-react-2", "29.00", "EVERY_30_DAYS"),
        ("c-gone", "19.00", "EVERY_30_DAYS"),
        ("c-nocancel", "19.00", "EVERY_30_DAYS"),
        ("c-orphan", "19.00", "EVERY_30_DAYS"),
    ]:
        _charge(db, gid, Decimal(amount), interval)
    _charge(db, "c-test", Decimal("19.00"), test=True)
    db.commit()

    events = [
        # Plain monthly subscriber, still paying.
        _ev("e1", "RELATIONSHIP_INSTALLED", "sh-monthly", months_ago(5)),
        _ev("e2", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-monthly", months_ago(5, 1), "c-monthly"),
        # Annual subscriber: must count at one twelfth per month.
        _ev("e3", "RELATIONSHIP_INSTALLED", "sh-annual", months_ago(4)),
        _ev("e4", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-annual", months_ago(4, 1), "c-annual"),
        # Plan change: Shopify mints a new subscription and cancels the old one.
        _ev("e5", "RELATIONSHIP_INSTALLED", "sh-upgrade", months_ago(4)),
        _ev("e6", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-upgrade", months_ago(4, 1), "c-up-old"),
        _ev("e7", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-upgrade", months_ago(2), "c-up-new"),
        _ev("e8", "SUBSCRIPTION_CHARGE_CANCELED", "sh-upgrade", months_ago(2, 1), "c-up-old"),
        # Cancelled last month, still installed.
        _ev("e9", "RELATIONSHIP_INSTALLED", "sh-churned", months_ago(6)),
        _ev("e10", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-churned", months_ago(6, 1), "c-churned"),
        _ev("e11", "SUBSCRIPTION_CHARGE_CANCELED", "sh-churned", months_ago(1), "c-churned"),
        # Paid, left, came back: a reactivation rather than a new customer.
        _ev("e12", "RELATIONSHIP_INSTALLED", "sh-react", months_ago(6)),
        _ev("e13", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-react", months_ago(6, 1), "c-react-1"),
        _ev("e14", "SUBSCRIPTION_CHARGE_CANCELED", "sh-react", months_ago(4), "c-react-1"),
        _ev("e15", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-react", months_ago(2), "c-react-2"),
        # Cancelled, then uninstalled. The ordinary exit.
        _ev("e16", "RELATIONSHIP_INSTALLED", "sh-gone", months_ago(5)),
        _ev("e17", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-gone", months_ago(5, 1), "c-gone"),
        _ev("e18", "SUBSCRIPTION_CHARGE_CANCELED", "sh-gone", months_ago(3), "c-gone"),
        _ev("e19", "RELATIONSHIP_UNINSTALLED", "sh-gone", months_ago(3, 1),
            reason="too_expensive"),
        # Uninstalled with NO cancel event. Shopify does not guarantee one, and
        # the paying-shop figures are computed by paths that disagree about
        # whether install_state is part of the question.
        _ev("e20", "RELATIONSHIP_INSTALLED", "sh-nocancel", months_ago(5)),
        _ev("e21", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-nocancel", months_ago(5, 1), "c-nocancel"),
        _ev("e22", "RELATIONSHIP_UNINSTALLED", "sh-nocancel", months_ago(2)),
        # Installed, never paid.
        _ev("e23", "RELATIONSHIP_INSTALLED", "sh-trial", months_ago(0)),
        # An expiry whose activation predates the ingest window, so there is no
        # conversion to record. These legitimately exist; they must stay
        # inert rather than being counted or crashing anything.
        _ev("e26", "RELATIONSHIP_INSTALLED", "sh-orphan-expiry", months_ago(6)),
        _ev("e27", "SUBSCRIPTION_CHARGE_EXPIRED", "sh-orphan-expiry", months_ago(3),
            "c-orphan"),
        # Test charge: contributes to nothing.
        _ev("e24", "RELATIONSHIP_INSTALLED", "sh-test", months_ago(3)),
        _ev("e25", "SUBSCRIPTION_CHARGE_ACTIVATED", "sh-test", months_ago(3, 1), "c-test"),
    ]
    upsert_raw_events(db, events)
    for shop in {e["shop_gid"] for e in events}:
        derive_installation(db, shop)

    # Money that actually moved, including one refund. Deliberately not derived
    # from the events above: transactions are an independent feed, which is the
    # whole reason collected revenue and MRR are separate numbers.
    for tid, type, at, gross, net in [
        ("t1", "AppSubscriptionSale", months_ago(2), "19.00", "18.45"),
        ("t2", "AppSubscriptionSale", months_ago(1), "19.00", "18.45"),
        ("t3", "AppSubscriptionSale", months_ago(1, 2), "190.00", "184.49"),
        ("t4", "AppSaleAdjustment", months_ago(1, 3), "-19.00", "-18.45"),
        ("t5", "AppSubscriptionSale", months_ago(0), "49.00", "47.58"),
    ]:
        db.execute(
            """insert into transactions(id, type, created_at, gross_amount,
                                        shopify_fee, net_amount, currency_code)
               values (%s, %s, %s, %s, 0, %s, 'USD')""",
            (tid, type, at, gross, net),
        )
    db.commit()
    return db


# --- MRR: three code paths, one truth ------------------------------------


def test_active_mrr_tile_matches_the_mrr_chart_and_the_plan_mix(world):
    """The disagreement that exposed the scalar-MRR bug: the Overview tile, the
    last bucket of the 12-month chart, and the plan-mix table are three separate
    queries over the same subscriptions and must land on the same number."""
    tile = stats.overview_stats(world)["active_mrr"]
    chart = stats.mrr_trend(world)[-1]["mrr"]
    mix = sum((p["mrr"] for p in stats.plan_mix(world)), Decimal("0"))

    assert tile == chart, "Active MRR tile disagrees with the MRR chart"
    assert tile == mix, "Active MRR tile disagrees with the plan mix"


def test_mrr_movement_buckets_sum_to_the_step_in_the_trend_line(world):
    """The waterfall claims to decompose the line above it. Assert that, rather
    than captioning it."""
    trend = stats.mrr_trend(world)
    movements = stats.mrr_movements(world)
    assert len(movements) == len(trend)

    for i, m in enumerate(movements):
        parts = sum(m[k] for k in stats.MOVEMENT_KINDS)
        assert parts == m["net"], f"{m['label']}: buckets do not sum to their own net"
        if i:
            step = trend[i]["mrr"] - trend[i - 1]["mrr"]
            assert parts == step, (
                f"{m['label']}: waterfall says {parts}, the trend line stepped {step}"
            )


def test_paying_shop_count_agrees_across_every_path_that_reports_it(world):
    tile = stats.overview_stats(world)["paying"]
    economics = stats.unit_economics(world)["paying"]
    funnel = next(s for s in stats.funnel_stats(world) if s["label"] == "Currently paying")
    live = world.execute(
        """select count(distinct sub.shop_gid) from subscriptions sub
           join shops s on s.shop_gid = sub.shop_gid
           where sub.churned_at is null and s.install_state = 'installed'"""
    ).fetchone()[0]

    assert tile == economics == funnel["count"] == live


def test_arpu_is_mrr_over_paying_shops(world):
    s = stats.overview_stats(world)
    assert s["arpu"] * s["paying"] == s["active_mrr"]


# --- Subscription state --------------------------------------------------


def test_no_shop_has_two_simultaneously_live_subscriptions(world):
    """Possible only since `live` became a dict keyed on subscription id. A plan
    change leaves both ids live for the hour between them, but never past the
    trailing cancel."""
    rows = world.execute(
        """select shop_gid, count(*) from subscriptions
           where churned_at is null group by shop_gid having count(*) > 1"""
    ).fetchall()
    assert rows == []


def test_an_uninstalled_shop_has_no_live_subscription(world):
    """Otherwise the Active MRR tile (which filters on install_state) and the MRR
    chart (which does not) drift apart the moment Shopify sends an uninstall
    without a matching cancel."""
    rows = world.execute(
        """select sub.id from subscriptions sub join shops s on s.shop_gid = sub.shop_gid
           where sub.churned_at is null and s.install_state <> 'installed'"""
    ).fetchall()
    assert rows == []


def test_a_subscription_never_churns_before_it_converts(world):
    rows = world.execute(
        "select id from subscriptions where churned_at is not null and churned_at < converted_at"
    ).fetchall()
    assert rows == []


def test_a_subscription_without_a_converted_at_is_an_inert_tombstone(world):
    """A subscription with a null converted_at is invisible to mrr_trend,
    mrr_movements, retention_cohorts and paying_at while still counting toward
    the Active MRR tile -- exactly the gap that made the chart read below the
    tile. But the null itself is legitimate for one shape: an expiry whose
    activation predates the Partner API's retention window, so there is no
    conversion to record, so such rows legitimately exist.

    So the rule is not "never null", it is that such a row must be inert: no
    amount, and already churned. A *live* subscription without a converted_at is
    the bug."""
    bad = world.execute(
        """select id, monthly_amount, churned_at from subscriptions
           where converted_at is null
             and (churned_at is null or coalesce(monthly_amount, 0) <> 0)"""
    ).fetchall()
    assert bad == [], "a subscription with no converted_at is carrying money or is live"

    # And the fixture really does contain one, so the check above is exercised
    # rather than passing on an empty table.
    assert world.execute(
        "select count(*) from subscriptions where converted_at is null"
    ).fetchone()[0] == 1


def test_replaying_the_whole_history_changes_nothing(world):
    """derive_installation is called again for every shop touched by a sync, so
    a second replay of identical input must be a no-op in every table that feeds
    a number."""
    before = (
        stats.overview_stats(world),
        stats.mrr_trend(world),
        world.execute("select count(*) from app_events").fetchone()[0],
        world.execute(
            "select id, monthly_amount, converted_at, churned_at from subscriptions order by id"
        ).fetchall(),
    )
    for shop in [r[0] for r in world.execute("select shop_gid from shops").fetchall()]:
        derive_installation(world, shop)
    after = (
        stats.overview_stats(world),
        stats.mrr_trend(world),
        world.execute("select count(*) from app_events").fetchone()[0],
        world.execute(
            "select id, monthly_amount, converted_at, churned_at from subscriptions order by id"
        ).fetchall(),
    )
    assert before == after


def test_install_state_matches_the_last_lifecycle_event(world):
    """The header-vs-timeline contradiction: a shop labelled installed whose most
    recent event is an uninstall."""
    rows = world.execute(
        """
        select s.shop_gid, s.install_state, last.type
        from shops s
        join lateral (
            select type from app_events e
            where e.shop_gid = s.shop_gid
              and e.type in ('installed', 'reinstalled', 'uninstalled')
            order by e.occurred_at desc, e.id desc limit 1
        ) last on true
        """
    ).fetchall()
    assert rows, "fixture should have lifecycle events"
    for shop_gid, state, last_type in rows:
        expected = "uninstalled" if last_type == "uninstalled" else "installed"
        assert state == expected, f"{shop_gid}: state {state!r} after a {last_type!r} event"


# --- Money ---------------------------------------------------------------


def test_annual_plans_count_at_one_twelfth(world):
    """An annual plan counts as one twelfth of its price. Counting it at the full
    yearly amount is the classic 12x MRR overstatement."""
    monthly = world.execute(
        "select monthly_amount from subscriptions where id = 'c-annual'"
    ).fetchone()[0]
    assert monthly == Decimal("15.83")

    annual = next(p for p in stats.plan_mix(world) if "Annual" in p["label"])
    assert annual["mrr"] == Decimal("15.83")


def test_test_charges_contribute_to_nothing(world):
    assert world.execute(
        "select count(*) from subscriptions sub join charges c on c.gid = sub.id where c.test"
    ).fetchone()[0] == 0
    assert world.execute(
        "select count(*) from subscriptions where id = 'c-test'"
    ).fetchone()[0] == 0


def test_collected_revenue_is_internally_consistent(world):
    money = stats.collected_revenue(world)
    assert money["gross"] - money["net"] == money["taken"]
    assert money["count"] == world.execute("select count(*) from transactions").fetchone()[0]


def test_revenue_by_month_sums_to_the_totals_over_the_same_window(world):
    """Two aggregates over one transactions table. The fixture's transactions all
    fall inside the 12-month window, so the monthly series must add back up."""
    money = stats.collected_revenue(world)
    monthly = stats.revenue_by_month(world)
    assert sum(m["gross"] for m in monthly) == money["gross"]
    assert sum(m["net"] for m in monthly) == money["net"]
    assert sum(m["charges"] for m in monthly) == money["count"]


def test_refunds_are_counted_once_and_are_already_inside_gross(world):
    money = stats.collected_revenue(world)
    assert money["refund_count"] == 1
    assert money["refunded"] == Decimal("19.00")
    # Reported gross is net of the refund, not gross plus it.
    assert money["gross"] == Decimal("258.00")  # 19 + 19 + 190 + 49 - 19


# --- Referential integrity ------------------------------------------------


def test_no_orphaned_shop_gids(world):
    for table in ("subscriptions", "app_events"):
        orphans = world.execute(
            f"""select count(*) from {table} t
                where not exists (select 1 from shops s where s.shop_gid = t.shop_gid)"""
        ).fetchone()[0]
        assert orphans == 0, f"{table} references a shop that does not exist"


def test_every_app_event_traces_back_to_a_raw_event(world):
    orphans = world.execute(
        """select count(*) from app_events e
           where not exists (select 1 from raw_app_events r where r.id = e.platform_event_id)"""
    ).fetchone()[0]
    assert orphans == 0
