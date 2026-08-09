"""stats.py computes the numbers on the dashboard, so it gets direct coverage
rather than being exercised only through page renders against an empty DB."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from partner_dashboard.stats import (
    COMPARED,
    collected_revenue,
    country_breakdown,
    churn_composition,
    annual_upgrade_candidates,
    churn_rows,
    install_reconciliation,
    installed_at_time,
    overview_comparison,
    install_retention_cohorts,
    revenue_by_month,
    review_candidates,
    store_deaths,
    trial_watch,
    mrr_movements,
    mrr_trend,
    overview_stats,
    plan_mix,
    time_to_uninstall,
    uninstall_reasons,
    uninstall_verbatims,
    unit_economics,
)


def _shop(db, gid, **kw):
    cols = {"shop_gid": gid, "install_state": "installed"}
    cols.update(kw)
    names = ", ".join(cols)
    holes = ", ".join(["%s"] * len(cols))
    db.execute(f"insert into shops ({names}) values ({holes})", list(cols.values()))


def _sub(db, sub_id, gid, monthly, converted_at, churned_at=None):
    db.execute(
        "insert into subscriptions (id, shop_gid, monthly_amount, converted_at, churned_at) "
        "values (%s, %s, %s, %s, %s)",
        (sub_id, gid, monthly, converted_at, churned_at),
    )


def _uninstall_event(db, gid, at, reason=None, description=None,
                     raw_type="RELATIONSHIP_UNINSTALLED"):
    # The reason report joins back to raw_app_events to tell a real uninstall
    # from a deactivation, so both rows have to exist.
    event_id = f"e-{gid}-{at}"
    db.execute(
        "insert into raw_app_events (id, type, occurred_at, shop_gid, payload) "
        "values (%s, %s, %s, %s, '{}')",
        (event_id, raw_type, at, gid),
    )
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid, "
        "uninstall_reason, uninstall_description) values (%s, 'uninstalled', %s, %s, %s, %s)",
        (event_id, at, gid, reason, description),
    )


def test_overview_arpu_and_churn(db):
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3", install_state="uninstalled")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z")
    _sub(db, "c2", "s2", Decimal("15.83"), "2026-01-01Z")
    _uninstall_event(db, "s3", "2026-08-01T00:00:00Z")
    db.commit()

    stats = overview_stats(db)
    assert stats["installed"] == 2
    assert stats["active_mrr"] == Decimal("34.83")
    assert stats["paying"] == 2
    assert stats["arpu"] == Decimal("17.415")
    # 1 uninstall against 2 still installed + 1 that left
    assert stats["churn_30d"] == 33.3


def test_mrr_trend_counts_a_sub_only_while_it_lived(db):
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-15Z", churned_at="2026-03-10Z")
    db.commit()
    by_label = {m["label"]: m["mrr"] for m in mrr_trend(db, months=12)}
    assert by_label["Feb 2026"] == Decimal("19.00")   # alive for the whole month
    assert by_label["Mar 2026"] == Decimal("0")       # churned mid-month, gone by month end


def test_mrr_movements_splits_new_churn_and_upgrades(db):
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3")
    # New in February.
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-02-10Z")
    # Subscribed in January, gone in March.
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-01-10Z", churned_at="2026-03-05Z")
    # Monthly in January, swapped to annual in April: an upgrade, not a new sub.
    _sub(db, "c3a", "s3", Decimal("19.00"), "2026-01-10Z", churned_at="2026-04-02Z")
    _sub(db, "c3b", "s3", Decimal("25.00"), "2026-04-02Z")
    db.commit()

    by_label = {m["label"]: m for m in mrr_movements(db, months=12)}
    assert by_label["Jan 2026"]["new"] == Decimal("38.00")     # s2 + s3
    assert by_label["Feb 2026"]["new"] == Decimal("19.00")     # s1
    assert by_label["Mar 2026"]["churned"] == Decimal("-19.00")
    assert by_label["Apr 2026"]["expansion"] == Decimal("6.00")
    assert by_label["Apr 2026"]["new"] == Decimal("0")
    assert by_label["Apr 2026"]["net"] == Decimal("6.00")


def test_mrr_movements_calls_a_returning_payer_a_reactivation(db):
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-10Z", churned_at="2026-02-05Z")
    _sub(db, "c2", "s1", Decimal("19.00"), "2026-05-10Z")
    db.commit()
    by_label = {m["label"]: m for m in mrr_movements(db, months=12)}
    assert by_label["May 2026"]["reactivation"] == Decimal("19.00")
    assert by_label["May 2026"]["new"] == Decimal("0")


def test_mrr_movements_reconcile_with_the_trend_line(db):
    """The waterfall sits under the MRR trend on Overview. If the buckets did not
    sum to the trend's month-over-month change, the two charts would contradict
    each other on the same screen."""
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-10Z")
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-02-10Z", churned_at="2026-06-05Z")
    _sub(db, "c3a", "s3", Decimal("19.00"), "2026-03-10Z", churned_at="2026-04-02Z")
    _sub(db, "c3b", "s3", Decimal("15.83"), "2026-04-02Z")
    db.commit()

    trend = mrr_trend(db, months=12)
    moves = {m["label"]: m for m in mrr_movements(db, months=12)}
    for prev, curr in zip(trend, trend[1:]):
        assert moves[curr["label"]]["net"] == curr["mrr"] - prev["mrr"], curr["label"]


def test_country_breakdown_splits_live_from_all_time(db):
    _shop(db, "s1", country="US")
    _shop(db, "s2", country="US", install_state="uninstalled")
    _shop(db, "s3", country="GB")
    db.commit()
    rows = {r["country"]: r for r in country_breakdown(db)}
    assert rows["US"]["installed"] == 1 and rows["US"]["ever"] == 2
    assert rows["GB"]["installed"] == 1 and rows["GB"]["ever"] == 1


def test_country_breakdown_folds_the_tail_into_other(db):
    for i in range(12):
        _shop(db, f"s{i}", country=f"C{i:02d}")
    db.commit()
    rows = country_breakdown(db, top=10)
    assert len(rows) == 11
    assert rows[-1]["country"] == "Other (2)"
    assert rows[-1]["installed"] == 2


def test_plan_mix_labels_intervals(db):
    _shop(db, "s1")
    _shop(db, "s2")
    for gid, amount, interval in (("c1", "19.00", "EVERY_30_DAYS"), ("c2", "190.00", "ANNUAL")):
        db.execute(
            "insert into charges (gid, amount, currency_code, subscription_id, plan_interval, "
            "plan_amount) values (%s, %s, 'USD', %s, %s, %s)",
            (gid, amount, gid, interval, amount),
        )
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z")
    _sub(db, "c2", "s2", Decimal("15.83"), "2026-01-01Z")
    db.commit()
    labels = {p["label"]: p for p in plan_mix(db)}
    assert labels["Monthly"]["count"] == 1
    assert labels["Annual"]["mrr"] == Decimal("15.83")


def test_uninstall_reasons_group_across_languages(db):
    _shop(db, "s1", install_state="uninstalled")
    _shop(db, "s2", install_state="uninstalled")
    _shop(db, "s3", install_state="uninstalled")
    _uninstall_event(db, "s1", "2026-07-01T00:00:00Z", reason="Not using app now")
    # Same reason, German admin. Must land in the same bucket, not its own bar.
    _uninstall_event(db, "s2", "2026-07-02T00:00:00Z", reason="App wird derzeit nicht genutzt")
    _uninstall_event(db, "s3", "2026-07-03T00:00:00Z")   # no reason given
    db.commit()

    out = uninstall_reasons(db)
    assert out["total"] == 3
    assert out["with_reason"] == 2
    assert out["coverage_pct"] == 66.7
    assert out["buckets"][0] == {"label": "Not using app now", "count": 2, "pct": 100}
    assert {l["lang"] for l in out["languages"]} == {"en", "de"}


def test_reason_buckets_count_the_mandatory_era_only(db):
    """Shopify made the exit question mandatory partway through 2026. Pooling
    the eras averages a self-selected minority with a near-census, which is how
    the dashboard came to claim the question was optional at 45% coverage."""
    _shop(db, "s1", install_state="uninstalled")
    _shop(db, "s2", install_state="uninstalled")
    _shop(db, "s3", install_state="uninstalled")
    # Optional era: one answer, one silence.
    _uninstall_event(db, "s1", "2026-01-10T00:00:00Z", reason="Too expensive")
    _uninstall_event(db, "s2", "2026-02-10T00:00:00Z")
    # Mandatory era.
    _uninstall_event(db, "s3", "2026-07-01T00:00:00Z", reason="Not using app now")
    db.commit()

    out = uninstall_reasons(db)
    assert out["mandatory_from"] == "2026-04-29"
    assert out["era"]["pre"] == {"total": 2, "with_reason": 1, "coverage_pct": 50.0}
    assert out["era"]["post"] == {"total": 1, "with_reason": 1, "coverage_pct": 100.0}
    # "Too expensive" is pre-cutover, so it must not appear in the bars.
    assert [b["label"] for b in out["buckets"]] == ["Not using app now"]
    # All-time figures survive for anything that wants the whole feed.
    assert (out["total"], out["with_reason"]) == (3, 2)
    # Language is a property of the merchant base, not of the survey rules, so
    # it keeps every answer ever given.
    assert {l["lang"] for l in out["languages"]} == {"en"}
    assert sum(l["count"] for l in out["languages"]) == 2


def test_deactivations_are_left_out_of_the_reason_denominator(db):
    _shop(db, "s1", install_state="uninstalled")
    _shop(db, "s2", install_state="uninstalled")
    _uninstall_event(db, "s1", "2026-07-01T00:00:00Z", reason="Too expensive")
    # Shopify froze or closed this store; the merchant was never asked why.
    _uninstall_event(db, "s2", "2026-07-02T00:00:00Z", raw_type="RELATIONSHIP_DEACTIVATED")
    db.commit()
    out = uninstall_reasons(db)
    assert out["total"] == 1
    assert out["coverage_pct"] == 100


def test_multi_reason_uninstall_counts_in_every_bucket(db):
    _shop(db, "s1", install_state="uninstalled")
    _uninstall_event(db, "s1", "2026-07-01T00:00:00Z",
                     reason="Too expensive, Testing multiple apps")
    db.commit()
    out = uninstall_reasons(db)
    assert out["with_reason"] == 1
    assert {b["label"] for b in out["buckets"]} == {"Too expensive", "Testing multiple apps"}


def test_time_to_uninstall_median_and_buckets(db):
    _shop(db, "s1", install_state="uninstalled",
          installed_at="2026-01-01Z", uninstalled_at="2026-01-01Z")       # same day
    _shop(db, "s2", install_state="uninstalled",
          installed_at="2026-01-01Z", uninstalled_at="2026-01-05Z")       # 4 days
    _shop(db, "s3", install_state="uninstalled",
          installed_at="2026-01-01Z", uninstalled_at="2026-03-01Z")       # 59 days
    db.commit()
    out = time_to_uninstall(db)
    assert out["count"] == 3
    assert out["median"] == 4.0
    counts = {b["label"]: b["count"] for b in out["buckets"]}
    assert counts["Same day"] == 1 and counts["1-7 days"] == 1 and counts["31-90 days"] == 1


def test_churn_composition_separates_payers_from_tourists(db):
    _shop(db, "s1", install_state="uninstalled")
    _shop(db, "s2", install_state="uninstalled")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z", churned_at="2026-02-01Z")
    db.commit()
    out = {c["label"]: c["count"] for c in churn_composition(db)}
    assert out == {"Had a subscription": 1, "Never subscribed": 1}


def _install_event(db, gid, at):
    event_id = f"i-{gid}-{at}"
    db.execute(
        "insert into raw_app_events (id, type, occurred_at, shop_gid, payload) "
        "values (%s, 'RELATIONSHIP_INSTALLED', %s, %s, '{}')", (event_id, at, gid))
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid) "
        "values (%s, 'installed', %s, %s)", (event_id, at, gid))


def test_churn_rows_counts_every_real_uninstall_and_no_deactivations(db):
    _shop(db, "s1", shop_name="Left Shop", install_state="uninstalled")
    _shop(db, "s2", shop_name="Dead Store", install_state="uninstalled")
    _install_event(db, "s1", "2026-05-01T00:00:00Z")
    _uninstall_event(db, "s1", "2026-06-10T00:00:00Z", reason="Too expensive",
                     description="not worth it")
    _install_event(db, "s2", "2026-05-01T00:00:00Z")
    _uninstall_event(db, "s2", "2026-06-11T00:00:00Z",
                     raw_type="RELATIONSHIP_DEACTIVATED")
    db.commit()

    rows = churn_rows(db)
    assert [r["shop"] for r in rows] == ["Left Shop"]
    assert rows[0]["days"] == 40
    assert rows[0]["buckets"] == ["Too expensive"]
    assert rows[0]["note"] == "not worth it"
    assert rows[0]["paid"] is False

    deaths = store_deaths(db)
    assert deaths["count"] == 1 and deaths["rows"][0]["shop"] == "Dead Store"


def test_churn_rows_flag_shops_that_paid(db):
    _shop(db, "s1", shop_name="Payer", install_state="uninstalled")
    _shop(db, "s2", shop_name="Tourist", install_state="uninstalled")
    _install_event(db, "s1", "2026-01-01T00:00:00Z")
    _install_event(db, "s2", "2026-01-01T00:00:00Z")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-02-01Z", churned_at="2026-06-01Z")
    _uninstall_event(db, "s1", "2026-06-01T00:00:00Z")
    _uninstall_event(db, "s2", "2026-06-02T00:00:00Z")
    db.commit()

    by_shop = {r["shop"]: r for r in churn_rows(db)}
    assert by_shop["Payer"]["paid"] is True
    assert by_shop["Payer"]["monthly_amount"] == Decimal("19.00")
    assert by_shop["Tourist"]["paid"] is False
    assert [r["shop"] for r in churn_rows(db, paid="yes")] == ["Payer"]
    assert [r["shop"] for r in churn_rows(db, paid="no")] == ["Tourist"]


def test_churn_rows_filter_on_whether_a_reason_was_given(db):
    _shop(db, "s1", shop_name="Talker", install_state="uninstalled")
    _shop(db, "s2", shop_name="Silent", install_state="uninstalled")
    _uninstall_event(db, "s1", "2026-06-01T00:00:00Z", reason="Not using app now")
    _uninstall_event(db, "s2", "2026-06-02T00:00:00Z")
    db.commit()
    assert [r["shop"] for r in churn_rows(db, gave_reason="yes")] == ["Talker"]
    assert [r["shop"] for r in churn_rows(db, gave_reason="no")] == ["Silent"]
    # An unrecognised filter value must not silently drop or inject anything.
    assert len(churn_rows(db, paid="'; drop table shops; --")) == 2


def test_churn_rows_measure_the_stay_that_ended_not_the_first_install(db):
    """A shop that installed, left, came back, and left again reports two stays."""
    _shop(db, "s1", shop_name="Repeat", install_state="uninstalled")
    _install_event(db, "s1", "2026-01-01T00:00:00Z")
    _uninstall_event(db, "s1", "2026-01-11T00:00:00Z")
    _install_event(db, "s1", "2026-05-01T00:00:00Z")
    _uninstall_event(db, "s1", "2026-05-06T00:00:00Z")
    db.commit()
    rows = churn_rows(db)
    assert [r["days"] for r in rows] == [5, 10]   # newest first


def test_review_candidates_start_at_thirty_days(db):
    _shop(db, "s1", shop_name="Veteran", shop_domain="veteran.myshopify.com")
    _shop(db, "s2", shop_name="Fresh")
    _shop(db, "s3", shop_name="Gone", install_state="uninstalled")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z")
    db.execute("update subscriptions set converted_at = now() - interval '30 days' where id='c1'")
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-01-01Z")
    db.execute("update subscriptions set converted_at = now() - interval '29 days' where id='c2'")
    _sub(db, "c3", "s3", Decimal("19.00"), "2024-01-01Z")
    db.commit()
    rows = review_candidates(db)
    assert [r["shop"] for r in rows] == ["Veteran"]      # 29 days out, uninstalled out
    assert rows[0]["domain"] == "veteran.myshopify.com"


def test_review_candidates_skip_merchants_who_already_reviewed(db):
    """A merchant asked for something they already did learns nobody is reading.
    reviewed_at is hand-maintained precisely so this list can skip them; the
    Partner API does not report reviews."""
    _shop(db, "s1", shop_name="Reviewed", owner_name="Ada", email="ada@ex.example",
          reviewed_at="2026-04-13")
    _shop(db, "s2", shop_name="Not yet", owner_name="Bo", email="bo@ex.example")
    for sub_id, gid in (("c1", "s1"), ("c2", "s2")):
        _sub(db, sub_id, gid, Decimal("19.00"), "2026-01-01Z")
    db.execute("update subscriptions set converted_at = now() - interval '90 days'")
    db.commit()
    rows = review_candidates(db)
    assert [r["shop"] for r in rows] == ["Not yet"]
    # No contact details, even though the columns still hold values: the only
    # source we ever had for them named agencies, not merchants (migration 008).
    assert "email" not in rows[0] and "owner_name" not in rows[0]


def test_review_candidates_exclude_churned_subscriptions(db):
    _shop(db, "s1", shop_name="Lapsed")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z", churned_at="2026-05-01Z")
    db.commit()
    assert review_candidates(db) == []


def test_annual_candidates_are_monthly_plans_past_three_months(db):
    _shop(db, "s1", shop_name="Monthly Long")
    _shop(db, "s2", shop_name="Monthly Short")
    _shop(db, "s3", shop_name="Already Annual")
    for cid, interval in (("c1", "EVERY_30_DAYS"), ("c2", "EVERY_30_DAYS"), ("c3", "ANNUAL")):
        db.execute(
            "insert into charges (gid, amount, currency_code, subscription_id, plan_interval) "
            "values (%s, 19.00, 'USD', %s, %s)", (cid, cid, interval))
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z")
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-01-01Z")
    _sub(db, "c3", "s3", Decimal("15.83"), "2026-01-01Z")
    db.execute("update subscriptions set converted_at = now() - interval '4 months' where id='c1'")
    db.execute("update subscriptions set converted_at = now() - interval '2 months' where id='c2'")
    db.execute("update subscriptions set converted_at = now() - interval '4 months' where id='c3'")
    db.commit()
    assert [r["shop"] for r in annual_upgrade_candidates(db)] == ["Monthly Long"]


def test_trial_watch_is_recent_installs_with_no_subscription(db):
    _shop(db, "s1", shop_name="Silent New")
    _shop(db, "s2", shop_name="Paid New")
    _shop(db, "s3", shop_name="Silent Old")
    db.execute("update shops set installed_at = now() - interval '3 days' where shop_gid='s1'")
    db.execute("update shops set installed_at = now() - interval '2 days' where shop_gid='s2'")
    db.execute("update shops set installed_at = now() - interval '20 days' where shop_gid='s3'")
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-08-01Z")
    db.commit()
    assert [r["shop"] for r in trial_watch(db)] == ["Silent New"]


def test_install_retention_covers_everyone_who_ever_installed(db):
    # Retained, churned in month 1, and one that left but came back.
    _shop(db, "s1", installed_at="2026-01-05Z")
    _shop(db, "s2", install_state="uninstalled",
          installed_at="2026-01-06Z", uninstalled_at="2026-02-10Z")
    _shop(db, "s3", installed_at="2026-01-07Z", uninstalled_at="2026-02-11Z")
    for gid, at in (("s1", "2026-01-05Z"), ("s2", "2026-01-06Z"), ("s3", "2026-01-07Z")):
        _install_event(db, gid, at)
    db.commit()

    out = install_retention_cohorts(db)
    jan = [c for c in out["cohorts"] if c["label"] == "01/2026"][0]
    assert jan["size"] == 3
    assert sum(c["size"] for c in out["cohorts"]) == 3   # every install lands in a cohort
    assert jan["cells"][0] == 100                        # month 0: all three still on
    # Month 1: s2 is gone, s1 and s3 (reinstalled) are not.
    assert jan["cells"][1] == 67


def test_install_retention_includes_a_shop_whose_first_event_was_a_reactivation(db):
    # Mid-month on purpose: month boundaries are read in the DB session's
    # timezone (as mrr_trend's date_trunc is), so a midnight-UTC timestamp lands
    # in the previous month on a machine west of UTC.
    _shop(db, "s1", installed_at="2026-03-15Z")
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid) "
        "values ('re1', 'reinstalled', '2026-03-15Z', 's1')")
    db.commit()
    out = install_retention_cohorts(db)
    assert [c["label"] for c in out["cohorts"]] == ["03/2026"]
    assert out["cohorts"][0]["size"] == 1


def _txn(db, id, at, gross, net, type="AppSubscriptionSale", shop_gid="s1"):
    db.execute(
        "insert into transactions (id, type, created_at, shop_gid, gross_amount, "
        "shopify_fee, net_amount, currency_code) "
        "values (%s, %s, %s, %s, %s, 0, %s, 'USD')",
        (id, type, at, shop_gid, gross, net),
    )


def test_collected_revenue_measures_the_fee_rather_than_assuming_a_rate(db):
    # Three different settlement rates on three identically priced charges,
    # which is what the real feed looks like. If `taken` were computed from a
    # 2.9% constant, or read off shopify_fee (the revenue share, 0.00 below
    # $1M of lifetime earnings), both assertions below would be wrong.
    _txn(db, "t1", "2026-07-01Z", Decimal("19.00"), Decimal("18.45"))
    _txn(db, "t2", "2026-07-02Z", Decimal("19.00"), Decimal("18.07"))
    _txn(db, "t3", "2026-07-03Z", Decimal("19.00"), Decimal("17.88"))
    db.commit()

    money = collected_revenue(db)
    assert money["gross"] == Decimal("57.00")
    assert money["net"] == Decimal("54.40")
    assert money["taken"] == Decimal("2.60")
    assert money["count"] == 3
    assert money["refund_count"] == 0


def test_collected_revenue_counts_refunds_and_nets_them_out(db):
    _txn(db, "t1", "2026-07-01Z", Decimal("19.00"), Decimal("18.45"))
    _txn(db, "t2", "2026-07-05Z", Decimal("-19.00"), Decimal("-19.00"),
         type="AppSaleAdjustment")
    _txn(db, "t3", "2026-07-06Z", Decimal("-19.00"), Decimal("-19.00"),
         type="AppSaleCredit")
    db.commit()

    money = collected_revenue(db)
    assert money["refund_count"] == 2
    # Reported as a positive amount even though it is stored negative.
    assert money["refunded"] == Decimal("38.00")
    # ...and already netted out of the totals rather than added on top.
    assert money["gross"] == Decimal("-19.00")


def test_revenue_by_month_keeps_empty_months(db):
    _txn(db, "t1", "2026-07-15Z", Decimal("19.00"), Decimal("18.45"))
    db.commit()
    months = revenue_by_month(db, months=12)
    assert len(months) == 12                        # a gap is a zero, not a missing row
    assert all(m["gross"] is not None for m in months)


def test_ltv_is_arpu_over_churn(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3", install_state="uninstalled")
    _sub(db, "c1", "s1", Decimal("20.00"), now - timedelta(days=300))
    _sub(db, "c2", "s2", Decimal("20.00"), now - timedelta(days=300))
    # Active when the 90-day window opened, gone inside it: 1 of 3 = 33.3% over
    # 90 days, so 11.1% a month, so LTV = 20 / 0.111 = 180.
    _sub(db, "c3", "s3", Decimal("20.00"), now - timedelta(days=300),
         churned_at=now - timedelta(days=10))
    db.commit()

    out = unit_economics(db)
    assert out["subs_at_start"] == 3
    assert out["churned_in_window"] == 1
    assert out["monthly_churn_pct"] == 11.1
    assert round(out["ltv"]) == 180


def test_ltv_is_none_rather_than_infinite_when_nobody_churned(db):
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), datetime.now(timezone.utc) - timedelta(days=300))
    db.commit()
    out = unit_economics(db)
    assert out["ltv"] is None
    assert out["monthly_churn_pct"] == 0.0


def test_install_reconciliation_names_the_measurement_gap(db):
    now = datetime.now(timezone.utc)
    db.execute("insert into ga4_daily (date, dimension, value, sessions, users, "
               "add_app_clicks, installs, ad_clicks) "
               "values (current_date - 3, 'total', '', 100, 90, 12, 6, 0)")
    for i in range(10):
        _shop(db, f"s{i}")
        db.execute("insert into app_events (platform_event_id, type, occurred_at, "
                   "shop_gid) values (%s, 'installed', %s, %s)",
                   (f"e{i}", now - timedelta(days=2), f"s{i}"))
    db.commit()

    out = install_reconciliation(db)
    assert out["ga4_installs"] == 6
    assert out["partner_installs"] == 10
    # The Partner API is the truth, so the gap is what GA4 never saw.
    assert out["gap"] == 4
    assert out["missed_pct"] == 40.0


def test_install_reconciliation_survives_an_empty_partner_side(db):
    out = install_reconciliation(db)
    assert out["partner_installs"] == 0
    assert out["missed_pct"] == 0.0


def test_verbatims_group_under_the_first_reason_selected(db):
    _shop(db, "s1", shop_domain="a.myshopify.com", install_state="uninstalled")
    _shop(db, "s2", shop_domain="b.myshopify.com", install_state="uninstalled")
    _shop(db, "s3", shop_domain="c.myshopify.com", install_state="uninstalled")
    _uninstall_event(db, "s1", "2026-07-01T00:00:00Z", reason="Too expensive",
                     description="Costs more than the GWP earned us.")
    _uninstall_event(db, "s2", "2026-07-02T00:00:00Z", reason="Too expensive",
                     description="Cheaper option elsewhere.")
    _uninstall_event(db, "s3", "2026-07-03T00:00:00Z", reason="Not using app now",
                     description="Seasonal pause.")
    db.commit()

    groups = {g["label"]: g for g in uninstall_verbatims(db)}
    assert len(groups["Too expensive"]["notes"]) == 2
    # Biggest group first, newest note first inside it.
    assert uninstall_verbatims(db)[0]["label"] == "Too expensive"
    assert groups["Too expensive"]["notes"][0]["note"] == "Cheaper option elsewhere."


def test_verbatims_skip_empty_notes_and_deactivations(db):
    _shop(db, "s1", install_state="uninstalled")
    _shop(db, "s2", install_state="uninstalled")
    _shop(db, "s3", install_state="uninstalled")
    _uninstall_event(db, "s1", "2026-07-01T00:00:00Z", reason="Too expensive")
    _uninstall_event(db, "s2", "2026-07-02T00:00:00Z", reason="Too expensive",
                     description="   ")
    # A store Shopify closed was never shown the question at all.
    _uninstall_event(db, "s3", "2026-07-03T00:00:00Z", description="ignore me",
                     raw_type="RELATIONSHIP_DEACTIVATED")
    db.commit()
    assert uninstall_verbatims(db) == []


# --- Comparison to the previous period ---------------------------------------
#
# The reason these get their own coverage rather than riding on a page render:
# a comparison that silently compares the wrong two things looks exactly like a
# comparison that works.

def _install_event(db, gid, at, kind="installed"):
    event_id = f"i-{gid}-{at}-{kind}"
    db.execute(
        "insert into raw_app_events (id, type, occurred_at, shop_gid, payload) "
        "values (%s, 'RELATIONSHIP_INSTALLED', %s, %s, '{}')",
        (event_id, at, gid),
    )
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid) "
        "values (%s, %s, %s, %s)",
        (event_id, kind, at, gid),
    )


def test_installed_at_time_replays_the_lifecycle(db):
    """The installed base as of an instant, read off the events rather than off
    shops.install_state, which only ever describes now."""
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _shop(db, "s2", install_state="uninstalled")
    _install_event(db, "s1", now - timedelta(days=90))
    _install_event(db, "s2", now - timedelta(days=90))
    _uninstall_event(db, "s2", now - timedelta(days=10))
    db.commit()

    # Both were installed 30 days ago; one has left since.
    assert installed_at_time(db, now - timedelta(days=30)) == 2
    assert installed_at_time(db, now) == 1
    # Before either of them existed.
    assert installed_at_time(db, now - timedelta(days=200)) == 0


def test_a_shop_that_came_back_counts_as_installed(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _install_event(db, "s1", now - timedelta(days=100))
    _uninstall_event(db, "s1", now - timedelta(days=60))
    _install_event(db, "s1", now - timedelta(days=40), kind="reinstalled")
    db.commit()
    assert installed_at_time(db, now - timedelta(days=50)) == 0
    assert installed_at_time(db, now) == 1


def test_windowed_counts_compare_to_the_window_before(db):
    """Installs in the last 30 days are compared against the 30 days before
    that, not against the whole of history and not against a point in time."""
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3")
    _install_event(db, "s1", now - timedelta(days=5))     # this window
    _install_event(db, "s2", now - timedelta(days=40))    # previous window
    _install_event(db, "s3", now - timedelta(days=200))   # neither
    db.commit()

    current = overview_stats(db)
    assert current["installs_30d"] == 1
    comparison = overview_comparison(db, {**current, "net_30d": Decimal("0")})
    assert comparison["installs_30d"]["prior"] == 1
    assert comparison["installs_30d"]["change"] == 0


def test_point_in_time_figures_compare_to_their_own_past(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _shop(db, "s2")
    _sub(db, "c1", "s1", Decimal("19.00"), now - timedelta(days=90))
    _sub(db, "c2", "s2", Decimal("19.00"), now - timedelta(days=10))
    db.commit()

    current = overview_stats(db)
    comparison = overview_comparison(db, {**current, "net_30d": Decimal("0")})
    # One of the two subscriptions did not exist 30 days ago.
    assert comparison["active_mrr"]["prior"] == Decimal("19.00")
    assert comparison["active_mrr"]["change"] == Decimal("19.00")
    assert comparison["paying"]["prior"] == 1
    assert comparison["paying"]["change"] == 1
    assert comparison["active_mrr"]["pct"] == 100.0


def test_no_percentage_from_a_zero_base(db):
    """"Up 100%" from nothing is a division by zero wearing a hat."""
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), now - timedelta(days=2))
    db.commit()
    current = overview_stats(db)
    comparison = overview_comparison(db, {**current, "net_30d": Decimal("0")})
    assert comparison["active_mrr"]["prior"] == 0
    assert comparison["active_mrr"]["pct"] is None


def test_every_compared_key_is_reported(db):
    current = overview_stats(db)
    comparison = overview_comparison(db, {**current, "net_30d": Decimal("0")})
    assert set(comparison) == set(COMPARED)


# --- Churn filters -----------------------------------------------------------

def test_churn_rows_filter_on_a_reason_bucket(db):
    _shop(db, "s1", install_state="uninstalled")
    _shop(db, "s2", install_state="uninstalled")
    _uninstall_event(db, "s1", "2026-07-01T00:00:00Z", reason="Too expensive")
    _uninstall_event(db, "s2", "2026-07-02T00:00:00Z", reason="Not using app now")
    db.commit()

    rows = churn_rows(db, bucket="Too expensive")
    assert len(rows) == 1
    assert rows[0]["shop"] == "s1"
    # A bucket nobody selected is empty rather than an error.
    assert churn_rows(db, bucket="Nonexistent bucket") == []


def test_a_multi_reason_uninstall_matches_either_bucket(db):
    _shop(db, "s1", install_state="uninstalled")
    _uninstall_event(db, "s1", "2026-07-01T00:00:00Z",
                     reason="Too expensive,Not using app now")
    db.commit()
    assert len(churn_rows(db, bucket="Too expensive")) == 1
    assert len(churn_rows(db, bucket="Not using app now")) == 1


def test_churn_rows_take_a_window(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1", install_state="uninstalled")
    _shop(db, "s2", install_state="uninstalled")
    _uninstall_event(db, "s1", now - timedelta(days=10))
    _uninstall_event(db, "s2", now - timedelta(days=200))
    db.commit()

    assert len(churn_rows(db)) == 2                    # all time by default
    assert len(churn_rows(db, since_days=30)) == 1
    assert len(churn_rows(db, since_days=365)) == 2


def test_a_window_and_a_bucket_apply_together(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1", install_state="uninstalled")
    _shop(db, "s2", install_state="uninstalled")
    _uninstall_event(db, "s1", now - timedelta(days=10), reason="Too expensive")
    _uninstall_event(db, "s2", now - timedelta(days=200), reason="Too expensive")
    db.commit()
    assert len(churn_rows(db, bucket="Too expensive", since_days=30)) == 1


# --- Link targets ------------------------------------------------------------

def test_plan_mix_carries_the_raw_interval_for_its_link(db):
    """The bar links to /customers?plan=..., so the interval has to survive the
    label lookup rather than being thrown away with it."""
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z")
    db.execute("insert into charges (gid, plan_interval) values ('c1', 'EVERY_30_DAYS')")
    db.commit()
    rows = plan_mix(db)
    assert rows[0]["interval"] == "EVERY_30_DAYS"
    assert rows[0]["label"] == "Monthly"


def test_the_other_country_row_is_flagged_not_sniffed(db):
    """It sums many countries, so no single /customers filter reproduces it and
    it must not become a link."""
    for i in range(12):
        _shop(db, f"s{i}", country=f"C{i:02d}")
    db.commit()
    rows = country_breakdown(db, top=10)
    assert rows[-1]["other"] is True
    assert all("other" not in r for r in rows[:-1])
