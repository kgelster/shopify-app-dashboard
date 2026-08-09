import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Credentials that appear in this repository's own documentation. Refused
# outright: Basic auth bypasses the SSO allowlist, so one of these is a full
# account on a public-facing deployment.
PUBLISHED_CREDENTIALS = {"admin:change-me", "user:pass", "u:p", "admin:admin"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- required: nothing here has a safe default -------------------------
    database_url: str
    partner_api_token: str
    partner_org_id: str
    partner_app_id: str
    # "user:pass,user2:pass2" -- one credential pair per dashboard user
    dashboard_users: str
    # Redirect URIs must match Google's registration byte for byte, so this is
    # configured rather than derived from the request: behind a TLS-terminating
    # proxy the request scheme can read as http and silently break the callback.
    # Required, and deliberately without a default: a default here would point
    # every deployment at whoever published it.
    public_base_url: str
    # Comma-separated domains and individual addresses. Enforced by us, not by
    # Google: the OAuth client is External, so Google authenticates any account
    # and this is the gate. Required for the same reason as public_base_url --
    # inheriting somebody else's allowlist is a standing back door.
    google_allowed_domains: str

    # --- identity ----------------------------------------------------------
    # The Shopify app being measured. The dashboard calls itself
    # "<app_name> Analytics".
    app_name: str = "Shopify App"
    # Used in export filenames. Falls back to a slug of app_name.
    app_slug: str = ""
    # Public App Store listing. The reviews link is hidden when this is unset.
    app_listing_url: str = ""

    # --- optional integrations ---------------------------------------------
    slack_webhook_url: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    # Signs the session cookie. Rotating it logs everyone out. create_app
    # refuses to serve a non-local deployment while this is the published
    # default, so leaving it alone is a startup failure, not a silent weakness.
    session_secret: str = "dev-only-not-a-secret"
    ga4_property_id: str | None = None
    # The service-account key JSON, whole, as one secret. Held in memory only:
    # never written to disk on the machine.
    ga4_credentials_json: str | None = None
    # The day your GA4 property started collecting. Backfills clamp to it, so a
    # date earlier than the real one just asks GA4 for empty days.
    ga4_earliest_data: date = date(2020, 1, 1)
    # Shared secret for POST /ingest/usage, the one route an external caller
    # reaches. Unset means the endpoint refuses everything.
    usage_ingest_token: str | None = None

    # --- what your app sells -----------------------------------------------
    # AppSubscription carries no billing-interval field, so the interval is
    # inferred from the price. List every annual price you charge, comma
    # separated (e.g. "190.00,490.00"). EMPTY MEANS EVERY PLAN IS MONTHLY: an
    # annual price missing from this list is counted at twelve times its true
    # MRR, which is the single easiest way to make this dashboard lie.
    annual_plan_amounts: str = ""

    # --- what your app does ------------------------------------------------
    # Accepted event names on POST /ingest/usage. Anything outside the list is
    # rejected rather than stored. See docs/usage-events-integration.md.
    usage_event_types: str = "offer_created,offer_impression,offer_conversion,settings_completed"
    # Which of those means "the merchant built something" and which means "it is
    # running for shoppers". Both must appear in usage_event_types.
    usage_activation_event: str = "offer_created"
    usage_live_event: str = "offer_impression"

    # --- operational -------------------------------------------------------
    poll_interval_minutes: int = 15
    poll_overlap_minutes: int = 60
    # Weekly Slack digest, as a cron day-of-week and hour in this timezone.
    digest_day_of_week: str = "mon"
    digest_hour: int = 9
    # Empty would fall through to the scheduler's system-local timezone, so the
    # digest would fire at an hour nobody chose. Normalised in the validator.
    digest_timezone: str = "UTC"
    # The header your proxy puts the real client address in. Rate limiting keys
    # on it. PREFER A SINGLE-VALUE HEADER your proxy overwrites: Fly-Client-IP,
    # CF-Connecting-IP, X-Real-IP. X-Forwarded-For works but is a list that
    # proxies append to, so only the rightmost entry is trustworthy, which is
    # what client_key reads. Empty means trust the socket peer, which is right
    # only with no proxy in front.
    trusted_client_ip_header: str = ""
    # No annotation may be dated before this. Set it to roughly when your app
    # launched; a chart marker dated 1970 is a typo, not history.
    annotations_earliest: date = date(2020, 1, 1)
    # Shopify made the uninstall reason question mandatory during 2026, so
    # coverage before and after is not comparable and reports say so. Verify
    # against your own feed before trusting the boundary.
    reason_mandatory_from: date = date(2026, 4, 29)

    # --- validation ---------------------------------------------------------
    # These run at construction, which for this app means at import, because
    # web.py builds the ASGI app at module level. So a bad value is a process
    # that refuses to start rather than a dashboard that quietly reports the
    # wrong number hours later.

    @field_validator("annual_plan_amounts")
    @classmethod
    def _annual_prices_are_sane(cls, raw: str) -> str:
        """Reject anything that is not a positive, finite price.

        Unvalidated, "abc" raises decimal.InvalidOperation lazily inside the
        first poll that ingests a charge, which stalls the sync hours after the
        deploy. Worse, "1,900.00" parses silently as two prices, 1 and 900,
        which is the likeliest real typo here: a $1,900 annual plan stays
        monthly AND two junk amounts start matching.
        """
        parts = [p.strip() for p in raw.split(",")]

        # A thousands separator cannot be caught by parsing each part, because
        # "1,900.00" is also a valid two-price list of 1 and 900.00. It is
        # caught by SHAPE: a bare integer with no decimal point, followed by a
        # part whose integer portion is exactly three digits, is what a split
        # number looks like. Requiring the left side to have no cents is the
        # disambiguator -- "190.00,490.00" is unmistakably two prices and passes,
        # while "1,900.00" is flagged. This does refuse the genuinely ambiguous
        # "190,490.00", which is why the message names both readings.
        for left, right in zip(parts, parts[1:]):
            if re.fullmatch(r"\d{1,3}", left) and re.fullmatch(r"\d{3}(\.\d+)?", right):
                raise ValueError(
                    f"ANNUAL_PLAN_AMOUNTS is ambiguous around {left!r},{right!r}. "
                    f"If that is one price with a thousands separator, remove it: "
                    f"{left}{right}. If they are two prices, write both with cents: "
                    f"{left}.00,{right}. Read the wrong way, an annual plan stays "
                    f"counted as monthly at twelve times its true MRR."
                )

        for part in parts:
            if not part:
                continue
            try:
                value = Decimal(part)
            except InvalidOperation:
                raise ValueError(
                    f"ANNUAL_PLAN_AMOUNTS contains {part!r}, which is not a number. "
                    "Use plain decimals separated by commas, with no currency "
                    "symbols and no thousands separators: 190.00,1900.00"
                ) from None
            if not value.is_finite() or value <= 0:
                raise ValueError(
                    f"ANNUAL_PLAN_AMOUNTS contains {part!r}. Prices must be "
                    "positive and finite."
                )

        return raw

    @field_validator("dashboard_users")
    @classmethod
    def _every_pair_has_a_colon(cls, raw: str) -> str:
        """Catch a password containing a comma.

        The format is "user:pass,user2:pass2", so a comma inside a password
        silently truncates it: "admin:pa,ssword" parses as {"admin": "pa"} and
        logging in with "pa" succeeds. An operator who generated a random
        password would get a two-character one and never know. A fragment with
        no colon in it is that accident, every time.
        """
        for part in (p.strip() for p in raw.split(",")):
            if part and ":" not in part:
                raise ValueError(
                    f"DASHBOARD_USERS has a fragment with no colon: {part!r}. "
                    "Entries are user:pass separated by commas, so a password "
                    "containing a comma is silently truncated. Generate one "
                    "without: python -c \"import secrets; "
                    "print(secrets.token_urlsafe(24))\""
                )
        # Basic auth bypasses the Google domain allowlist by design, so a
        # published placeholder here is a full account. This repository is
        # public, which makes any example credential the first thing anyone
        # tries against a deployment.
        if any(p.strip() in PUBLISHED_CREDENTIALS for p in raw.split(",")):
            raise ValueError(
                "DASHBOARD_USERS is still an example credential from this "
                "repository. It grants full access and bypasses "
                "GOOGLE_ALLOWED_DOMAINS. Generate one: python -c \"import "
                "secrets; print(secrets.token_urlsafe(24))\""
            )
        return raw

    @model_validator(mode="after")
    def _usage_events_agree(self) -> "Settings":
        """The activation and live events must be names the endpoint accepts.

        Otherwise /ingest/usage rejects every event of that name with a 422 and
        the activation report shows a confident 0% for merchants who did in fact
        activate. The live event fails in the reassuring direction, which is
        worse: "every paying shop has served an offer" on an empty result.
        """
        known = self.usage_event_types_set
        for label, value in (("USAGE_ACTIVATION_EVENT", self.usage_activation_event),
                             ("USAGE_LIVE_EVENT", self.usage_live_event)):
            if known and value not in known:
                raise ValueError(
                    f"{label} is {value!r}, which is not in USAGE_EVENT_TYPES "
                    f"({', '.join(sorted(known))}). Events with that name would "
                    "be rejected on ingest, and the reports built on them would "
                    "read 0% rather than saying they have no data."
                )
        return self

    @model_validator(mode="after")
    def _warn_about_silent_misconfiguration(self) -> "Settings":
        # Not an error: an app with no annual plan is a legitimate deployment,
        # and refusing to start would be wrong. But an empty list is also what
        # an operator who simply has not read this setting ends up with, and
        # every annual subscriber is then counted at twelve times their true
        # MRR with nothing on any page to say so. Invariant 10 cannot catch it
        # either: with nothing labelled ANNUAL, it runs over zero rows and
        # passes. So say it once, loudly, at startup.
        if not self.annual_plan_amounts.strip():
            logger.warning(
                "ANNUAL_PLAN_AMOUNTS is empty, so every plan is treated as "
                "monthly. If you sell an annual plan, its subscribers are "
                "currently counted at 12x their true MRR. Set it, then reset "
                "the sync cursor and replay: a corrected price only reaches "
                "stored charges on re-ingest."
            )
        return self

    # --- derived ------------------------------------------------------------

    @property
    def dashboard_users_map(self) -> dict[str, str]:
        pairs = (p.split(":", 1) for p in self.dashboard_users.split(",") if ":" in p)
        return {u.strip(): pw for u, pw in pairs}

    @field_validator("digest_timezone")
    @classmethod
    def _timezone_is_named(cls, raw: str) -> str:
        return raw.strip() or "UTC"

    @property
    def dashboard_name(self) -> str:
        return f"{self.app_name} Analytics"

    @property
    def slug(self) -> str:
        if self.app_slug:
            return self.app_slug
        cleaned = "".join(c if c.isalnum() else "-" for c in self.app_name.lower())
        return "-".join(p for p in cleaned.split("-") if p) or "app"

    @property
    def annual_plan_amounts_set(self) -> frozenset[Decimal]:
        return frozenset(
            Decimal(p.strip()) for p in self.annual_plan_amounts.split(",") if p.strip()
        )

    @property
    def usage_event_types_set(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.usage_event_types.split(",") if p.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
