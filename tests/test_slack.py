from partner_dashboard.slack import build_event_message, notify_events


def _capture():
    sent = []
    return sent, lambda url, json: sent.append(json) or type("R", (), {"status_code": 200})()


def test_message_identifies_the_shop_and_carries_no_contact_details():
    """Slack alerts name the business, never a person. The only source of
    contact details this system ever had was a vendor export's staff list, which named
    agencies and our own team rather than the merchant; see migration 008."""
    msg = build_event_message({"shop_name": "X Store", "shop_domain": "x.myshopify.com",
        "country": "US", "plan": "Pro"}, "installed")
    text = str(msg)
    assert "x.myshopify.com" in text and "X Store" in text and "New install" in text
    assert "@" not in text          # no addresses of any kind
    assert "—" not in text          # no em dash


def test_notify_fires_without_email(db):
    # Fresh live installs have shop name/domain from the events feed but no
    # contact info until enrichment: alert anyway, render Unknown.
    db.execute("insert into shops(shop_gid, shop_domain, shop_name, install_state) "
               "values ('ai1','x.myshopify.com','X','installed')"); db.commit()
    sent, post = _capture()
    assert notify_events(db, [("ai1", "installed")], "http://hook", http_post=post) == 1
    text = str(sent[0])
    assert "x.myshopify.com" in text
    assert "Unknown" in text             # country/plan render as Unknown


def test_notify_uninstall_uses_uninstall_header(db):
    db.execute("insert into shops(shop_gid, shop_domain, shop_name, install_state) "
               "values ('ai1','x.myshopify.com','X','uninstalled')"); db.commit()
    sent, post = _capture()
    notify_events(db, [("ai1", "uninstalled")], "http://hook", http_post=post)
    assert "Uninstalled" in str(sent[0])
    assert "x.myshopify.com" in str(sent[0])


def test_notify_without_webhook_url_is_a_noop(db):
    db.execute("insert into shops(shop_gid, shop_name, install_state) "
               "values ('ai1','X','installed')"); db.commit()
    sent, post = _capture()
    assert notify_events(db, [("ai1", "installed")], None, http_post=post) == 0
    assert sent == []


def test_notify_caps_bulk_replay(db):
    for i in range(30):
        db.execute("insert into shops(shop_gid, shop_name, install_state) "
                   "values (%s,'X','installed')", (f"ai{i}",))
    db.commit()
    sent, post = _capture()
    n = notify_events(db, [(f"ai{i}", "installed") for i in range(30)],
                      "http://hook", http_post=post)
    assert n == 20                       # MAX_ALERTS_PER_SYNC
    assert len(sent) == 20


def test_notify_reports_active_plan_not_churned(db):
    # A shop with two subscription rows: an older, churned one and a newer,
    # active one. Without a deterministic ORDER BY, a plain LEFT JOIN +
    # fetchone() can surface either row, so this pins the fix (active rows
    # first, then most-recently-converted, limit 1).
    db.execute("insert into shops(shop_gid, shop_name, email, install_state) "
               "values ('ai1','X','j@x.com','installed')")
    db.execute("insert into subscriptions(id, shop_gid, monthly_amount, "
               "converted_at, churned_at) values "
               "('old','ai1',29.00,'2026-01-01','2026-02-01'), "
               "('new','ai1',49.00,'2026-05-01',NULL)")
    db.commit()
    sent, post = _capture()
    notify_events(db, [("ai1", "installed")], "http://hook", http_post=post)
    text = str(sent[0])
    assert "$49.00/mo" in text
    assert "$29.00/mo" not in text


def test_alert_links_the_shop_to_its_detail_page():
    """An alert that only names a shop makes you go and find it. Linking the
    headline puts the merchant's timeline, payments and uninstall reason one
    click away."""
    msg = build_event_message(
        {"shop_name": "X Store", "shop_domain": "x.myshopify.com",
         "country": "US", "plan": "Pro"},
        "uninstalled", "https://dash.test/")
    text = str(msg)
    assert "<https://dash.test/customers/x.myshopify.com|X Store>" in text
    assert "@" not in text


def test_alert_without_a_base_url_is_still_readable():
    msg = build_event_message({"shop_name": "X Store"}, "installed")
    assert "X Store" in str(msg)
    assert "customers/" not in str(msg)
