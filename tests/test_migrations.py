def test_core_tables_exist(db):
    cur = db.execute("""
        select table_name from information_schema.tables
        where table_schema='public' order by table_name
    """)
    names = {r[0] for r in cur.fetchall()}
    assert {"raw_app_events","charges","app_events","subscriptions",
            "shops","tracking_events","sync_state","schema_migrations"} <= names

def test_migrations_are_idempotent(db):
    from partner_dashboard.db import run_migrations
    run_migrations(db)   # second run must not raise
