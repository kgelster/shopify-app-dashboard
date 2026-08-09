import json
from datetime import datetime, timedelta, timezone

import pytest

from partner_dashboard.usage import (
    MAX_BATCH,
    MAX_PROPERTIES_BYTES,
    PER_SHOP_DAILY_CAP,
    UsageError,
    activation_cohorts,
    at_risk_shops,
    has_usage_data,
    ingest,
    parse_batch,
    time_to_activation,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def _event(**over):
    event = {
        "event_id": "e1",
        "shop_gid": "gid://shopify/Shop/1",
        "event_type": "offer_created",
        "occurred_at": "2026-08-10T11:00:00Z",
    }
    event.update(over)
    return event


def _body(*events):
    return json.dumps({"events": list(events)}).encode()


# --- validation ------------------------------------------------------------

def test_a_well_formed_batch_parses():
    events = parse_batch(_body(_event(), _event(event_id="e2",
                                                event_type="offer_impression")), now=NOW)
    assert [e["event_type"] for e in events] == ["offer_created", "offer_impression"]
    assert events[0]["occurred_at"] == datetime(2026, 8, 10, 11, tzinfo=timezone.utc)
    assert events[0]["properties"] == {}


@pytest.mark.parametrize("raw,status", [
    (b"not json", 400),
    (b"[]", 422),                                   # array, not an object
    (b'{"events": {}}', 422),                       # events not an array
    (b'{"events": []}', 422),                       # empty batch
    (b'{"events": ["nope"]}', 422),                 # element not an object
])
def test_malformed_bodies_are_rejected(raw, status):
    with pytest.raises(UsageError) as exc:
        parse_batch(raw, now=NOW)
    assert exc.value.status == status


def test_unknown_event_types_are_rejected_not_stored():
    """An unrecognized name is either an app bug or a probe. Storing it would
    silently corrupt whatever report later counts that column."""
    with pytest.raises(UsageError) as exc:
        parse_batch(_body(_event(event_type="offer_created; drop table")), now=NOW)
    assert exc.value.status == 422
    assert "not a known event" in exc.value.message


def test_batch_size_is_capped():
    events = [_event(event_id=f"e{i}") for i in range(MAX_BATCH + 1)]
    with pytest.raises(UsageError) as exc:
        parse_batch(_body(*events), now=NOW)
    assert exc.value.status == 413


def test_oversized_and_nested_properties_are_rejected():
    with pytest.raises(UsageError):
        parse_batch(_body(_event(properties={"blob": "x" * (MAX_PROPERTIES_BYTES + 10)})),
                    now=NOW)
    with pytest.raises(UsageError) as exc:
        parse_batch(_body(_event(properties={"nested": {"a": 1}})), now=NOW)
    assert "string, number, or boolean" in exc.value.message


def test_long_identifiers_are_rejected():
    with pytest.raises(UsageError):
        parse_batch(_body(_event(event_id="x" * 5000)), now=NOW)
    with pytest.raises(UsageError):
        parse_batch(_body(_event(shop_gid="x" * 5000)), now=NOW)


def test_future_and_naive_timestamps_are_rejected():
    """A client clock far in the future would park events past every report
    window; a naive timestamp would be read in whatever timezone the DB session
    happens to hold."""
    with pytest.raises(UsageError):
        parse_batch(_body(_event(occurred_at="2027-01-01T00:00:00Z")), now=NOW)
    with pytest.raises(UsageError):
        parse_batch(_body(_event(occurred_at="2026-08-10T11:00:00")), now=NOW)


def test_a_batch_cannot_contain_the_same_event_twice():
    with pytest.raises(UsageError):
        parse_batch(_body(_event(), _event()), now=NOW)


def test_the_same_event_id_from_two_shops_is_fine():
    """The dedupe key is scoped per shop, so two apps generating id "1" do not
    collide."""
    events = parse_batch(_body(_event(), _event(shop_gid="gid://shopify/Shop/2")), now=NOW)
    assert len(events) == 2


# --- ingest ----------------------------------------------------------------

def test_ingest_is_idempotent_and_never_overwrites(db):
    first = ingest(db, parse_batch(_body(_event(properties={"offer": "bogo"})), now=NOW))
    assert first == {"received": 1, "stored": 1, "duplicates": 0, "rate_limited": 0}

    # Same key, different payload: the stored event must win.
    second = ingest(db, parse_batch(
        _body(_event(event_type="offer_conversion", properties={"offer": "tampered"})), now=NOW))
    assert second["stored"] == 0 and second["duplicates"] == 1

    row = db.execute(
        "select event_type, properties from usage_events").fetchall()
    assert row == [("offer_created", {"offer": "bogo"})]


def test_a_shop_over_the_daily_cap_is_dropped_without_failing_the_batch(db, monkeypatch):
    monkeypatch.setattr("partner_dashboard.usage.PER_SHOP_DAILY_CAP", 2)
    ingest(db, parse_batch(_body(_event(event_id="a"), _event(event_id="b")), now=NOW))

    result = ingest(db, parse_batch(
        _body(_event(event_id="c"), _event(shop_gid="gid://shopify/Shop/2", event_id="d")),
        now=NOW))
    # The flooding shop is dropped; the innocent one in the same batch is not.
    assert result == {"received": 2, "stored": 1, "duplicates": 0, "rate_limited": 1}
    assert db.execute(
        "select count(*) from usage_events where shop_gid = %s",
        ("gid://shopify/Shop/1",)).fetchone()[0] == 2


def test_the_cap_is_a_rolling_day_not_all_time(db, monkeypatch):
    monkeypatch.setattr("partner_dashboard.usage.PER_SHOP_DAILY_CAP", 1)
    ingest(db, parse_batch(_body(_event(event_id="old")), now=NOW))
    db.execute("update usage_events set received_at = now() - interval '2 days'")
    db.commit()
    assert ingest(db, parse_batch(_body(_event(event_id="new")), now=NOW))["stored"] == 1


# --- reports ---------------------------------------------------------------

def _install(db, gid, when, name=None):
    db.execute("insert into shops (shop_gid, shop_name, install_state, installed_at) "
               "values (%s, %s, 'installed', %s)", (gid, name or gid, when))
    db.execute("insert into app_events (platform_event_id, type, occurred_at, shop_gid) "
               "values (%s, 'installed', %s, %s)", (f"ev-{gid}", when, gid))


def _usage(db, gid, event_type, when, received=None):
    db.execute(
        """insert into usage_events (shop_gid, event_id, event_type, occurred_at, received_at)
           values (%s, %s, %s, %s, coalesce(%s, now()))""",
        (gid, f"{gid}-{event_type}-{when.isoformat()}", event_type, when, received))


def test_empty_state_is_distinguishable_from_zero_activation(db):
    _install(db, "s1", NOW - timedelta(days=10))
    db.commit()
    assert has_usage_data(db) is False


def test_activation_cohorts_and_median(db):
    tracking_started = NOW - timedelta(days=60)
    fast = NOW - timedelta(days=40)
    slow = NOW - timedelta(days=39)
    never = NOW - timedelta(days=38)
    # Installed before tracking existed: must be excluded, not counted as a
    # merchant who never activated.
    _install(db, "old", NOW - timedelta(days=200))
    _install(db, "fast", fast)
    _install(db, "slow", slow)
    _install(db, "never", never)
    _usage(db, "fast", "offer_created", fast + timedelta(hours=2), tracking_started)
    _usage(db, "slow", "offer_created", slow + timedelta(days=5), tracking_started)
    db.commit()

    summary = time_to_activation(db)
    assert summary["eligible"] == 3          # "old" is not in the denominator
    assert summary["activated"] == 2 and summary["never"] == 1
    assert summary["rate"] == 67
    assert summary["median_hours"] == pytest.approx(61, abs=2)   # median of 2h and 120h

    rows = activation_cohorts(db, months=6)
    assert len(rows) == 1
    assert rows[0]["cohort"] == 3
    assert rows[0]["within_48h"] == 1        # only "fast" made 48 hours
    assert rows[0]["within_7d"] == 2
    assert rows[0]["rate_7d"] == 67


def test_at_risk_lists_paying_shops_whose_offers_stopped_showing(db):
    for gid in ("quiet", "busy", "unpaid"):
        _install(db, gid, NOW - timedelta(days=90), name=gid.title())
    for gid in ("quiet", "busy"):
        db.execute("insert into subscriptions (id, shop_gid, monthly_amount, converted_at) "
                   "values (%s, %s, 19.00, now() - interval '60 days')", (f"c-{gid}", gid))
    _usage(db, "quiet", "offer_impression", datetime.now(timezone.utc) - timedelta(days=30))
    _usage(db, "busy", "offer_impression", datetime.now(timezone.utc) - timedelta(days=1))
    _usage(db, "unpaid", "offer_impression", datetime.now(timezone.utc) - timedelta(days=30))
    db.commit()

    rows = at_risk_shops(db, days=14)
    # "busy" served an offer yesterday; "unpaid" is quiet but pays nothing, so
    # it is a trial-watch problem, not a churn-risk one.
    assert [r["shop"] for r in rows] == ["Quiet"]
    assert rows[0]["days_quiet"] == 30


def test_a_shop_that_predates_tracking_is_not_called_at_risk(db):
    _install(db, "ancient", NOW - timedelta(days=300), name="Ancient")
    db.execute("insert into subscriptions (id, shop_gid, monthly_amount, converted_at) "
               "values ('c1', 'ancient', 19.00, now() - interval '200 days')")
    db.commit()
    assert at_risk_shops(db, days=14) == []


# --- hardening added after the pre-publication red team ---------------------


def test_a_shop_gid_must_be_a_shop_gid():
    """An arbitrary string is a free per-shop rate-limit bucket and an unbounded
    set of junk keys. The cap keys on this value, so without a shape check the
    'per-shop flood ceiling' bounds nothing: 500 fresh strings in one batch all
    get their own allowance."""
    for bad in ("x", "'; drop table shops; --", "gid://shopify/Customer/1",
                "gid://shopify/Shop/", "gid://shopify/Shop/abc", " gid://shopify/Shop/1"):
        with pytest.raises(UsageError) as e:
            parse_batch(_body(_event(shop_gid=bad)), now=NOW)
        assert e.value.status == 422


def test_non_finite_and_oversized_numbers_are_rejected_not_500s():
    """json.loads accepts NaN and Infinity, and isinstance(inf, float) is True,
    so these passed validation and died in Postgres instead."""
    for bad in (float("inf"), float("-inf"), float("nan"), 10 ** 40):
        with pytest.raises(UsageError) as e:
            parse_batch(_body(_event(properties={"n": bad})), now=NOW)
        assert e.value.status == 422


def test_a_malformed_body_is_a_400_however_it_is_malformed():
    """Beyond bad JSON: deep nesting raises RecursionError and a huge integer
    literal raises ValueError from the int-str limit. Both are malformed
    requests, and either escaping turns a 400 into a 500 on the one route an
    anonymous caller reaches."""
    for raw in (b"{" * 200_000, b'{"events": [' + b"[" * 100_000,
                b'{"events": [{"properties": {"n": ' + b"1" * 100_000 + b"}}]}"):
        with pytest.raises(UsageError) as e:
            parse_batch(raw, now=NOW)
        assert e.value.status in (400, 422)


def test_an_event_cannot_be_backdated_before_the_app_store_existed():
    """Without a floor, a backdated 'activation' rewrites the headline number:
    time_to_activation returns a negative median and the cohort rates read
    100%. The endpoint holds one shared secret, not a per-shop credential."""
    with pytest.raises(UsageError) as e:
        parse_batch(_body(_event(occurred_at="2001-01-01T00:00:00+00:00")), now=NOW)
    assert e.value.status == 422
