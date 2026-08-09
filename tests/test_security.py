"""Security headers, constant-time comparison, rate limiting, Slack escaping."""

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from partner_dashboard.auth import SESSION_COOKIE, issue_session
from partner_dashboard.security import RateLimiter, client_key
from partner_dashboard.slack import build_event_message, escape
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
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://dash.test")
    monkeypatch.setenv("USAGE_INGEST_TOKEN", "ingest-secret")


def keep_open(conn):
    """Routes close the connection they were handed; the shared test connection
    has to survive more than one request per test. Same shape as the helper in
    tests/test_web.py, duplicated rather than imported because the tests
    directory is not a package."""
    class NoClose:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass
    return NoClose()


def client_for(db):
    return TestClient(create_app(conn_factory=lambda: keep_open(db)))


def signed_in(db):
    client = client_for(db)
    client.cookies.set(SESSION_COOKIE,
                       issue_session(SESSION_SECRET, "ada@example.com", "Ada"))
    return client


# --- Headers --------------------------------------------------------------


def test_every_response_carries_the_security_headers(db):
    response = signed_in(db).get("/")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    # Authenticated data must not linger in a cache or on the back button, and
    # the page must stay isolated from any cross-origin opener.
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


def test_headers_are_set_on_the_markdown_twins_too(db):
    """The .md routes are a separate response path; a header set only on the
    HTML pages would leave half the surface uncovered."""
    response = signed_in(db).get("/index.md")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers
    # The twins are the surface an agent fetches; no-store matters most here.
    assert response.headers["cache-control"] == "no-store"


def test_the_csp_nonce_matches_the_one_in_the_rendered_page(db):
    """A nonce that does not match is the same as no nonce: the browser refuses
    every inline block and the page ships dead. This is the assertion that keeps
    the CSP honest rather than merely present."""
    response = signed_in(db).get("/")
    header_nonces = re.findall(r"'nonce-([\w-]+)'",
                               response.headers["content-security-policy"])
    assert len(set(header_nonces)) == 1
    page_nonces = set(re.findall(r'<script nonce="([\w-]+)"', response.text))
    assert page_nonces, "base.html should carry inline scripts with a nonce"
    assert page_nonces == {header_nonces[0]}


def test_the_nonce_changes_between_requests(db):
    client = signed_in(db)
    first = client.get("/").headers["content-security-policy"]
    second = client.get("/").headers["content-security-policy"]
    assert first != second


def test_hsts_only_when_the_request_arrived_over_https(db):
    client = signed_in(db)
    assert "strict-transport-security" not in client.get("/").headers
    forwarded = client.get("/", headers={"x-forwarded-proto": "https"})
    assert "max-age=31536000" in forwarded.headers["strict-transport-security"]


# --- Non-ASCII credentials ------------------------------------------------


def test_a_non_ascii_usage_token_is_rejected_not_a_500(db):
    """secrets.compare_digest raises TypeError on a str with a codepoint above
    127, and Starlette decodes headers as latin-1. A 500 here where every wrong
    ASCII token returns 401 tells an unauthenticated caller that the secret is
    configured at all."""
    client = client_for(db)
    # Sent as raw bytes: h11 permits 0x80-0xFF in a header value and Starlette
    # decodes it as latin-1, which is how a str with a codepoint above 127
    # reaches the comparison. httpx refuses to encode such a str itself, so
    # passing one here would test the client rather than the server.
    response = client.post("/ingest/usage", json={"events": []},
                           headers={"X-Usage-Token": b"\xff\xfe"})
    assert response.status_code == 401


def test_a_non_ascii_basic_password_is_rejected_not_a_500(db):
    client = client_for(db)
    assert client.get("/", auth=("tester", "pässwörd")).status_code == 401


def test_a_non_ascii_oauth_state_is_rejected_not_a_500(db):
    client = client_for(db)
    client.cookies.set("dashboard_state", "expected-state")
    response = client.get("/auth/callback?code=x&state=%C3%BF%C3%BE",
                          follow_redirects=False)
    assert response.status_code == 400


def test_the_right_credentials_still_work(db):
    """The whole point of the byte comparison is that it changes nothing about
    what counts as a match."""
    client = client_for(db)
    assert client.get("/healthz", auth=("tester", "suite-only-credential")).status_code == 200
    assert client.get("/", auth=("tester", "suite-only-credential")).status_code == 200


# --- Rate limiting --------------------------------------------------------


def test_repeated_bad_passwords_are_throttled(db):
    client = client_for(db)
    codes = [client.get("/", auth=("tester", "wrong")).status_code for _ in range(12)]
    assert codes[0] == 401
    assert 429 in codes, "a brute-force run should start getting throttled"


def test_a_good_password_is_never_throttled(db):
    client = client_for(db)
    codes = [client.get("/", auth=("tester", "suite-only-credential")).status_code for _ in range(15)]
    assert set(codes) == {200}


def test_a_signed_in_session_is_never_throttled(db):
    client = signed_in(db)
    codes = [client.get("/").status_code for _ in range(15)]
    assert set(codes) == {200}


def test_repeated_bad_ingest_tokens_are_throttled(db):
    client = client_for(db)
    codes = [
        client.post("/ingest/usage", json={"events": []},
                    headers={"X-Usage-Token": "nope"}).status_code
        for _ in range(25)
    ]
    assert codes[0] == 401
    assert 429 in codes


def test_rate_limiter_window_and_reset():
    limiter = RateLimiter(limit=3, window_seconds=300)
    for _ in range(3):
        assert limiter.check("a")
        limiter.record("a")
    assert not limiter.check("a")
    assert limiter.check("b"), "limits are per key"
    limiter.reset("a")
    assert limiter.check("a")


# --- Fail-closed session secret -------------------------------------------


def test_the_app_refuses_to_start_on_the_published_default_secret(db, monkeypatch):
    """The default is printed in this repository, so serving with it would make
    every session cookie forgeable by anyone who can read the source."""
    monkeypatch.setenv("SESSION_SECRET", "dev-only-not-a-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://dash.example.com")
    from partner_dashboard.config import get_settings
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        create_app(conn_factory=lambda: db)


def test_the_default_secret_is_still_fine_on_localhost(db, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "dev-only-not-a-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    from partner_dashboard.config import get_settings
    get_settings.cache_clear()
    create_app(conn_factory=lambda: db)   # does not raise


# --- Slack mrkdwn escaping ------------------------------------------------


def test_a_shop_name_cannot_forge_a_slack_link_label():
    """`<url|label>` is Slack's link syntax. A shop name carrying `|` or `>`
    could otherwise close the link early and put its own text on a link that
    points somewhere else."""
    shop = {"shop_name": "Evil|https://evil.example", "shop_domain": "e.myshopify.com",
            "country": "US", "plan": "$19/mo"}
    text = build_event_message(shop, "installed", "https://dash.test")["blocks"][0]["text"]["text"]
    # Exactly one link, and it points at the real customer page.
    assert text.count("<") == 1 and text.count(">") == 1
    assert "<https://dash.test/customers/e.myshopify.com|" in text
    assert "|https://evil.example" not in text


def test_angle_brackets_and_ampersands_are_escaped():
    assert escape("A & B") == "A &amp; B"
    assert escape("<script>") == "&lt;script&gt;"
    # The ampersands the escaping introduces are not escaped a second time.
    assert escape("<") == "&lt;"


def test_escaping_leaves_ordinary_names_alone():
    assert escape("Smith and Sons") == "Smith and Sons"


class _Req:
    def __init__(self, headers=None, peer="10.0.0.1"):
        self.headers = headers or {}
        self.client = SimpleNamespace(host=peer) if peer else None


def test_client_key_uses_the_socket_peer_when_no_proxy_header_is_configured(monkeypatch):
    monkeypatch.setenv("TRUSTED_CLIENT_IP_HEADER", "")
    # An unconfigured deployment must not start trusting a header just because
    # a caller sent one: that would let anyone pick their own rate-limit bucket.
    req = _Req({"x-forwarded-for": "1.2.3.4", "fly-client-ip": "5.6.7.8"})
    assert client_key(req) == "10.0.0.1"


def test_client_key_reads_the_configured_header(monkeypatch):
    monkeypatch.setenv("TRUSTED_CLIENT_IP_HEADER", "Fly-Client-IP")
    assert client_key(_Req({"fly-client-ip": "5.6.7.8"})) == "5.6.7.8"


def test_client_key_reads_the_rightmost_entry_of_a_forwarded_chain(monkeypatch):
    """Proxies APPEND to X-Forwarded-For, so the leftmost entry is whatever the
    client sent and the rightmost is what our own proxy observed. Reading the
    leftmost lets a caller rotate a header value to get a fresh rate-limit
    bucket per request, which is a brute-force guard that guards nothing."""
    monkeypatch.setenv("TRUSTED_CLIENT_IP_HEADER", "X-Forwarded-For")
    req = _Req({"x-forwarded-for": "1.2.3.4, 10.9.9.9, 203.0.113.7"})
    assert client_key(req) == "203.0.113.7"


def test_client_key_ignores_a_spoofed_leftmost_entry(monkeypatch):
    monkeypatch.setenv("TRUSTED_CLIENT_IP_HEADER", "X-Forwarded-For")
    # The attacker controls everything before the entry the proxy appended.
    keys = {client_key(_Req({"x-forwarded-for": f"9.9.9.{n}, 203.0.113.7"}))
            for n in range(50)}
    assert keys == {"203.0.113.7"}, "rotating a spoofed prefix must not change the bucket"


def test_client_key_refuses_a_value_that_is_not_an_ip(monkeypatch):
    """Without this the bucket key is an arbitrary caller-supplied string, so
    the keyspace is unbounded and an 8KB header becomes an 8KB dict key."""
    monkeypatch.setenv("TRUSTED_CLIENT_IP_HEADER", "X-Forwarded-For")
    assert client_key(_Req({"x-forwarded-for": "attacker-chosen-bucket-7"})) == "10.0.0.1"
    assert client_key(_Req({"x-forwarded-for": "A" * 8000})) == "10.0.0.1"
    assert client_key(_Req({"x-forwarded-for": "   "})) == "10.0.0.1"
    assert client_key(_Req({"x-forwarded-for": " ,203.0.113.9"})) == "203.0.113.9"


def test_rate_limiter_keyspace_is_bounded(monkeypatch):
    """Both limiter paths check() before authenticating, so an unauthenticated
    caller must not be able to grow this dict without bound."""
    limiter = RateLimiter(limit=3, window_seconds=300)
    for n in range(RateLimiter.MAX_KEYS + 500):
        limiter.record(f"key-{n}")
    assert len(limiter._hits) == RateLimiter.MAX_KEYS


def test_checking_an_unseen_key_does_not_store_it():
    limiter = RateLimiter(limit=3, window_seconds=300)
    for n in range(1000):
        assert limiter.check(f"unseen-{n}")
    assert len(limiter._hits) == 0


def test_client_key_falls_back_when_the_configured_header_is_absent(monkeypatch):
    monkeypatch.setenv("TRUSTED_CLIENT_IP_HEADER", "Fly-Client-IP")
    assert client_key(_Req({})) == "10.0.0.1"
    assert client_key(_Req({}, peer=None)) == "unknown"
