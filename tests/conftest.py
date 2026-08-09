import os

import pytest

# Settings has required fields, and partner_dashboard.web builds the app at import time, so
# without these pytest fails during *collection* rather than in a test. Set
# before any partner_dashboard module is imported: conftest runs first, which is the whole
# reason this block lives here and not in a fixture.
#
# These are deliberately assigned, not setdefault-ed. A developer with a real
# .env next to pyproject.toml would otherwise have their live Partner token and
# session secret read straight into the suite, because pydantic-settings lets
# the environment win over .env. DATABASE_URL is the one exception: point it
# wherever your test database actually is.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/partner_dashboard_test")
os.environ.update({
    "PARTNER_API_TOKEN": "test-token",
    "PARTNER_ORG_ID": "1",
    "PARTNER_APP_ID": "1",
    "DASHBOARD_USERS": "tester:suite-only-credential",
    "PUBLIC_BASE_URL": "http://localhost:8000",
    "GOOGLE_ALLOWED_DOMAINS": "example.com,example.org",
    "SESSION_SECRET": "test-session-secret-not-the-default",
    # The suite's fixture pricing: $19/month and $190/year. Tests that exercise
    # the inference itself override this, which is the point -- an annual price
    # the operator forgets to list is counted as monthly, at 12x its real MRR.
    "ANNUAL_PLAN_AMOUNTS": "190.00",
    # Never start APScheduler under test: two schedulers means duplicate polls.
    "NO_SCHEDULER": "1",
})

from partner_dashboard.config import get_settings  # noqa: E402
from partner_dashboard.db import connect, run_migrations  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # Settings is lru_cached, so one test's monkeypatched env would otherwise
    # be read by the next one.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db():
    conn = connect()
    run_migrations(conn)
    tables = [
        row[0]
        for row in conn.execute(
            """
            select table_name from information_schema.tables
            where table_schema='public' and table_name != 'schema_migrations'
            """
        ).fetchall()
    ]
    if tables:
        conn.execute(f"truncate {', '.join(tables)} restart identity cascade")
    yield conn
    conn.close()
