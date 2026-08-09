from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from partner_dashboard.digest import (
    DIGEST_SOURCE,
    collect_digest,
    render_digest,
    send_weekly_digest,
    should_send,
)

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


def _settings(webhook="http://hook"):
    return SimpleNamespace(slack_webhook_url=webhook,
                           public_base_url="https://dash.example.com",
                           app_name="Example App",
                           dashboard_name="Example App Analytics")


def _capture():
    sent = []

    def post(url, json):
        sent.append(json)
        return SimpleNamespace(status_code=200)

    return sent, post


def _seed(db):
    db.execute("insert into shops (shop_gid, shop_name, install_state, installed_at) values "
               "('s1','Old Payer','installed','2026-01-05Z'),"
               "('s2','New Payer','installed',%s),"
               "('s3','Silent New','installed',%s),"
               "('s4','Left','uninstalled','2026-02-01Z')",
               (NOW - timedelta(days=3), NOW - timedelta(days=2)))
    db.execute("insert into subscriptions (id, shop_gid, monthly_amount, converted_at, churned_at) "
               "values ('c1','s1',19.00,'2026-01-10Z',null),"
               "('c2','s2',19.00,%s,null),"
               "('c4','s4',19.00,'2026-01-15Z',%s)",
               (NOW - timedelta(days=3), NOW - timedelta(days=4)))
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid) values "
        "('e1','installed',%s,'s2'),('e2','installed',%s,'s3'),('e3','uninstalled',%s,'s4')",
        (NOW - timedelta(days=3), NOW - timedelta(days=2), NOW - timedelta(days=4)))
    # 10 sessions/day last week, 20/day the week before, and a wild partial
    # figure for today, which must be excluded from both windows.
    for d in range(0, 15):
        sessions = 999 if d == 0 else (10 if d <= 7 else 20)
        db.execute("insert into ga4_daily (date, dimension, value, sessions, installs) "
                   "values (%s,'total','',%s,1)", ((NOW - timedelta(days=d)).date(), sessions))
    db.commit()


def test_collect_counts_the_week_and_its_deltas(db):
    _seed(db)
    data = collect_digest(db, now=NOW)
    assert data["installed"] == 3
    assert data["installs"] == 2 and data["uninstalls"] == 1
    assert data["installed_delta"] == 1
    assert data["paying"] == 2
    assert data["mrr"] == Decimal("38.00")
    # One shop started paying this week, one stopped: net zero, both visible.
    assert data["movement"]["new"] == Decimal("19.00")
    assert data["movement"]["churned"] == Decimal("-19.00")
    assert data["movement"]["net"] == Decimal("0")
    assert [s["shop"] for s in data["trial_watch"]] == ["Silent New"]
    assert data["sessions"] == 70            # 7 whole days, today excluded
    assert data["sessions_delta"] == -70     # against 7 x 20 the week before


def test_render_is_one_message_with_no_merchant_contact_details(db):
    _seed(db)
    text = render_digest(collect_digest(db, now=NOW))
    assert text.count("\n") < 10          # a glance, not a report
    assert "@" not in text                # no emails, ever
    assert "—" not in text                # no em dash
    assert "Silent New" in text           # shop names are fine internally
    assert "new +19" in text and "churned -19" in text


def test_render_survives_an_empty_week(db):
    text = render_digest(collect_digest(db, now=NOW))
    assert "nothing moved" in text
    assert "no GA4 sessions" in text


def test_digest_will_not_fire_twice_in_a_week(db):
    _seed(db)
    sent, post = _capture()
    assert send_weekly_digest(db, _settings(), http_post=post, now=NOW) is True
    assert len(sent) == 1
    assert send_weekly_digest(db, _settings(), http_post=post, now=NOW) is False
    assert len(sent) == 1


def test_a_failed_post_is_retried_next_run(db):
    _seed(db)
    calls = []

    def failing(url, json):
        calls.append(json)
        return SimpleNamespace(status_code=500)

    assert send_weekly_digest(db, _settings(), http_post=failing, now=NOW) is False
    assert db.execute(
        "select count(*) from sync_state where source = %s", (DIGEST_SOURCE,)
    ).fetchone()[0] == 0
    send_weekly_digest(db, _settings(), http_post=failing, now=NOW)
    assert len(calls) == 2


def test_should_send_guards_replays_and_restarts():
    monday = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    assert should_send(None, monday) is True
    assert should_send(monday - timedelta(minutes=5), monday) is False
    assert should_send(monday - timedelta(days=7), monday) is True


def test_no_webhook_is_a_noop(db):
    _seed(db)
    sent, post = _capture()
    assert send_weekly_digest(db, _settings(webhook=None), http_post=post, now=NOW) is False
    assert sent == []
