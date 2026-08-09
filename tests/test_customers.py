from decimal import Decimal

from partner_dashboard.customers import (
    count_customers,
    customer_detail,
    distinct_facets,
    list_customers,
)


def _shop(db, ai, **kw):
    cols = {"shop_gid": ai, "shop_name": ai, "install_state": "installed"}
    cols.update(kw)
    keys = ",".join(cols)
    ph = ",".join(["%s"] * len(cols))
    db.execute(f"insert into shops({keys}) values ({ph})", list(cols.values()))
    db.commit()


def test_filter_by_industry_and_country(db):
    _shop(db, "ai1", industry="Apparel", country="US", email="a@x.com")
    _shop(db, "ai2", industry="Food", country="US", email="b@x.com")
    _shop(db, "ai3", industry="Apparel", country="CA", email="c@x.com")
    rows = list_customers(db, industry="Apparel", country="US")
    assert [r["shop_gid"] for r in rows] == ["ai1"]


def test_facets_are_distinct_sorted(db):
    _shop(db, "ai1", industry="Apparel", country="US")
    _shop(db, "ai2", industry="Food", country="US")
    f = distinct_facets(db)
    assert f["industries"] == ["Apparel", "Food"] and f["countries"] == ["US"]


def test_sort_whitelist_rejects_injection(db):
    _shop(db, "ai1", industry="Apparel", country="US")
    rows = list_customers(db, sort="; drop table shops;--")
    assert isinstance(rows, list)  # falls back to default sort, no error


def test_install_state_filter(db):
    db.execute("insert into shops(shop_gid,shop_name,install_state) "
               "values ('a','Live Shop','installed'),('b','Gone Shop','uninstalled')")
    db.commit()
    live = list_customers(db, install_state="installed")
    assert [r["shop_name"] for r in live] == ["Live Shop"]
    # Anything not in the whitelist is ignored rather than injected into the SQL.
    assert len(list_customers(db, install_state="'; drop table shops; --")) == 2
    assert "uninstalled" in distinct_facets(db)["states"]


def test_count_matches_the_same_filters_the_page_query_uses(db):
    for i in range(7):
        _shop(db, f"ai{i}", country="US" if i < 5 else "CA")
    assert count_customers(db) == 7
    assert count_customers(db, country="US") == 5
    # The count must ignore limit/offset or the pager would say "5 of 2".
    assert len(list_customers(db, country="US", limit=2)) == 2
    assert count_customers(db, country="US") == 5


def test_paging_walks_the_whole_result_set_without_gaps_or_repeats(db):
    for i in range(12):
        _shop(db, f"ai{i:02d}", shop_name=f"Shop {i:02d}")
    seen = []
    for page in range(3):
        seen += [r["shop_gid"] for r in list_customers(db, limit=5, offset=page * 5)]
    assert len(seen) == 12 and len(set(seen)) == 12


def _event(db, gid, raw_type, clean_type, at, **kw):
    eid = f"{gid}-{at}"
    db.execute("insert into raw_app_events (id, type, occurred_at, shop_gid, payload) "
               "values (%s, %s, %s, %s, '{}')", (eid, raw_type, at, gid))
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid, "
        "plan_amount, plan_interval, uninstall_reason, uninstall_description) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (eid, clean_type, at, gid, kw.get("amount"), kw.get("interval"),
         kw.get("reason"), kw.get("note")),
    )


def test_detail_header_agrees_with_a_reinstalling_merchant(db):
    """The classic defect in this kind of page: a header reading "churned" above
    a timeline whose last event is an install. The state here is derived from
    the timeline rows, so it cannot drift."""
    _shop(db, "g1", shop_domain="back.myshopify.com")
    _event(db, "g1", "RELATIONSHIP_INSTALLED", "installed", "2026-01-10Z")
    _event(db, "g1", "RELATIONSHIP_UNINSTALLED", "uninstalled", "2026-03-05Z",
           reason="Too expensive", note="Back next season.")
    _event(db, "g1", "RELATIONSHIP_REACTIVATED", "reinstalled", "2026-06-20Z")
    db.commit()

    detail = customer_detail(db, "back.myshopify.com")
    assert [t["kind"] for t in detail["timeline"]] == \
        ["installed", "uninstalled", "reinstalled"]
    assert detail["install_count"] == 2
    # "reinstalled" is an event, not a state.
    assert detail["current_state"] == "installed"
    assert detail["timeline"][1]["note"] == "Back next season."
    assert detail["timeline"][1]["chose_to_leave"] is True


def test_detail_names_a_store_shopify_closed(db):
    _shop(db, "g1", shop_domain="dead.myshopify.com")
    _event(db, "g1", "RELATIONSHIP_INSTALLED", "installed", "2026-01-10Z")
    _event(db, "g1", "RELATIONSHIP_DEACTIVATED", "uninstalled", "2026-04-01Z")
    db.commit()

    detail = customer_detail(db, "dead.myshopify.com")
    last = detail["timeline"][-1]
    assert last["chose_to_leave"] is False
    assert last["label"] == "Store closed or frozen by Shopify"
    assert detail["current_state"] == "uninstalled"


def test_detail_totals_money_and_falls_back_to_the_transaction_interval(db):
    _shop(db, "g1", shop_domain="paid.myshopify.com")
    db.execute(
        "insert into subscriptions (id, shop_gid, monthly_amount, converted_at) "
        "values ('s1', 'g1', 15.83, '2026-06-21Z')")
    for tid, at, gross, net, interval, kind in (
        ("t1", "2026-06-21Z", "190.00", "184.49", "ANNUAL", "AppSubscriptionSale"),
        ("t2", "2026-07-01Z", "-19.00", "-19.00", None, "AppSaleCredit"),
    ):
        db.execute(
            "insert into transactions (id, type, created_at, shop_gid, gross_amount, "
            "shopify_fee, net_amount, billing_interval, currency_code) "
            "values (%s, %s, %s, 'g1', %s, 0, %s, %s, 'USD')",
            (tid, kind, at, gross, net, interval))
    db.commit()

    detail = customer_detail(db, "paid.myshopify.com")
    assert detail["money"]["gross"] == Decimal("171.00")
    assert detail["money"]["net"] == Decimal("165.49")
    assert detail["money"]["taken"] == Decimal("5.51")
    # No charges row exists, so the interval comes from the transaction feed --
    # without that fallback an annual plan renders as a cheap monthly one.
    assert detail["subscription"]["plan_interval"] == "ANNUAL"
    # Newest first.
    assert [p["id"] for p in detail["payments"]] == ["t2", "t1"]


def test_detail_is_none_for_an_unknown_shop(db):
    assert customer_detail(db, "nobody.myshopify.com") is None


def test_detail_never_selects_contact_details(db):
    """markdown_export renders this dict straight to a copyable document, so a
    contact column reaching it would leak on the next paste."""
    _shop(db, "g1", shop_domain="x.myshopify.com", owner_name="Jo Smith",
          email="jo@x.myshopify.com")
    db.commit()
    detail = customer_detail(db, "x.myshopify.com")
    assert "owner_name" not in detail["shop"]
    assert "email" not in detail["shop"]
