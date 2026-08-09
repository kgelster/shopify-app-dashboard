"""The JSON export is the one artifact that leaves this app as a file.

Two things matter and neither is caught by the page tests: that it carries no
merchant contact details, and that "everything" is true rather than the top ten
of everything.
"""

import json
from datetime import datetime, timezone

from partner_dashboard import export
from partner_dashboard.config import get_settings
from partner_dashboard.markdown_export import CONTACT_KEYS


def _payload(db):
    return export.full_export(db, get_settings())


def _shops(db, count=3):
    """Real rows, so the contact-field sweep has something to find if the
    stripping ever stops working."""
    for n in range(count):
        db.execute(
            """insert into shops (shop_gid, shop_name, shop_domain, country,
                                  install_state, owner_name, email)
               values (%s, %s, %s, 'US', 'installed', %s, %s)""",
            (f"gid://shopify/Shop/{n}", f"Shop {n}", f"shop{n}.myshopify.com",
             f"Owner {n}", f"owner{n}@example.test"),
        )
    db.commit()


def test_every_section_is_present(db):
    """A missing key reads as zero to whatever consumes this, so the shape is
    fixed even on an empty database."""
    payload = _payload(db)
    assert set(payload) == {
        "meta", "definitions", "sync_health", "annotations", "overview",
        "customers", "actions", "funnel", "churn", "retention", "traffic", "faq",
    }


def test_it_serialises_without_a_custom_encoder_at_the_call_site(db):
    """Decimals and dates come straight out of psycopg. If render() did not
    handle them the route would 500 on the first shop with money."""
    text = export.render(db, get_settings())
    assert json.loads(text)["meta"]["source"]


def test_no_contact_details_anywhere_in_the_file(db):
    """The rule the markdown twins follow, applied to a file that gets saved,
    attached and forwarded. Asserted against the whole serialised document
    rather than the shop list, because a contact field could arrive through any
    section that grows a join later."""
    _shops(db)
    text = export.render(db, get_settings())
    for key in CONTACT_KEYS:
        assert f'"{key}"' not in text


def test_definitions_travel_with_the_numbers(db):
    """A file opened a year from now has no dashboard beside it, so it has to
    say what each number counted."""
    from partner_dashboard.metrics import METRICS
    payload = _payload(db)
    assert set(payload["definitions"]) == set(METRICS)
    assert payload["definitions"]["active_mrr"]["rule"] == METRICS["active_mrr"].rule


def test_the_limits_it_used_are_written_into_the_file(db):
    """Every section has a cap somewhere. A reader has to be able to tell a real
    end from a ceiling, so the ceilings are stated rather than implied."""
    assert _payload(db)["meta"]["windows"] == export.LIMITS


def test_customers_is_not_a_page_of_customers(db):
    """The page paginates at 50 and the markdown twin caps at 1000. Both are
    display decisions; an archive that inherited them would be wrong."""
    _shops(db)
    from partner_dashboard.customers import count_customers
    payload = _payload(db)
    assert payload["customers"]["total"] == count_customers(db)
    assert len(payload["customers"]["shops"]) == payload["customers"]["total"]


def test_unknown_is_null_and_says_why_rather_than_reading_as_zero(db):
    """With no usage events, "no paying shop has gone quiet" is a much better
    story than the truth, which is that nothing can tell."""
    payload = _payload(db)
    assert payload["actions"]["at_risk"] is None
    assert "unknown" in payload["actions"]["at_risk_note"]
    assert payload["funnel"]["activation"]["time_to_activation"] is None
    assert "unknown rather than zero" in payload["funnel"]["activation"]["note"]


def test_the_caveats_that_live_on_the_page_travel_with_the_data(db):
    """A model that does not know deactivations are folded into uninstalls will
    confidently report the wrong churn number."""
    about = _payload(db)["meta"]["about"]
    assert "one twelfth" in about
    assert "closed or froze" in about


def test_generated_at_is_the_time_it_was_asked_for(db):
    stamped = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    payload = export.full_export(db, get_settings(), now=stamped)
    assert payload["meta"]["generated_at"].startswith("2026-03-01T12:00:00")
    assert export.filename(stamped, slug="example-app") == "example-app-2026-03-01.json"


def test_export_filename_slug_comes_from_the_app_name(monkeypatch):
    """Two operators' downloads land in the same folder as often as not, so the
    name has to say whose dashboard it came from as well as which day."""
    monkeypatch.setenv("APP_NAME", "Acme Gift & Wrap")
    assert get_settings().slug == "acme-gift-wrap"
    monkeypatch.setenv("APP_SLUG", "acme")
    get_settings.cache_clear()
    assert get_settings().slug == "acme"
