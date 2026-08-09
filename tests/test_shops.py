from partner_dashboard.shops import upsert_shop_state


def test_install_creates_shop_row(db):
    upsert_shop_state(db, "ai1", install_state="installed", at="2026-06-01T00:00:00Z")
    r = db.execute("select install_state, installed_at from shops "
                   "where shop_gid='ai1'").fetchone()
    assert r[0] == "installed" and r[1] is not None


def test_state_update_preserves_identity_fields(db):
    upsert_shop_state(db, "ai1", install_state="installed", at="2026-06-01T00:00:00Z")
    db.execute("update shops set email='m@shop.com', industry='Apparel', "
               "country='US' where shop_gid='ai1'"); db.commit()
    upsert_shop_state(db, "ai1", install_state="uninstalled", at="2026-06-10T00:00:00Z")
    r = db.execute("select email, industry, country, install_state, uninstalled_at "
                   "from shops where shop_gid='ai1'").fetchone()
    assert r[:3] == ("m@shop.com", "Apparel", "US")   # identity preserved
    assert r[3] == "uninstalled" and r[4] is not None
