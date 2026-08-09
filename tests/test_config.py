import os
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from partner_dashboard.config import Settings, get_settings

def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("PARTNER_API_TOKEN", "tok")
    monkeypatch.setenv("PARTNER_ORG_ID", "1")
    monkeypatch.setenv("PARTNER_APP_ID", "2")
    monkeypatch.setenv("DASHBOARD_USERS", "ada:pw,grace:pw2")
    s = Settings()
    assert s.database_url == "postgresql://x"
    assert s.poll_interval_minutes == 15
    assert s.poll_overlap_minutes == 60
    assert s.dashboard_users_map == {"ada": "pw", "grace": "pw2"}


# --- settings that had no test, so a regression to a hardcoded constant would
# --- have been invisible to the whole suite.

def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return get_settings()


def test_a_price_that_is_not_a_number_refuses_to_start(monkeypatch):
    """Unvalidated, this raised lazily inside the first poll that ingested a
    charge, stalling the sync hours after the deploy rather than at boot."""
    for bad in ("abc", "$190.00", "190.00 USD", "-190.00", "0", "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            _settings(monkeypatch, ANNUAL_PLAN_AMOUNTS=bad)


def test_a_thousands_separator_is_refused(monkeypatch):
    """The likeliest real typo: "1,900.00" silently parses as two prices, 1 and
    900, so a $1,900 annual plan stays monthly AND two junk amounts match."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, ANNUAL_PLAN_AMOUNTS="1,900.00")


def test_price_matching_ignores_decimal_scale(monkeypatch):
    """Decimal("190") == Decimal("190.00") and they hash equal, so frozenset
    membership is scale-insensitive. A charge of 190.0 matches an entry of 190."""
    s = _settings(monkeypatch, ANNUAL_PLAN_AMOUNTS="19, 49.0 ,")
    assert Decimal("19.00") in s.annual_plan_amounts_set
    assert Decimal("49.000") in s.annual_plan_amounts_set


def test_two_prices_written_with_cents_are_not_mistaken_for_one(monkeypatch):
    """The disambiguator: a left-hand side carrying cents cannot be the first
    group of a split number, so this must pass while "1,900.00" does not."""
    s = _settings(monkeypatch, ANNUAL_PLAN_AMOUNTS="190.00,490.00")
    assert s.annual_plan_amounts_set == {Decimal("190"), Decimal("490")}


def test_a_password_containing_a_comma_is_refused(monkeypatch):
    """"admin:pa,ssword" parsed as {"admin": "pa"} and logging in with "pa"
    worked. An operator who generated a random password got a 2-character one."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, DASHBOARD_USERS="admin:pa,ssword")


def test_the_activation_event_must_be_one_the_endpoint_accepts(monkeypatch):
    """Otherwise ingest 422s every event of that name and the funnel reports a
    confident 0% for merchants who did activate."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, USAGE_EVENT_TYPES="a,b",
                  USAGE_ACTIVATION_EVENT="not_in_list", USAGE_LIVE_EVENT="a")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, USAGE_EVENT_TYPES="a,b",
                  USAGE_ACTIVATION_EVENT="a", USAGE_LIVE_EVENT="not_in_list")


def test_usage_event_settings_are_actually_read(monkeypatch):
    s = _settings(monkeypatch, USAGE_EVENT_TYPES="campaign_created, campaign_view",
                  USAGE_ACTIVATION_EVENT="campaign_created",
                  USAGE_LIVE_EVENT="campaign_view")
    assert s.usage_event_types_set == {"campaign_created", "campaign_view"}
    assert s.usage_activation_event == "campaign_created"


def test_date_settings_are_read_not_hardcoded(monkeypatch):
    s = _settings(monkeypatch, ANNOTATIONS_EARLIEST="2015-06-01",
                  REASON_MANDATORY_FROM="2027-01-01", GA4_EARLIEST_DATA="2019-03-04")
    assert s.annotations_earliest == date(2015, 6, 1)
    assert s.reason_mandatory_from == date(2027, 1, 1)
    assert s.ga4_earliest_data == date(2019, 3, 4)


def test_an_empty_digest_timezone_becomes_utc(monkeypatch):
    """Empty is falsy, so APScheduler would fall back to the machine's local
    timezone and the digest would fire at an hour nobody chose."""
    assert _settings(monkeypatch, DIGEST_TIMEZONE="").digest_timezone == "UTC"


def test_a_published_example_credential_is_refused(monkeypatch):
    """Basic auth bypasses GOOGLE_ALLOWED_DOMAINS, and this repository is
    public, so any credential appearing in its documentation is the first thing
    anyone tries against a deployment."""
    for bad in ("admin:change-me", "user:pass", "admin:admin",
                "real:secret,admin:change-me"):
        with pytest.raises(ValidationError):
            _settings(monkeypatch, DASHBOARD_USERS=bad)
