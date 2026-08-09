from partner_dashboard.import_shops_csv import import_shops_csv

# A representative vendor-export header subset (real exports have 50+ columns;
# DictReader only needs the mapped ones present).
HEADER = "Name,Email,Shopify Domain,Country Code,Industry,Contact 1 Name\n"


def _seed_shop(db, gid="gid://partners/Shop/1", domain="x.myshopify.com", **cols):
    keys = ", ".join(["shop_gid", "shop_domain", "install_state", *cols])
    vals = [gid, domain, "installed", *cols.values()]
    ph = ", ".join(["%s"] * len(vals))
    db.execute(f"insert into shops({keys}) values ({ph})", vals); db.commit()


def test_import_fills_identity_fields_but_never_contact_details(db, tmp_path):
    """A vendor export's Email and "Contact 1 Name" are staff accounts on the
    shop, not the merchant: agencies, freelancers and the app's own team. They
    are not mapped, so a re-import cannot refill what migration 008 cleared."""
    _seed_shop(db)
    csv = tmp_path / "s.csv"
    csv.write_text(HEADER + "X Store,jane@x.com,x.myshopify.com,US,Apparel,Jane\n")
    assert import_shops_csv(db, str(csv)) == 1
    r = db.execute("select shop_name, industry, country, email, owner_name from shops "
                   "where shop_domain='x.myshopify.com'").fetchone()
    assert r == ("X Store", "Apparel", "US", None, None)


def test_import_is_idempotent(db, tmp_path):
    _seed_shop(db)
    csv = tmp_path / "s.csv"
    csv.write_text(HEADER + "X,jane@x.com,x.myshopify.com,US,Apparel,Jane\n")
    import_shops_csv(db, str(csv)); import_shops_csv(db, str(csv))
    n = db.execute("select count(*) from shops").fetchone()[0]
    assert n == 1


def test_import_skips_unmatched_domains(db, tmp_path):
    # Update-only: export rows with no derivation-created shop row are skipped
    # (vendor exports carry no Partner shop GID to insert with).
    csv = tmp_path / "s.csv"
    csv.write_text(HEADER + "N,jo@n.com,nobody.myshopify.com,US,Apparel,Jo\n")
    assert import_shops_csv(db, str(csv)) == 0
    assert db.execute("select count(*) from shops").fetchone()[0] == 0


def test_import_empty_cells_never_blank_existing_values(db, tmp_path):
    _seed_shop(db, email="keep@x.com", owner_name="Keep")
    csv = tmp_path / "s.csv"
    csv.write_text(HEADER + "X Store,,x.myshopify.com,US,Apparel,\n")
    assert import_shops_csv(db, str(csv)) == 1
    r = db.execute("select email, owner_name, country from shops "
                   "where shop_domain='x.myshopify.com'").fetchone()
    assert r == ("keep@x.com", "Keep", "US")
