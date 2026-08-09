from partner_dashboard.scheduler import run_sync_job


class FakeConn:
    def __init__(self):
        self.closed_count = 0

    def close(self):
        self.closed_count += 1


def test_run_sync_job_closes_connection_on_success(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr("partner_dashboard.scheduler.run_sync", lambda *a, **k: {"raw_inserted": 0})
    run_sync_job(lambda: conn, client=object(), settings=object())
    assert conn.closed_count == 1


def test_run_sync_job_closes_connection_even_if_sync_raises(monkeypatch):
    conn = FakeConn()

    def boom(*a, **k):
        raise RuntimeError("sync failed")

    monkeypatch.setattr("partner_dashboard.scheduler.run_sync", boom)
    try:
        run_sync_job(lambda: conn, client=object(), settings=object())
    except RuntimeError:
        pass
    assert conn.closed_count == 1


def test_weekly_digest_is_registered_at_the_configured_local_time(monkeypatch):
    """Only the wiring: send_weekly_digest itself is tested in test_digest."""
    from types import SimpleNamespace

    import partner_dashboard.scheduler as sched

    monkeypatch.setattr(sched, "PartnerClient", lambda *a, **k: object())

    started = {}

    class FakeScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, **kw):
            self.jobs.append((trigger, kw))

        def start(self):
            started["yes"] = True

    fake = FakeScheduler()
    monkeypatch.setattr(sched, "BackgroundScheduler", lambda: fake)
    sched.start_scheduler(lambda: None, SimpleNamespace(
        partner_api_token="t", partner_org_id="1", poll_interval_minutes=15,
        digest_day_of_week="tue", digest_hour=7, digest_timezone="Europe/Berlin"))

    digest = [kw for trigger, kw in fake.jobs if trigger == "cron"]
    assert len(digest) == 1
    # Read off settings rather than hardcoded, so a deployment that wants its
    # digest on Tuesday morning in Berlin gets it there.
    assert digest[0]["day_of_week"] == "tue"
    assert digest[0]["hour"] == 7 and digest[0]["minute"] == 0
    assert digest[0]["timezone"] == "Europe/Berlin"
    assert started["yes"] is True
