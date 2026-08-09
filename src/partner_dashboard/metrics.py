"""One definition per number, written once.

Every figure on a dashboard is somebody's decision about what to count, and a
reader who cannot see that decision has to either trust it blind or go and read
the SQL. Mixpanel solves this with Lexicon: descriptions live on the event, not
in a wiki, and surface inside the report itself. This is the same idea at the
size this app actually is -- a dict, not a governance product.

The registry is the single source. The tiles read it for the hover panel, the
markdown twins read it so a pasted page carries its own definitions into
whatever agent reads it next, and `docs/architecture.md` points at this file
rather than restating anything. A definition can therefore be wrong, but it
cannot be *inconsistent*, which is the failure that actually happens: the page
says one thing, the doc says another, and nobody knows which shipped first.

`rule` is deliberately close to the SQL rather than a paraphrase of it. A
paraphrase is what drifts.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Metric:
    name: str
    """The label the tile shows. The template reads this rather than hardcoding
    it, so renaming a metric renames it everywhere at once."""

    definition: str
    """One line of plain English. What a person would say out loud."""

    rule: str
    """The exact counting rule, close enough to the query to be checkable."""

    source: str
    """Which table is the truth for it. Money is always subscriptions joined to
    charges, or transactions; never app_events.net_change."""

    kind: str = "window"
    """`point` is a state as of now (installed base, MRR). `window` is a count
    over a span (installs in 30 days). The two compare to different things: a
    point value against its value 30 days ago, a window against the window
    before it. Getting this backwards is how a comparison lies."""

    unit: str = "count"
    """`count`, `usd`, or `pct`. Drives how a delta is formatted."""

    better: str | None = None
    """`up`, `down`, or None when neither direction is good news on its own."""


METRICS: dict[str, Metric] = {
    # -- Overview headline tiles ------------------------------------------
    "installed": Metric(
        name="Currently installed",
        definition="Shops whose most recent lifecycle event is an install.",
        rule="count of shops where install_state = 'installed'",
        source="shops, derived by replaying app_events",
        kind="point", better="up",
    ),
    "active_mrr": Metric(
        name="Active MRR",
        definition="What the current set of subscriptions is worth per month. A "
                   "projection, not cash.",
        rule="sum of monthly_amount over subscriptions that have not churned, on "
             "shops still installed. An annual plan counts as price / 12, but "
             "only if its price is listed in ANNUAL_PLAN_AMOUNTS: the Partner "
             "API does not state a billing interval, so an unlisted price is "
             "treated as monthly and counted at twelve times its true value.",
        source="subscriptions joined to charges",
        kind="point", unit="usd", better="up",
    ),
    "paying": Metric(
        name="Paying shops",
        definition="Installed shops with a live subscription.",
        rule="distinct shop_gid over subscriptions where churned_at is null and "
             "the shop is still installed",
        source="subscriptions joined to shops",
        kind="point", better="up",
    ),
    "arpu": Metric(
        name="ARPU",
        definition="Average monthly revenue per paying shop.",
        rule="active MRR divided by paying shops",
        source="subscriptions joined to charges",
        kind="point", unit="usd", better="up",
    ),
    "installs_30d": Metric(
        name="Installs, last 30 days",
        definition="Install and reinstall events in the last 30 days.",
        rule="count of app_events of type 'installed' or 'reinstalled' with "
             "occurred_at in the last 30 days",
        source="app_events",
        better="up",
    ),
    "uninstalls_30d": Metric(
        name="Uninstalls, last 30 days",
        definition="Every uninstall in the last 30 days, including stores "
                   "Shopify closed or froze.",
        rule="count of app_events of type 'uninstalled' with occurred_at in the "
             "last 30 days. The Churn page separates merchant-chosen exits from "
             "deactivations; this tile does not.",
        source="app_events",
        better="down",
    ),
    "net_30d": Metric(
        name="Collected, last 30 days",
        definition="Cash that reached the payout in the last 30 days, net of "
                   "Shopify's cut.",
        rule="sum of net_amount over transactions created in the last 30 days",
        source="transactions",
        unit="usd", better="up",
    ),
    "churn_30d": Metric(
        name="Logo churn, 30 days",
        definition="Share of the shops that were installed 30 days ago who have "
                   "since left.",
        rule="uninstalls in the last 30 days divided by (currently installed + "
             "those uninstalls)",
        source="shops and app_events",
        unit="pct", better="down",
    ),
    "ltv": Metric(
        name="Lifetime value",
        definition="What the average paying merchant is worth before they leave, "
                   "at the current churn rate.",
        rule="ARPU divided by the monthly subscription churn rate, measured over "
             "90 days and scaled to a month. Null when nobody churned in the "
             "window, because no departures is not evidence of no churn.",
        source="subscriptions",
        unit="usd", better="up",
    ),

    # -- Traffic ----------------------------------------------------------
    "sessions": Metric(
        name="Listing sessions",
        definition="Sessions on the App Store listing page.",
        rule="sum of sessions from the GA4 daily totals row over the window",
        source="ga4_daily",
        better="up",
    ),
    "add_app_clicks": Metric(
        name="Add App clicks",
        definition="Clicks on the listing's Add App button.",
        rule="sum of add_app_clicks from the GA4 daily totals row over the window",
        source="ga4_daily",
        better="up",
    ),
    "listing_installs": Metric(
        name="Installs from listing",
        definition="Installs GA4 saw. Lower than the real number, always.",
        rule="sum of installs from the GA4 daily totals row over the window. This "
             "is the browser-side shopify_app_install event; consent banners and "
             "tracking blockers suppress it while the install still happens.",
        source="ga4_daily",
        better="up",
    ),
    "install_rate": Metric(
        name="Session to install",
        definition="Share of listing sessions that ended in an install GA4 saw.",
        rule="GA4 installs divided by GA4 sessions over the same window. A floor, "
             "not an estimate: the numerator undercounts.",
        source="ga4_daily",
        unit="pct", better="up",
    ),
    "click_to_install": Metric(
        name="Click to install",
        definition="Share of Add App clicks that became an install GA4 saw.",
        rule="GA4 installs divided by GA4 Add App clicks over the same window",
        source="ga4_daily",
        unit="pct", better="up",
    ),
    "ad_clicks": Metric(
        name="Ad clicks",
        definition="Listing sessions that arrived from a Shopify Ads placement.",
        rule="sum of ad_clicks from the GA4 daily totals row over the window",
        source="ga4_daily",
    ),
}


# What a comparison is against, said out loud. A point-in-time metric compares
# to its own past value; a windowed count compares to the window before it.
# Rendering the wrong one is a lie that looks like a feature.
COMPARE_LABEL = {"point": "vs 30 days ago", "window": "vs prior 30 days"}


def signed(value, unit: str = "count") -> str:
    """A change, with its sign always visible.

    `+0` is deliberate rather than blank: "no change" is information, and an
    empty slot where the other five tiles have a number reads as broken.
    """
    if unit == "usd":
        return f"{'-' if value < 0 else '+'}${abs(Decimal(value)):,.2f}"
    if unit == "pct":
        return f"{value:+.1f} pts"
    return f"{value:+,}"
