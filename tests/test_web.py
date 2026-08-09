import json
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from partner_dashboard.auth import SESSION_COOKIE, issue_session
from partner_dashboard.web import create_app

# 32+ chars: create_app refuses a short secret on a non-local host.
SESSION_SECRET = "test-session-secret-long-enough-to-pass"


@pytest.fixture(autouse=True)
def ppa_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost:5432/partner_dashboard_test")
    monkeypatch.setenv("PARTNER_API_TOKEN", "x")
    monkeypatch.setenv("PARTNER_ORG_ID", "1")
    monkeypatch.setenv("PARTNER_APP_ID", "2")
    monkeypatch.setenv("DASHBOARD_USERS", "tester:suite-only-credential")
    monkeypatch.setenv("NO_SCHEDULER", "1")
    # Pin the SSO config rather than inheriting whatever the developer's .env
    # holds: with real client credentials present these tests take the Google
    # redirect path, without them the Basic-auth path, and the assertions
    # differ. Explicit env vars win over the .env file.
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://dash.test")
    # Distinctive on purpose: the leak assertions below check that a guarded
    # page's name does not appear where it should not, and a name that never
    # renders anywhere would make those assertions vacuously true.
    monkeypatch.setenv("APP_NAME", "Zarquon Widgets")


def keep_open(conn):
    """Routes close the connection they were handed; the shared test connection
    has to survive more than one request per test."""
    class NoClose:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass
    return NoClose()


def test_healthz_open(db):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    assert c.get("/healthz").status_code == 200


@pytest.mark.parametrize("path", ["/", "/customers", "/reports/funnel",
                                  "/reports/retention", "/reports/traffic"])
def test_pages_bounce_anonymous_browsers_to_google(db, path):
    """No page content without auth. A browser (no Authorization header) is
    redirected into the Google flow rather than being shown a Basic prompt."""
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get(path, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/auth/login"
    # The body of a redirect must not leak the page it was guarding. The
    # wordmark and <title> carry the app name on every rendered page, so its
    # absence is what says no page was rendered into this body.
    assert "Zarquon Widgets" not in r.text


def test_pages_render_for_basic_auth(db):
    # Real factory: each route opens and closes its own connection, so a shared
    # one would be closed out from under the second request.
    from partner_dashboard.db import connect
    app = create_app(conn_factory=connect)
    c = TestClient(app)
    assert c.get("/", auth=("tester", "suite-only-credential")).status_code == 200
    assert c.get("/customers", auth=("tester", "suite-only-credential")).status_code == 200


def test_wrong_basic_password_is_401_not_a_google_redirect(db):
    """A supplied-but-wrong credential is a failure, not a reason to start an
    interactive login: scripts must see 401, never a 307 loop."""
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get("/", auth=("tester", "wrong"), follow_redirects=False)
    assert r.status_code == 401
    assert "Currently installed" not in r.text


def test_basic_auth_only_when_sso_is_not_configured(db, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Basic"


def test_signed_session_cookie_authenticates(db):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE,
                  issue_session(SESSION_SECRET, "ada@example.com", "Ada Lovelace"))
    r = c.get("/")
    assert r.status_code == 200
    # The header greets you by name; the address it was derived from stays off
    # the page, where it was eating a third of the nav bar.
    assert "Ada Lovelace" in r.text
    assert "ada@example.com" not in r.text


@pytest.mark.parametrize("cookie", [
    "not-a-real-token",
    issue_session("some-other-secret", "ada@example.com"),   # wrong signing key
    issue_session(SESSION_SECRET, "stranger@gmail.com"),         # domain not allowed
])
def test_forged_or_out_of_domain_cookies_do_not_authenticate(db, cookie):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, cookie)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/auth/login"


def test_logout_clears_the_session_cookie(db):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, issue_session(SESSION_SECRET, "ada@example.com"))
    r = c.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"
    # Assert on the Set-Cookie the browser actually receives; the test client's
    # cookie jar keys on domain and would not match a jar entry set by hand.
    cleared = r.headers["set-cookie"]
    assert cleared.startswith(f'{SESSION_COOKIE}=""')
    assert "Max-Age=0" in cleared


def test_oauth_callback_rejects_a_mismatched_state(db):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get("/auth/callback", params={"code": "x", "state": "forged"},
              follow_redirects=False)
    assert r.status_code == 400


def test_report_pages_render(db):
    # real factory: report routes open AND close their own connection each hit
    from partner_dashboard.db import connect
    app = create_app(conn_factory=connect)
    c = TestClient(app)
    for name, marker in (("funnel", "Lifecycle funnel"), ("traffic", "No traffic data yet"),
                         ("retention", "Retention")):
        r = c.get(f"/reports/{name}", auth=("tester", "suite-only-credential"))
        assert r.status_code == 200
        assert marker in r.text


def test_customers_route_closes_the_connection_it_opened(db):
    # conn_factory in production is partner_dashboard.db.connect: a fresh connection per
    # call. The route must close what it opens or every dashboard hit leaks
    # a Postgres connection. Wrap (don't replace) `db` so the shared test
    # connection stays open for the rest of the suite/fixture teardown.
    class TrackClose:
        def __init__(self, inner):
            self._inner = inner
            self.closed = False

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            self.closed = True

    tracked = TrackClose(db)
    app = create_app(conn_factory=lambda: tracked)
    c = TestClient(app)
    r = c.get("/customers", auth=("tester", "suite-only-credential"))
    assert r.status_code == 200
    assert tracked.closed is True


def test_customers_pages_at_50_and_keeps_filters_on_the_next_link(db):
    rows = ",".join(
        f"('gid{i:03d}','Shop {i:03d}','US','installed')" for i in range(120)
    )
    db.execute(
        f"insert into shops(shop_gid,shop_name,country,install_state) values {rows}"
    )
    db.commit()
    app = create_app(conn_factory=lambda: keep_open(db))
    c = TestClient(app)

    first = c.get("/customers", params={"country": "US"}, auth=("tester", "suite-only-credential"))
    assert first.status_code == 200
    assert "1&ndash;50 of 120" in first.text
    assert "country=US&amp;page=2" in first.text

    last = c.get("/customers", params={"country": "US", "page": 3}, auth=("tester", "suite-only-credential"))
    assert "101&ndash;120 of 120" in last.text
    # No next link on the final page.
    assert "page=4" not in last.text


def test_customers_page_number_is_clamped_to_the_real_range(db):
    db.execute("insert into shops(shop_gid,shop_name,install_state) "
               "values ('g1','Only Shop','installed')")
    db.commit()
    app = create_app(conn_factory=lambda: keep_open(db))
    c = TestClient(app)
    for page in ("999", "0", "-4"):
        r = c.get("/customers", params={"page": page}, auth=("tester", "suite-only-credential"))
        assert r.status_code == 200
        assert "Only Shop" in r.text


def test_customers_filters_render_results(db):
    db.execute(
        "insert into shops(shop_gid,shop_name,industry,country,email,install_state) "
        "values ('ai1','Test Shop','Apparel','US','a@x.com','installed')"
    )
    db.commit()
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get("/customers", params={"industry": "Apparel"}, auth=("tester", "suite-only-credential"))
    assert r.status_code == 200
    assert "Test Shop" in r.text


MD_PATHS = ["/index.md", "/customers.md", "/actions.md", "/reports/funnel.md",
            "/reports/churn.md", "/reports/retention.md", "/reports/traffic.md"]


@pytest.mark.parametrize("path", MD_PATHS)
def test_markdown_mirrors_render_with_frontmatter(db, path):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get(path, auth=("tester", "suite-only-credential"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("---\n")
    assert "source_url:" in r.text and f"{path}'" in r.text
    assert "## How to read this" in r.text


@pytest.mark.parametrize("path", MD_PATHS)
def test_markdown_mirrors_are_behind_the_same_auth_as_the_pages(db, path):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get(path, follow_redirects=False)
    assert r.status_code == 307
    assert "source_url" not in r.text


def test_unknown_markdown_slug_is_a_404_not_a_render(db):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    assert c.get("/etc-passwd.md", auth=("tester", "suite-only-credential")).status_code == 404
    assert c.get("/reports/nope.md", auth=("tester", "suite-only-credential")).status_code == 404


def test_login_is_a_page_that_explains_the_dashboard(db):
    """It used to bounce straight to Google, so nobody ever read what they
    were signing in to, and a disallowed account's first words were a 403."""
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get("/auth/login", follow_redirects=False)
    assert r.status_code == 200
    assert "For internal use only" in r.text
    assert "/auth/google" in r.text          # the redirect moved behind a button
    # The art is the page background, not an <img> in the panel.
    assert 'class="cover"' in r.text
    assert "url('/static/login.webp')" in r.text
    assert c.get("/static/login.webp").status_code == 200
    # No sidebar on an unauthenticated page: every link in it would 307 back
    # here.
    assert 'class="sidebar"' not in r.text
    # The page is public, so it must not describe the auth model. An earlier
    # version named the allowed domains, the sync interval, and how the address
    # check works.
    for leak in ("example.com", "example.org", "Basic", "allowlist",
                 "refreshed every"):
        assert leak not in r.text, leak


def test_google_redirect_moved_to_its_own_route(db):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get("/auth/google", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].startswith("https://accounts.google.com/")
    assert "dashboard_oauth_state" in r.headers["set-cookie"]


def test_nothing_here_is_indexable(db):
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    robots = c.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /" in robots.text
    # The header covers what has no <head>: the .md twins, the illustrations,
    # a JSON error body.
    for path in ("/robots.txt", "/auth/login", "/healthz"):
        assert c.get(path).headers["x-robots-tag"] == "noindex, nofollow"
    assert '<meta name="robots" content="noindex, nofollow">' in \
        c.get("/auth/login").text


def test_error_pages_carry_their_illustration(db):
    """401 and 403 were JSON too. 403 especially is a human moment: someone
    signed in with the wrong Google account."""
    app = create_app(conn_factory=lambda: keep_open(db))
    c = TestClient(app)
    accept_html = {"Accept": "text/html"}

    unauthorized = c.get("/", auth=("tester", "wrong"), headers=accept_html,
                         follow_redirects=False)
    assert unauthorized.status_code == 401
    assert "url('/static/error-401.webp')" in unauthorized.text
    # Dropping this would stop curl -u being able to authenticate at all.
    assert unauthorized.headers["www-authenticate"] == "Basic"

    # The image itself is served, and is the page background rather than an
    # <img>, so there is no alt text to get wrong.
    assert c.get("/static/error-401.webp").status_code == 200
    assert 'class="cover"' in unauthorized.text


def test_wrong_google_account_gets_a_page_not_a_json_object(db, monkeypatch):
    """The 403 is the most human of these: someone signed in successfully and
    is being turned away. It used to answer with a JSON object."""
    monkeypatch.setattr("partner_dashboard.web.exchange_code",
                        lambda *a, **k: ("someone@gmail.com", "Someone"))
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    c.cookies.set("dashboard_oauth_state", "s7")
    r = c.get("/auth/callback", params={"code": "x", "state": "s7"},
              headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 403
    assert "Not on the list" in r.text
    assert "url('/static/error-403.webp')" in r.text
    assert "Try another account" in r.text
    # It names the address that was refused, so a teammate knows which account
    # they used, and nothing else. It used to list the allowed domains, which
    # handed the targets to whoever just failed to get in.
    assert "someone@gmail.com" in r.text
    assert "example.com" not in r.text
    assert "example.org" not in r.text


def test_a_signed_in_reader_keeps_the_sidebar_on_an_error(db):
    """A 404 is a place to navigate away from, and for them the links work."""
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE,
                  issue_session(SESSION_SECRET, "ada@example.com", "Ada Lovelace"))
    signed_in = c.get("/nope", headers={"Accept": "text/html"})
    assert signed_in.status_code == 404
    assert 'class="sidebar"' in signed_in.text
    assert "Back to Overview" in signed_in.text

    c.cookies.clear()
    signed_out = c.get("/nope", headers={"Accept": "text/html"})
    assert 'class="sidebar"' not in signed_out.text
    assert "Go to sign-in" in signed_out.text


def test_footer_reports_the_render_time(db):
    """It reads request.state.started, which the security middleware stamps.
    If that ever stops being set the footer empties silently, so pin it."""
    app = create_app(conn_factory=lambda: keep_open(db))
    c = TestClient(app)
    for path in ("/", "/reports/churn"):
        body = c.get(path, auth=("tester", "suite-only-credential")).text
        assert re.search(r"Rendered in \d+(\.\d)? ms", body), path
    # Including the 404, which renders the same shell.
    assert re.search(r"Rendered in \d+(\.\d)? ms",
                     c.get("/nope", auth=("tester", "suite-only-credential"),
                           headers={"Accept": "text/html"}).text)


def test_browser_404_renders_the_page_and_others_keep_json(db):
    """A browser gets the app's own chrome; anything parsing a response does
    not, so the .md twins and curl see the JSON body they always saw."""
    # keep_open: this test makes more than one request, and the customer route
    # closes the connection it was handed.
    app = create_app(conn_factory=lambda: keep_open(db))
    c = TestClient(app)
    accept_html = {"Accept": "text/html,application/xhtml+xml"}

    missing_shop = c.get("/customers/nope.myshopify.com", auth=("tester", "suite-only-credential"),
                         headers=accept_html)
    assert missing_shop.status_code == 404
    # Unescaped because Jinja autoescaping renders the apostrophe as &#39;.
    assert "That shop isn't on record" in unescape(missing_shop.text)
    assert "Back to Customers" in missing_shop.text
    # No Copy MD: there is no markdown twin of a 404 to copy.
    assert 'id="copy-md"' not in missing_shop.text

    generic = c.get("/no-such-route", auth=("tester", "suite-only-credential"), headers=accept_html)
    assert generic.status_code == 404
    assert "Page not found" in generic.text

    # Default TestClient Accept is */*, which is what curl and the Copy MD
    # fetch send.
    as_json = c.get("/customers/nope.myshopify.com", auth=("tester", "suite-only-credential"))
    assert as_json.status_code == 404
    assert as_json.json() == {"detail": "No such shop"}


def test_404_page_carries_the_csp_nonce(db):
    """It extends base.html, so its inline scripts need the nonce like every
    other page. A mismatch here is silent in the status code."""
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get("/no-such-route", auth=("tester", "suite-only-credential"),
              headers={"Accept": "text/html"})
    nonce = r.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in r.text


def test_non_404_errors_keep_their_shape(db):
    """The handler is registered for every HTTPException, so the redirect to
    /auth/login has to survive a browser Accept header."""
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    r = c.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/auth/login"


def test_customers_markdown_carries_shops_but_no_contact_details(db):
    db.execute(
        "insert into shops (shop_gid, shop_name, shop_domain, owner_name, email, "
        "country, install_state) values "
        "('g1','Visible Shop','visible.myshopify.com','Jane Merchant',"
        "'jane@visible.example','US','installed')")
    db.commit()
    app = create_app(conn_factory=lambda: db)
    c = TestClient(app)
    text = c.get("/customers.md", auth=("tester", "suite-only-credential")).text
    assert "Visible Shop" in text and "visible.myshopify.com" in text
    # The page shows these; the exportable document must not.
    assert "jane@visible.example" not in text
    assert "Jane Merchant" not in text


def test_actions_carries_no_contact_details_on_either_surface(db):
    """A vendor export's contact columns were every staff account on the shop, agencies
    and our own team included, so the review sheet was naming the wrong people
    entirely. Neither the page nor its .md twin carries them now, and the shop
    still identifies itself by name and domain."""
    db.execute(
        "insert into shops (shop_gid, shop_name, shop_domain, owner_name, email, "
        "install_state) values ('g1','Askable','askable.myshopify.com',"
        "'Jane Merchant','jane@askable.example','installed')")
    db.execute(
        "insert into subscriptions (id, shop_gid, monthly_amount, converted_at) "
        "values ('c1','g1',19.00, now() - interval '90 days')")
    db.commit()
    c = TestClient(create_app(conn_factory=lambda: keep_open(db)))

    for path in ("/actions", "/actions.md"):
        body = c.get(path, auth=("tester", "suite-only-credential")).text
        assert "askable.myshopify.com" in body    # the shop still identifies itself
        assert "Jane Merchant" not in body
        assert "jane@askable.example" not in body


# --- POST /ingest/usage ----------------------------------------------------

USAGE_TOKEN = "ingest-token-for-tests"


def _usage_body(**over):
    event = {"event_id": "e1", "shop_gid": "gid://shopify/Shop/1",
             "event_type": "offer_created", "occurred_at": "2026-08-07T11:00:00Z"}
    event.update(over)
    return {"events": [event]}


@pytest.fixture
def ingest_client(db, monkeypatch):
    monkeypatch.setenv("USAGE_INGEST_TOKEN", USAGE_TOKEN)
    return TestClient(create_app(conn_factory=lambda: keep_open(db)))


def test_ingest_stores_a_batch_with_the_token(ingest_client, db):
    r = ingest_client.post("/ingest/usage", json=_usage_body(),
                           headers={"X-Usage-Token": USAGE_TOKEN})
    assert r.status_code == 200
    assert r.json()["stored"] == 1
    assert db.execute("select count(*) from usage_events").fetchone()[0] == 1


@pytest.mark.parametrize("headers", [
    {},                                    # no token at all
    {"X-Usage-Token": ""},                 # empty token
    {"X-Usage-Token": "wrong"},
    {"X-Usage-Token": USAGE_TOKEN + "x"},  # prefix of the real token
])
def test_ingest_refuses_every_wrong_token_identically(ingest_client, db, headers):
    r = ingest_client.post("/ingest/usage", json=_usage_body(), headers=headers)
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}
    assert db.execute("select count(*) from usage_events").fetchone()[0] == 0


def test_ingest_refuses_everything_when_no_token_is_configured(db, monkeypatch):
    """An unconfigured server must look exactly like a wrong token, so probing
    cannot tell the two apart."""
    monkeypatch.delenv("USAGE_INGEST_TOKEN", raising=False)
    c = TestClient(create_app(conn_factory=lambda: keep_open(db)))
    r = c.post("/ingest/usage", json=_usage_body(), headers={"X-Usage-Token": "anything"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}


def test_ingest_does_not_accept_a_dashboard_session_or_basic_auth(ingest_client):
    """The ingest secret is the only key to this door: a signed-in human, or
    anyone holding dashboard credentials, still cannot write events."""
    r = ingest_client.post("/ingest/usage", json=_usage_body(), auth=("tester", "suite-only-credential"))
    assert r.status_code == 401


def test_ingest_rejects_an_oversized_body_before_parsing_it(ingest_client, db):
    from partner_dashboard.usage import MAX_BODY_BYTES
    r = ingest_client.post(
        "/ingest/usage",
        content=b'{"events": [' + b"x" * (MAX_BODY_BYTES + 1024) + b"]}",
        headers={"X-Usage-Token": USAGE_TOKEN, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert db.execute("select count(*) from usage_events").fetchone()[0] == 0


def test_ingest_rejects_an_unknown_event_type(ingest_client, db):
    r = ingest_client.post("/ingest/usage", json=_usage_body(event_type="drop_table"),
                           headers={"X-Usage-Token": USAGE_TOKEN})
    assert r.status_code == 422
    assert db.execute("select count(*) from usage_events").fetchone()[0] == 0


def test_ingest_is_safe_to_retry(ingest_client):
    headers = {"X-Usage-Token": USAGE_TOKEN}
    first = ingest_client.post("/ingest/usage", json=_usage_body(), headers=headers).json()
    second = ingest_client.post("/ingest/usage", json=_usage_body(), headers=headers).json()
    assert first["stored"] == 1
    assert second["stored"] == 0 and second["duplicates"] == 1


def test_event_properties_render_escaped_if_they_ever_reach_a_page(ingest_client, db):
    """Nothing renders raw `properties` today, and the pages that would are
    Jinja-autoescaped. This pins that: a script tag pushed through the ingest
    endpoint cannot come back as markup."""
    ingest_client.post(
        "/ingest/usage",
        json=_usage_body(properties={"offer_name": "<script>alert(1)</script>"}),
        headers={"X-Usage-Token": USAGE_TOKEN})
    r = ingest_client.get("/reports/funnel", auth=("tester", "suite-only-credential"))
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text


def test_funnel_shows_an_honest_empty_state_before_any_usage_data(db):
    c = TestClient(create_app(conn_factory=lambda: keep_open(db)))
    r = c.get("/reports/funnel", auth=("tester", "suite-only-credential"))
    assert "Nothing has arrived yet" in r.text
    # Zero activation would be a claim; "unknown" is the truth, so the stat
    # tiles must not render at all.
    assert "Built an offer" not in r.text
    assert "Median time to first offer" not in r.text


def test_customer_detail_page_and_its_markdown_twin(db):
    """The .md route is registered before the HTML one on purpose: {shop_domain}
    compiles to a greedy [^/]+, so /customers/x.myshopify.com.md would otherwise
    be served as a shop named "x.myshopify.com.md"."""
    db.execute("insert into shops (shop_gid, shop_domain, shop_name, install_state, "
               "owner_name, email) values ('g1', 'x.myshopify.com', 'Ex', 'installed', "
               "'Jo Smith', 'jo@example.com')")
    db.execute("insert into raw_app_events (id, type, occurred_at, shop_gid, payload) "
               "values ('e1', 'RELATIONSHIP_INSTALLED', '2026-01-10Z', 'g1', '{}')")
    db.execute("insert into app_events (platform_event_id, type, occurred_at, shop_gid) "
               "values ('e1', 'installed', '2026-01-10Z', 'g1')")
    db.commit()

    c = TestClient(create_app(conn_factory=lambda: keep_open(db)))
    page = c.get("/customers/x.myshopify.com", auth=("tester", "suite-only-credential"))
    assert page.status_code == 200
    assert "Ex" in page.text

    md = c.get("/customers/x.myshopify.com.md", auth=("tester", "suite-only-credential"))
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "x.myshopify.com" in md.text
    # Same no-contact-details rule as every other .md document here.
    assert "Jo Smith" not in md.text
    assert "jo@example.com" not in md.text

    assert c.get("/customers/nope.myshopify.com", auth=("tester", "suite-only-credential")).status_code == 404


def test_customer_detail_needs_auth(db):
    c = TestClient(create_app(conn_factory=lambda: db))
    r = c.get("/customers/x.myshopify.com", follow_redirects=False)
    assert r.status_code == 307


# --- Definitions, deltas, and the FAQ ----------------------------------------

def _signed_in():
    """A client with a real session cookie. The annotation write path needs one
    specifically, so the whole block uses it rather than Basic auth."""
    from partner_dashboard.db import connect
    app = create_app(conn_factory=connect)
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE,
                  issue_session(SESSION_SECRET, "ada@example.com", "Ada Lovelace"))
    return c


def test_overview_carries_each_tile_definition(db):
    """A number with no definition beside it is the thing metrics.py exists to
    make impossible, so the page has to actually render them."""
    from partner_dashboard.metrics import METRICS
    r = _signed_in().get("/")
    assert r.status_code == 200
    body = unescape(r.text)
    for key in ("installed", "active_mrr", "paying", "net_30d"):
        assert METRICS[key].definition in body
        assert METRICS[key].source in body


def test_overview_compares_every_headline_tile(db):
    r = _signed_in().get("/")
    # Six tiles, each with its own delta line, and each saying what it is
    # against rather than leaving the reader to assume.
    assert r.text.count('class="delta-num"') == 6
    assert "vs 30 days ago" in r.text
    assert "vs prior 30 days" in r.text


def test_faq_renders_and_has_a_markdown_twin(db):
    from partner_dashboard.faq import FAQ
    c = _signed_in()
    page = c.get("/faq")
    assert page.status_code == 200
    twin = c.get("/faq.md")
    assert twin.status_code == 200
    assert twin.headers["content-type"].startswith("text/markdown")
    for question, paragraphs in FAQ:
        assert question in unescape(page.text)
        assert question in twin.text
        assert paragraphs[0] in twin.text


def test_the_tips_panel_links_to_the_faq(db):
    assert 'href="/faq"' in _signed_in().get("/").text


# --- Annotations --------------------------------------------------------------

def test_a_note_is_written_and_shown(db):
    c = _signed_in()
    posted = c.post("/annotations",
                    data={"on_date": "2026-03-01", "note": "Raised the price to $19"},
                    follow_redirects=False)
    assert posted.status_code == 303
    body = unescape(c.get("/").text)
    assert "Raised the price to $19" in body
    assert "ada@example.com" in body


def test_a_note_marks_its_month_on_the_charts(db):
    c = _signed_in()
    c.post("/annotations", data={"on_date": "2026-03-01", "note": "price change"})
    assert 'class="anno-dot"' in c.get("/?months=24").text


def test_the_author_comes_from_the_session_not_the_form(db):
    """A field the browser supplies is a field anyone can set."""
    c = _signed_in()
    c.post("/annotations", data={"on_date": "2026-03-01", "note": "n",
                                 "author": "someone-else@evil.test"})
    from partner_dashboard import annotations as anno
    from partner_dashboard.db import connect
    conn = connect()
    try:
        assert anno.recent(conn)[0]["author"] == "ada@example.com"
    finally:
        conn.close()


def test_a_bad_note_comes_back_with_a_message_rather_than_a_500(db):
    c = _signed_in()
    r = c.post("/annotations", data={"on_date": "not-a-date", "note": "x"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "note_error" in r.headers["location"]
    assert "YYYY-MM-DD" in unescape(c.get(r.headers["location"]).text)


def test_basic_auth_cannot_write_a_note(db):
    """The session cookie is SameSite=lax, so a form on another origin cannot
    make a browser submit here carrying it. Basic auth has no such property: a
    browser with cached credentials would send them cross-site, which would turn
    this route into a CSRF hole the moment it accepted them."""
    from partner_dashboard.db import connect
    c = TestClient(create_app(conn_factory=connect))
    r = c.post("/annotations", auth=("tester", "suite-only-credential"),
               data={"on_date": "2026-03-01", "note": "x"},
               follow_redirects=False)
    assert r.status_code == 403
    from partner_dashboard import annotations as anno
    conn = connect()
    try:
        assert anno.recent(conn) == []
    finally:
        conn.close()


def test_an_anonymous_post_never_reaches_the_database(db):
    from partner_dashboard.db import connect
    c = TestClient(create_app(conn_factory=connect))
    r = c.post("/annotations", data={"on_date": "2026-03-01", "note": "x"},
               follow_redirects=False)
    # 303, not 307: a method-preserving redirect would make the browser re-POST
    # the note to the GET-only login page, losing the text on a 405.
    assert r.status_code in (303, 401, 403)
    from partner_dashboard import annotations as anno
    conn = connect()
    try:
        assert anno.recent(conn) == []
    finally:
        conn.close()


def test_a_note_cannot_inject_markup_into_the_page(db):
    """The first route that stores something a person typed, so the two places
    it comes back out both get checked.

    The page is Jinja-autoescaped. The markdown twin is not, and does not need
    to be: it json-encodes the note into a fenced block, and is served as
    text/markdown with nosniff, so a browser never parses it as HTML. That is
    the actual protection, so that is what is asserted -- testing for escaping
    the twin does not do would have meant weakening the twin to satisfy a test.
    """
    c = _signed_in()
    c.post("/annotations", data={"on_date": "2026-03-01",
                                 "note": "<script>alert(1)</script>"})
    page = c.get("/")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text

    twin = c.get("/index.md")
    assert twin.headers["content-type"].startswith("text/markdown")
    assert twin.headers["x-content-type-options"] == "nosniff"


def _only_note_id():
    from partner_dashboard import annotations as anno
    from partner_dashboard.db import connect
    conn = connect()
    try:
        rows = anno.recent(conn)
        return rows[0]["id"] if rows else None
    finally:
        conn.close()


def test_a_note_can_be_deleted_from_the_page(db):
    c = _signed_in()
    c.post("/annotations", data={"on_date": "2026-03-01", "note": "wrong on purpose"})
    assert "wrong on purpose" in unescape(c.get("/").text)

    r = c.post("/annotations/delete", data={"id": _only_note_id()},
               follow_redirects=False)
    assert r.status_code == 303
    assert "note_error" not in r.headers["location"]
    assert "wrong on purpose" not in unescape(c.get("/").text)
    assert _only_note_id() is None


def test_deleting_a_note_takes_its_chart_marker_with_it(db):
    """A dot that outlives the note explaining it is worse than no dot."""
    c = _signed_in()
    c.post("/annotations", data={"on_date": "2026-03-01", "note": "price change"})
    assert 'class="anno-dot"' in c.get("/?months=24").text
    c.post("/annotations/delete", data={"id": _only_note_id()})
    assert 'class="anno-dot"' not in c.get("/?months=24").text


def test_basic_auth_cannot_delete_a_note(db):
    """Same reasoning as the write path, with more at stake: Basic credentials
    travel cross-site, so accepting them here would let another origin's form
    clear the table."""
    from partner_dashboard.db import connect
    c = _signed_in()
    c.post("/annotations", data={"on_date": "2026-03-01", "note": "keep me"})
    note_id = _only_note_id()

    anon = TestClient(create_app(conn_factory=connect))
    r = anon.post("/annotations/delete", auth=("tester", "suite-only-credential"), data={"id": note_id},
                  follow_redirects=False)
    assert r.status_code == 403
    assert _only_note_id() == note_id


def test_an_anonymous_delete_never_reaches_the_database(db):
    from partner_dashboard.db import connect
    c = _signed_in()
    c.post("/annotations", data={"on_date": "2026-03-01", "note": "keep me"})
    note_id = _only_note_id()

    anon = TestClient(create_app(conn_factory=connect))
    r = anon.post("/annotations/delete", data={"id": note_id},
                  follow_redirects=False)
    assert r.status_code in (303, 401, 403)
    assert _only_note_id() == note_id


def test_deleting_an_id_that_is_gone_says_so_rather_than_500ing(db):
    c = _signed_in()
    r = c.post("/annotations/delete", data={"id": 999999}, follow_redirects=False)
    assert r.status_code == 303
    assert "note_error" in r.headers["location"]
    assert "already gone" in unescape(c.get(r.headers["location"]).text)


def test_a_junk_id_comes_back_with_a_message(db):
    c = _signed_in()
    r = c.post("/annotations/delete", data={"id": "seven"}, follow_redirects=False)
    assert r.status_code == 303
    assert "note_error" in r.headers["location"]


def test_the_delete_control_is_hidden_from_a_reader_who_cannot_use_it(db):
    """Basic auth can read the page but not write, so showing it a Delete
    button would be offering a control that 403s."""
    from partner_dashboard.db import connect
    _signed_in().post("/annotations",
                      data={"on_date": "2026-03-01", "note": "visible to all"})
    basic = TestClient(create_app(conn_factory=connect))
    page = basic.get("/", auth=("tester", "suite-only-credential"))
    assert "visible to all" in unescape(page.text)
    assert 'class="anno-del"' not in page.text
    assert 'class="anno-del"' in _signed_in().get("/").text


# --- JSON export --------------------------------------------------------------

def test_the_export_downloads_as_a_dated_file(db):
    r = _signed_in().get("/export.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"].startswith("attachment; filename=")
    assert ".json" in r.headers["content-disposition"]
    # Merchant-typed strings ride in this file, so the browser must never be
    # allowed to guess it is anything but JSON.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert json.loads(r.text)["meta"]["windows"]


def test_the_download_button_is_on_the_overview_and_nowhere_else(db):
    """One file covers the whole dashboard, so repeating the control on seven
    pages would offer seven ways to fetch the identical bytes."""
    c = _signed_in()
    assert 'href="/export.json"' in c.get("/").text
    for path in ("/customers", "/actions", "/reports/funnel", "/reports/churn",
                 "/reports/retention", "/reports/traffic", "/faq"):
        assert 'href="/export.json"' not in c.get(path).text, path
        # Copy MD stays everywhere: each page has its own twin.
        assert 'id="copy-md"' in c.get(path).text, path


def test_the_export_needs_credentials(db):
    from partner_dashboard.db import connect
    anon = TestClient(create_app(conn_factory=connect))
    # A GET keeps the 307; only writes were moved to 303.
    assert anon.get("/export.json",
                    follow_redirects=False).status_code in (307, 401, 403)


# --- Window controls ----------------------------------------------------------

@pytest.mark.parametrize("path,param,good,bad", [
    ("/", "months", 24, 999),
    ("/reports/traffic", "days", 180, 7),
    ("/actions", "trial_days", 30, 1),
    ("/reports/churn", "days", 90, 5),
])
def test_a_window_outside_the_allowlist_falls_back(db, path, param, good, bad):
    """A range control is a number from the query string reaching a query. An
    unknown value falls back to the default rather than 422-ing the page or
    being clamped into an answer nobody asked for."""
    c = _signed_in()
    assert c.get(f"{path}?{param}={good}").status_code == 200
    assert c.get(f"{path}?{param}={bad}").status_code == 200
    assert c.get(f"{path}?{param}=banana").status_code == 200
    assert c.get(f"{path}?{param}=").status_code == 200


def _ga4_row(date, sessions=10, installs=1):
    from partner_dashboard.db import connect
    conn = connect()
    conn.execute(
        "insert into ga4_daily (date, dimension, value, sessions, users, "
        "add_app_clicks, installs, ad_clicks) "
        "values (%s, 'total', 'total', %s, %s, 2, %s, 0)",
        (date, sessions, sessions, installs),
    )
    conn.close()


def test_the_window_is_stated_on_the_page(db):
    c = _signed_in()
    assert "MRR, last 24 months" in c.get("/?months=24").text
    assert "MRR, last 12 months" in c.get("/?months=99").text
    # The traffic page shows an empty state rather than a range control when
    # there is nothing to range over, so it needs a row to have a window at all.
    _ga4_row("2026-08-01")
    assert "Last 180 days" in c.get("/reports/traffic?days=180").text
    assert "Last 90 days" in c.get("/reports/traffic?days=7").text


def test_the_markdown_twin_honours_the_same_window(db):
    """A twin that ignored ?days= would quietly stop being a mirror of what is
    on screen."""
    c = _signed_in()
    assert "MRR by month, last 24 months" in c.get("/index.md?months=24").text
    assert "MRR by month, last 12 months" in c.get("/index.md?months=banana").text
    assert "last 180 days" in c.get("/reports/traffic.md?days=180").text


def test_the_headline_tiles_ignore_the_range(db):
    """Installed base and MRR are states, not windows. A control over them would
    be a control that does nothing, so it does not exist and they must not move."""
    c = _signed_in()
    from partner_dashboard.metrics import METRICS
    for months in (6, 24):
        assert METRICS["installed"].name in c.get(f"/?months={months}").text


# --- Drill-downs ---------------------------------------------------------------

def test_country_and_plan_rows_link_into_the_customers_filters(db):
    from partner_dashboard.db import connect
    conn = connect()
    conn.execute("insert into shops (shop_gid, install_state, country) "
                 "values ('s1', 'installed', 'United States')")
    conn.execute("insert into subscriptions (id, shop_gid, monthly_amount, converted_at) "
                 "values ('c1', 's1', 19.00, '2026-01-01Z')")
    conn.execute("insert into charges (gid, plan_interval) values ('c1', 'EVERY_30_DAYS')")
    conn.close()
    body = unescape(_signed_in().get("/").text)
    assert "/customers?country=United%20States" in body
    assert "/customers?plan=EVERY_30_DAYS" in body


def test_the_customers_plan_filter_narrows_to_that_interval(db):
    from partner_dashboard.db import connect
    conn = connect()
    conn.execute("insert into shops (shop_gid, shop_domain, install_state) "
                 "values ('s1', 'monthly.myshopify.com', 'installed')")
    conn.execute("insert into shops (shop_gid, shop_domain, install_state) "
                 "values ('s2', 'annual.myshopify.com', 'installed')")
    conn.execute("insert into subscriptions (id, shop_gid, monthly_amount, converted_at) "
                 "values ('c1', 's1', 19.00, '2026-01-01Z')")
    conn.execute("insert into subscriptions (id, shop_gid, monthly_amount, converted_at) "
                 "values ('c2', 's2', 15.83, '2026-01-01Z')")
    conn.execute("insert into charges (gid, plan_interval) values ('c1', 'EVERY_30_DAYS')")
    conn.execute("insert into charges (gid, plan_interval) values ('c2', 'ANNUAL')")
    conn.close()
    c = _signed_in()
    annual = c.get("/customers?plan=ANNUAL").text
    assert "annual.myshopify.com" in annual
    assert "monthly.myshopify.com" not in annual
    # An unrecognised plan is no filter at all, never an empty page.
    both = c.get("/customers?plan=NONSENSE").text
    assert "annual.myshopify.com" in both and "monthly.myshopify.com" in both


def test_a_reason_bar_links_to_the_merchants_who_said_it(db):
    from partner_dashboard.db import connect
    conn = connect()
    conn.execute("insert into shops (shop_gid, shop_domain, install_state) "
                 "values ('s1', 'gone.myshopify.com', 'uninstalled')")
    conn.execute("insert into raw_app_events (id, type, occurred_at, shop_gid, payload) "
                 "values ('e1', 'RELATIONSHIP_UNINSTALLED', '2026-07-01Z', 's1', '{}')")
    conn.execute("insert into app_events (platform_event_id, type, occurred_at, shop_gid, "
                 "uninstall_reason) values ('e1', 'uninstalled', '2026-07-01Z', 's1', "
                 "'Too expensive')")
    conn.close()
    c = _signed_in()
    assert "/reports/churn?bucket=Too%20expensive" in unescape(c.get("/").text)
    filtered = c.get("/reports/churn?bucket=Too expensive").text
    assert "gone.myshopify.com" in filtered
    assert c.get("/reports/churn?bucket=Nobody said this").status_code == 200


def test_a_cross_origin_annotation_write_is_refused(db):
    """SameSite=lax blocks a cross-*site* post, but "site" is the registrable
    domain: any sibling host under the same domain still gets the cookie
    attached. The cookie alone is therefore not a CSRF defence."""
    c = TestClient(create_app(conn_factory=lambda: keep_open(db)))
    c.cookies.set(SESSION_COOKIE,
                  issue_session(SESSION_SECRET, "ada@example.com", "Ada Lovelace"))
    r = c.post("/annotations", data={"on_date": "2026-03-01", "note": "x"},
               headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    from partner_dashboard import annotations as anno
    assert anno.recent(db) == []


def test_a_same_origin_annotation_write_still_works(db):
    c = TestClient(create_app(conn_factory=lambda: keep_open(db)))
    c.cookies.set(SESSION_COOKIE,
                  issue_session(SESSION_SECRET, "ada@example.com", "Ada Lovelace"))
    r = c.post("/annotations", data={"on_date": "2026-03-01", "note": "shipped v2"},
               headers={"Origin": "https://dash.test"}, follow_redirects=False)
    assert r.status_code == 303
    from partner_dashboard import annotations as anno
    assert [n["note"] for n in anno.recent(db)] == ["shipped v2"]
