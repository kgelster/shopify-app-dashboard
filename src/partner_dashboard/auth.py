"""Google sign-in, restricted to allowed email domains.

Basic auth stays alongside this on purpose: it is what curl, health checks, and
any future scripted access use, and it is the fallback if Google is unreachable.
A browser gets redirected to Google; a request that already carries an
Authorization header is checked against DASHBOARD_USERS and let through.

Access is enforced here, not by Google. The OAuth client is an "External" one
because collaborators sit outside your Google Workspace organisation, so Google
will happily authenticate any Google account: the allowlist below is the only
thing standing between a stranger and the dashboard. Do not remove it in favour
of trusting the consent screen, and do not ship a deployment with
GOOGLE_ALLOWED_DOMAINS unset.

The allowlist holds whole domains and individual addresses side by side, so
granting one collaborator access never grants it to their whole company.
"""

import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SESSION_COOKIE = "dashboard_session"
STATE_COOKIE = "dashboard_oauth_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def allowed_principals(raw: str) -> set[str]:
    """Parse the allowlist into domains and exact addresses.

    An entry with an "@" inside it is one person; an entry without one is a
    whole domain. Both forms are needed: everybody at example.com should get
    in, but a collaborator at their own company is one address, not a standing
    invitation to everyone who ever gets a mailbox there.

    A leading "@" is stripped, so "@example.com" and "example.com" both
    mean the domain and neither is mistaken for an address.
    """
    out = set()
    for part in raw.split(","):
        entry = part.strip().lower().lstrip("@")
        if entry:
            out.add(entry)
    return out


def email_is_allowed(email: str | None, allowed: set[str]) -> bool:
    if not email or "@" not in email:
        return False
    email = email.lower()
    # Exact address first, then the domain it belongs to. An address entry can
    # never be matched by the domain branch, so listing one person does not
    # widen access to their colleagues.
    return email in allowed or email.rsplit("@", 1)[1] in allowed


def serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="dashboard-session")


def issue_session(secret: str, email: str, name: str | None = None) -> str:
    payload = {"email": email, "at": int(time.time())}
    if name:
        payload["name"] = name
    return serializer(secret).dumps(payload)


def _load(secret: str, token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return serializer(secret).loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    except Exception:
        logger.info("rejecting unreadable session cookie")
        return None


def read_session(secret: str, token: str | None, allowed: set[str]) -> str | None:
    """Return the signed-in email, or None. Re-checks the allowlist on every
    request, so removing a domain or an address locks out the cookie it already
    issued rather than waiting 14 days for it to expire."""
    data = _load(secret, token)
    if data is None:
        return None
    email = data.get("email")
    return email if email_is_allowed(email, allowed) else None


def display_name(secret: str, token: str | None, fallback: str) -> str:
    """What the header calls you. Display only, and deliberately separate from
    read_session: who gets in is decided by the email, and a name that could
    influence that would be an authorization input under a friendly label.

    Falls back to the local-part, which covers both Basic auth (no cookie at
    all) and any session issued before names were captured. Those cookies stay
    valid for their full 14 days; a missing name must not log anyone out.
    """
    data = _load(secret, token) or {}
    return data.get("name") or fallback.split("@", 1)[0]


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })


def new_state() -> str:
    return secrets.token_urlsafe(24)


def exchange_code(client_id, client_secret, redirect_uri, code, *, post=httpx.post,
                  get=httpx.get) -> tuple[str | None, str | None]:
    """Swap the auth code for an access token and return (verified email, name).

    The `profile` scope has always been requested, so the name was already in
    the userinfo response and was simply thrown away. It is returned separately
    from the email to keep it obvious which of the two decides anything.
    """
    token_response = post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=15)
    if token_response.status_code != 200:
        logger.warning("google token exchange failed: %s", token_response.status_code)
        return None, None
    access_token = token_response.json().get("access_token")
    if not access_token:
        return None, None

    info = get(GOOGLE_USERINFO_URL,
               headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    if info.status_code != 200:
        logger.warning("google userinfo failed: %s", info.status_code)
        return None, None
    payload = info.json()
    # An unverified email can be attacker-chosen on some Google account types.
    if not payload.get("email_verified", False):
        logger.warning("rejecting sign-in with unverified email")
        return None, None
    return payload.get("email"), payload.get("name")
