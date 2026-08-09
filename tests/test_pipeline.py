from partner_dashboard.pipeline import TRANSACTIONS_SOURCE, run_sync, sync_transactions


class FakeClient: ...


def _settings(**kw):
    from partner_dashboard.config import Settings
    return Settings(database_url="x", partner_api_token="t", partner_org_id="1",
                    partner_app_id="2", dashboard_users="tester:suite-only-credential", **kw)


def _txn(id, created_at, gross="19.0", net="18.45", type="AppSubscriptionSale"):
    return dict(id=id, type=type, created_at=created_at, shop_gid="gid://s/1",
                charge_gid="gid://c/1", billing_interval="EVERY_30_DAYS",
                gross_amount=gross, shopify_fee="0.0", net_amount=net,
                currency_code="USD")


def test_run_sync_ingests_derives_and_notifies(db, monkeypatch):
    # one install + subscribe page, then empty
    pages = [([
        dict(id="r1", type="RELATIONSHIP_INSTALLED", occurred_at="2026-06-01T00:00:00Z",
             shop_gid="ai1", charge_gid=None, payload={}),
    ], None)]
    monkeypatch.setattr("partner_dashboard.pipeline.fetch_app_events",
                        lambda *a, **k: pages.pop(0))
    db.execute("insert into shops(shop_gid,email,install_state) "
               "values ('ai1','j@x.com','')"); db.commit()
    sent = []
    from partner_dashboard.config import Settings
    s = Settings(database_url="x", partner_api_token="t", partner_org_id="1",
                 partner_app_id="2", dashboard_users="tester:suite-only-credential",
                 slack_webhook_url="http://hook")
    summary = run_sync(db, FakeClient(), s,
                       http_post=lambda url, json: sent.append(json) or type("R",(),{"status_code":200})())
    assert summary["raw_inserted"] == 1
    assert summary["alerts_sent"] == 1
    assert len(sent) == 1


def test_sync_transactions_pages_and_stores(db, monkeypatch):
    pages = [
        ([_txn("t1", "2026-08-01T00:00:00Z")], "cur1"),
        ([_txn("t2", "2026-08-02T00:00:00Z")], None),
    ]
    monkeypatch.setattr("partner_dashboard.pipeline.fetch_transactions", lambda *a, **k: pages.pop(0))
    summary = sync_transactions(db, FakeClient(), _settings(), sleep=lambda _: None)

    assert summary["transactions_inserted"] == 2
    assert summary["pages"] == 2
    # First run has no bound: pull the whole history rather than a window.
    assert summary["since"] is None
    (n,) = db.execute("select count(*) from transactions").fetchone()
    assert n == 2
    # Its own sync_state row, so the events cursor is untouched.
    (last,) = db.execute("select last_synced_at from sync_state where source = %s",
                         (TRANSACTIONS_SOURCE,)).fetchone()
    assert last is not None


def test_sync_transactions_rewinds_by_overlap_and_dedupes(db, monkeypatch):
    monkeypatch.setattr("partner_dashboard.pipeline.fetch_transactions",
                        lambda *a, **k: ([_txn("t1", "2026-08-02T12:00:00Z")], None))
    sync_transactions(db, FakeClient(), _settings(), sleep=lambda _: None)

    seen = {}

    def capture(client, **kwargs):
        seen.update(kwargs)
        # Same row again (the overlap replay) plus a settled net amount.
        return [_txn("t1", "2026-08-02T12:00:00Z", net="18.50")], None

    monkeypatch.setattr("partner_dashboard.pipeline.fetch_transactions", capture)
    summary = sync_transactions(db, FakeClient(), _settings(poll_overlap_minutes=60),
                                sleep=lambda _: None)

    # The window is derived from the newest stored row, not from a cursor.
    assert seen["created_at_min"].startswith("2026-08-02T11:00:00+00:00")
    # A replayed row is not a new payment...
    assert summary["transactions_inserted"] == 0
    # ...but its amounts do refresh, because Shopify settles after creating.
    (net,) = db.execute("select net_amount from transactions where id = 't1'").fetchone()
    assert str(net) == "18.50"
