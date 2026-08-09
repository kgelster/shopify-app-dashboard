from partner_dashboard.auth import (
    allowed_principals, display_name, email_is_allowed, issue_session, read_session,
    exchange_code,
)

DOMAINS = allowed_principals("example.com,example.org")


def test_allowlist_parsing_handles_domains_and_addresses():
    assert allowed_principals(" @Example.com , example.org ") == {
        "example.com", "example.org"}
    # A leading @ marks a domain and must not survive into the entry, or
    # "@example.com" would be stored looking like an address.
    assert allowed_principals("@x.com, Ada@Example.COM") == {"x.com", "ada@example.com"}


def test_only_listed_domains_pass():
    assert email_is_allowed("ada@example.com", DOMAINS)
    assert email_is_allowed("grace@example.org", DOMAINS)
    # The OAuth client is External, so Google will authenticate anyone. This
    # check is the only gate.
    assert not email_is_allowed("stranger@gmail.com", DOMAINS)
    assert not email_is_allowed("evil@notexample.com", DOMAINS)
    assert not email_is_allowed(None, DOMAINS)
    assert not email_is_allowed("no-at-sign", DOMAINS)


def test_one_address_does_not_admit_the_rest_of_their_company():
    """Collaborators are named individually. Adding a person by address must
    not become a standing invitation to everyone who later gets a mailbox at
    the same company."""
    # One whole domain, plus one individual at a *different* company.
    allowed = allowed_principals("example.com, ada@partner.example")
    assert email_is_allowed("ada@partner.example", allowed)
    assert email_is_allowed("ADA@Partner.Example", allowed)  # case-insensitive
    assert not email_is_allowed("someone-else@partner.example", allowed)
    assert not email_is_allowed("ada@partner.example.evil.com", allowed)
    # The domain entry keeps working alongside it.
    assert email_is_allowed("anyone@example.com", allowed)


def test_removing_an_address_locks_out_the_cookie_it_already_issued():
    # The collaborator is at a company that is not itself allowlisted, so
    # dropping their address is the only thing standing them down.
    token = issue_session("s3cret", "ada@partner.example")
    assert read_session("s3cret", token, allowed_principals("ada@partner.example"))
    assert read_session("s3cret", token, allowed_principals("example.com")) is None


def test_session_round_trip_and_tampering():
    token = issue_session("s3cret", "ada@example.com")
    assert read_session("s3cret", token, DOMAINS) == "ada@example.com"
    assert read_session("different-secret", token, DOMAINS) is None
    assert read_session("s3cret", token + "x", DOMAINS) is None
    assert read_session("s3cret", None, DOMAINS) is None


def test_session_rechecks_domain_so_revoking_locks_out_existing_cookies():
    token = issue_session("s3cret", "someone@example.org")
    assert read_session("s3cret", token, DOMAINS) is not None
    assert read_session("s3cret", token, {"example.com"}) is None


class _Resp:
    def __init__(self, status, payload): self.status_code, self._p = status, payload
    def json(self): return self._p


def test_unverified_google_email_is_rejected():
    post = lambda *a, **k: _Resp(200, {"access_token": "t"})
    get = lambda *a, **k: _Resp(200, {"email": "ada@example.com", "email_verified": False,
                                      "name": "Ex Ample"})
    assert exchange_code("id", "sec", "uri", "code", post=post, get=get) == (None, None)


def test_verified_google_email_and_name_are_returned():
    post = lambda *a, **k: _Resp(200, {"access_token": "t"})
    get = lambda *a, **k: _Resp(200, {"email": "ada@example.com", "email_verified": True,
                                      "name": "Ex Ample"})
    assert exchange_code("id", "sec", "uri", "code", post=post,
                         get=get) == ("ada@example.com", "Ex Ample")


def test_failed_token_exchange_returns_none():
    post = lambda *a, **k: _Resp(400, {})
    get = lambda *a, **k: _Resp(200, {})
    assert exchange_code("id", "sec", "uri", "code", post=post, get=get) == (None, None)


def test_display_name_is_cosmetic_and_never_an_authorization_input():
    named = issue_session("s3cret", "ada@example.com", "Ada Lovelace")
    assert display_name("s3cret", named, "ada@example.com") == "Ada Lovelace"

    # Cookies issued before names were captured carry only an email. They stay
    # valid for their full 14 days, so a missing name falls back rather than
    # erroring or signing anyone out.
    old = issue_session("s3cret", "ada@example.com")
    assert display_name("s3cret", old, "ada@example.com") == "ada"
    # Basic auth has no cookie at all.
    assert display_name("s3cret", None, "curl-user") == "curl-user"

    # A name in an unsigned or wrongly-signed cookie must not surface, and must
    # not be mistaken for an identity: read_session still decides on the email.
    forged = issue_session("attacker-secret", "stranger@gmail.com", "Ada Lovelace")
    assert display_name("s3cret", forged, "real@example.com") == "real"
    assert read_session("s3cret", forged, DOMAINS) is None
