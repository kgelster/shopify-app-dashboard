from decimal import Decimal

from partner_dashboard.ingest_raw import upsert_charges, upsert_raw_events


def _ev(**kw):
    base = dict(id="e1", type="RELATIONSHIP_INSTALLED",
                occurred_at="2026-06-01T00:00:00Z",
                shop_gid="ai1", charge_gid=None, payload={})
    base.update(kw); return base


def test_insert_then_dedupe(db):
    assert upsert_raw_events(db, [_ev()]) == 1
    # exact same event re-ingested (overlap window) inserts nothing
    assert upsert_raw_events(db, [_ev()]) == 0


def test_null_charge_dedupes_via_sentinel(db):
    upsert_raw_events(db, [_ev(id="a", charge_gid=None)])
    # different id, same (install,type,time,null-charge) => duplicate, skipped
    assert upsert_raw_events(db, [_ev(id="b", charge_gid=None)]) == 0


def test_upsert_charges_from_inline_charge_objects(db):
    events = [
        _ev(),                                    # no charge -> skipped
        _ev(id="e2", type="SUBSCRIPTION_CHARGE_ACTIVATED", charge_gid="c1",
            charge={"id": "c1", "amount": {"amount": "19.0", "currencyCode": "USD"},
                    "billingOn": None, "name": "Pro", "test": False}),
    ]
    assert upsert_charges(db, events) == 1
    row = db.execute("select amount, currency_code, subscription_id, plan_interval, test "
                     "from charges where gid='c1'").fetchone()
    assert row == (Decimal("19.00"), "USD", "c1", "EVERY_30_DAYS", False)


def _charge_ev(gid, amount):
    return _ev(id=f"ev-{gid}", type="SUBSCRIPTION_CHARGE_ACTIVATED", charge_gid=gid,
               charge={"id": gid, "amount": {"amount": amount, "currencyCode": "USD"},
                       "billingOn": None, "name": "Pro", "test": False})


def test_annual_price_is_stored_as_an_annual_interval(db):
    # $190 is the yearly price of a plan that also sells at $19/30 days, and
    # ANNUAL_PLAN_AMOUNTS lists it. Storing it as EVERY_30_DAYS would count one
    # subscriber as $190/mo of MRR.
    upsert_charges(db, [_charge_ev("c-annual", "190.0")])
    (interval,) = db.execute(
        "select plan_interval from charges where gid='c-annual'"
    ).fetchone()
    assert interval == "ANNUAL"


def test_reingesting_repairs_a_wrong_stored_interval(db):
    upsert_charges(db, [_charge_ev("c-annual", "190.0")])
    db.execute("update charges set plan_interval='EVERY_30_DAYS' where gid='c-annual'")
    # plan_interval is in the ON CONFLICT update set, so the next poll corrects
    # history rather than leaving the bad value in place forever.
    upsert_charges(db, [_charge_ev("c-annual", "190.0")])
    (interval,) = db.execute(
        "select plan_interval from charges where gid='c-annual'"
    ).fetchone()
    assert interval == "ANNUAL"


def test_an_unlisted_annual_price_is_silently_counted_as_monthly(db, monkeypatch):
    """The trap this setting exists to make visible.

    AppSubscription carries no billing-interval field, so the only signal is the
    price. A price the operator forgets to list in ANNUAL_PLAN_AMOUNTS is not
    rejected or flagged, it is counted as a 30-day plan, which reports it at
    twelve times its real MRR. Empty is the default precisely because inheriting
    somebody else's price list would be worse.
    """
    monkeypatch.setenv("ANNUAL_PLAN_AMOUNTS", "")
    upsert_charges(db, [_charge_ev("c-unlisted", "490.0")])
    (interval,) = db.execute(
        "select plan_interval from charges where gid='c-unlisted'"
    ).fetchone()
    assert interval == "EVERY_30_DAYS"


def test_listing_the_price_is_what_makes_it_annual(db, monkeypatch):
    monkeypatch.setenv("ANNUAL_PLAN_AMOUNTS", "190.00, 490.00")
    upsert_charges(db, [_charge_ev("c-listed", "490.0")])
    (interval,) = db.execute(
        "select plan_interval from charges where gid='c-listed'"
    ).fetchone()
    assert interval == "ANNUAL"
