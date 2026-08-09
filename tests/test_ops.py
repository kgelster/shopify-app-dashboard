"""The stale-sync warning is the only thing that notices when the pipeline dies,
so its predicate and its de-dupe get direct tests rather than being trusted."""

from types import SimpleNamespace

from partner_dashboard.ops import build_stale_message, check_stale_sync, sync_health


def _settings(webhook="http://hook", poll_interval_minutes=15):
    return SimpleNamespace(slack_webhook_url=webhook,
                           public_base_url="https://dash.example.com",
                           app_name="Example App",
                           dashboard_name="Example App Analytics",
                           poll_interval_minutes=poll_interval_minutes)


def _capture():
    sent = []

    def post(url, json):
        sent.append(json)
        return SimpleNamespace(status_code=200)

    return sent, post


def _synced(db, ago_sql):
    db.execute(
        "insert into sync_state (source, cursor, last_synced_at) "
        f"values ('partner_api', 'c', now() - interval '{ago_sql}') "
        "on conflict (source) do update set last_synced_at = excluded.last_synced_at"
    )
    db.commit()


def test_health_is_fresh_within_three_polls(db):
    _synced(db, "20 minutes")
    health = sync_health(db, poll_interval_minutes=15)
    assert health["stale"] is False
    assert health["age_minutes"] == 20
    assert health["page_threshold_minutes"] == 45


def test_health_goes_stale_past_three_polls(db):
    _synced(db, "50 minutes")
    assert sync_health(db, poll_interval_minutes=15)["stale"] is True


def test_a_sync_that_never_ran_is_stale_not_green(db):
    health = sync_health(db, poll_interval_minutes=15)
    assert health["stale"] is True
    assert health["last_synced_at"] is None and health["age_minutes"] is None


def test_health_counts_recent_ingestion_and_shops(db):
    _synced(db, "5 minutes")
    db.execute("insert into shops (shop_gid, install_state) values ('s1','installed')")
    db.execute(
        "insert into raw_app_events (id, type, occurred_at, shop_gid, payload, ingested_at) "
        "values ('r1','RELATIONSHIP_INSTALLED', now(), 's1', '{}', now())")
    db.execute(
        "insert into raw_app_events (id, type, occurred_at, shop_gid, payload, ingested_at) "
        "values ('r2','RELATIONSHIP_INSTALLED', now(), 's1', '{}', now() - interval '3 days')")
    db.commit()
    health = sync_health(db, poll_interval_minutes=15)
    assert health["events_24h"] == 1 and health["shops"] == 1


def test_fresh_sync_posts_nothing(db):
    _synced(db, "10 minutes")
    sent, post = _capture()
    assert check_stale_sync(db, _settings(), http_post=post) is False
    assert sent == []


def test_stale_sync_alerts_once_per_episode(db):
    _synced(db, "3 hours")
    sent, post = _capture()
    assert check_stale_sync(db, _settings(), http_post=post) is True
    assert len(sent) == 1
    # Still stale on the next tick: the point of the flag is that this is quiet.
    assert check_stale_sync(db, _settings(), http_post=post) is False
    assert len(sent) == 1


def test_recovery_rearms_the_alert(db):
    _synced(db, "3 hours")
    sent, post = _capture()
    check_stale_sync(db, _settings(), http_post=post)
    _synced(db, "1 minute")
    assert check_stale_sync(db, _settings(), http_post=post) is False
    assert db.execute(
        "select stale_alerted_at from sync_state where source='partner_api'"
    ).fetchone()[0] is None
    # A second outage after recovery must alert again.
    _synced(db, "3 hours")
    assert check_stale_sync(db, _settings(), http_post=post) is True
    assert len(sent) == 2


def test_a_failed_post_leaves_the_alert_armed(db):
    _synced(db, "3 hours")
    calls = []

    def failing(url, json):
        calls.append(json)
        return SimpleNamespace(status_code=500)

    assert check_stale_sync(db, _settings(), http_post=failing) is False
    assert db.execute(
        "select stale_alerted_at from sync_state where source='partner_api'"
    ).fetchone()[0] is None
    assert check_stale_sync(db, _settings(), http_post=failing) is False
    assert len(calls) == 2   # retried rather than silently swallowed


def test_no_webhook_configured_is_a_noop(db):
    _synced(db, "3 hours")
    sent, post = _capture()
    assert check_stale_sync(db, _settings(webhook=None), http_post=post) is False
    assert sent == []


def test_stale_message_carries_no_merchant_data():
    text = str(build_stale_message(180, "https://dash.example.com"))
    assert "180 minutes ago" in text
    assert "@" not in text          # no addresses of any kind
    assert "—" not in text          # no em dash


def test_both_thresholds_scale_with_the_poll_interval(db):
    """A 75-minute-old sync is normal at a 30-minute interval and a dead
    pipeline at 15. Neither threshold may be a fixed number of minutes, or
    raising POLL_INTERVAL_MINUTES silently turns every slow poll into an alert."""
    _synced(db, "75 minutes")

    assert sync_health(db, poll_interval_minutes=15)["stale"] is True
    assert sync_health(db, poll_interval_minutes=30)["page_threshold_minutes"] == 90
    assert sync_health(db, poll_interval_minutes=30)["stale"] is False

    sent, post = _capture()
    assert check_stale_sync(db, _settings(poll_interval_minutes=30),
                            http_post=post) is False
    assert sent == []
    assert check_stale_sync(db, _settings(poll_interval_minutes=15),
                            http_post=post) is True
    assert len(sent) == 1
