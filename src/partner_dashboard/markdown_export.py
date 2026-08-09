"""Every page, as a markdown document an agent can read.

Same shape Shopify uses on shopify.dev: YAML frontmatter naming the page and
where it came from, then prose, then the data itself as JSON. The prose exists
so a model reading this cold does not have to guess what a number means; the
JSON exists so it does not have to parse a table.

Two rules hold everywhere in here:

- **No merchant contact details.** Shop names and domains identify a business
  and stay; owner names and email addresses are dropped. A copied document is
  one paste away from a third-party model, and that is not a decision to make on
  a merchant's behalf. The stripping here is belt-and-braces: migration 008
  emptied both columns outright, so there is currently nothing to strip.
- **Every caveat that lives in a footnote on the page lives in the prose here.**
  A model that does not know deactivations are folded into uninstalls will
  confidently report the wrong churn number.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

from partner_dashboard import annotations as anno
from partner_dashboard import stats
from partner_dashboard.customers import count_customers, distinct_facets, list_customers
from partner_dashboard.faq import FAQ
from partner_dashboard.metrics import METRICS
from partner_dashboard.ops import sync_health
from partner_dashboard.ranges import (
    CHURN_DAYS,
    MONEY_MONTHS,
    TRAFFIC_DAYS,
    TRIAL_DAYS,
    choice,
)
from partner_dashboard.usage import (
    activation_cohorts,
    at_risk_shops,
    has_usage_data,
    time_to_activation,
)

# Path on the site -> (slug used for the .md URL, title, one-line description).
# "{app}" in a description is filled with APP_NAME when the frontmatter is
# built, so this stays a plain dict that web.py can enumerate routes from.
PAGES = {
    "overview": ("index", "Overview",
                 "Installed base, MRR and what moved it, lifecycle activity, and where "
                 "customers are, for the {app} Shopify app."),
    "customers": ("customers", "Customers",
                  "Every shop that has ever installed {app}, filterable by "
                  "install state, industry, and country."),
    "actions": ("actions", "Actions",
                "Three call sheets: merchants to ask for a review, monthly subscribers to "
                "pitch the annual plan, and recent installs that have not subscribed."),
    "funnel": ("reports/funnel", "Funnel",
               "Install to subscription conversion for {app}, all time and "
               "by install month, plus activation: whether merchants ever set it up."),
    "churn": ("reports/churn", "Churn",
              "Every merchant-chosen uninstall with its reason, how long the shop stayed, "
              "and whether it ever paid."),
    "retention": ("reports/retention", "Retention",
                  "Monthly install and subscription cohorts, and the share of each still "
                  "installed or still paying N months later."),
    "traffic": ("reports/traffic", "Traffic",
                "App Store listing sessions, Add App clicks, and installs from GA4, split "
                "by channel, source, country, and language."),
    "faq": ("faq", "Why the numbers don't match",
            "Why MRR and collected revenue differ, why uninstall reasons outnumber "
            "uninstalls, and why GA4 undercounts installs, for {app}."),
}


def _definitions(*keys: str) -> str:
    """The definitions for the numbers on this page, from partner_dashboard.metrics.

    The same registry the tiles read, so a pasted document carries the same
    definitions the reader saw on screen. Without this a model gets the numbers
    and has to infer what they count, which is exactly where it invents an
    answer that sounds right.
    """
    return "\n".join([
        "## What these numbers mean\n",
        *[f"- **{METRICS[k].name}** &mdash; {METRICS[k].definition} "
          f"Counted as: {METRICS[k].rule}. Source: {METRICS[k].source}."
          for k in keys],
        "",
    ])


def json_default(o):
    """How the database's own types become JSON.

    Module level because partner_dashboard.export serialises the same rows to a downloadable
    file: one encoder means a Decimal cannot land as a float in one export and a
    string in the other.
    """
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(type(o))


def _json(value) -> str:
    return "```json\n" + json.dumps(value, indent=2, default=json_default) + "\n```"


def _frontmatter(page: str, base_url: str, now: datetime,
                 app_name: str = "the app", dashboard_name: str = "Analytics") -> str:
    slug, title, description = PAGES[page]
    description = description.format(app=app_name)
    html_url = f"{base_url}/" if slug == "index" else f"{base_url}/{slug}"
    # Block scalar for the description so a long line does not need quoting,
    # matching the shopify.dev format this is modelled on.
    return (
        "---\n"
        f"title: '{dashboard_name}: {title}'\n"
        "description: >-\n"
        f"  {description}\n"
        "source_url:\n"
        f"  html: '{html_url}'\n"
        f"  md: '{base_url}/{slug}.md'\n"
        f"generated_at: '{now.isoformat(timespec='seconds')}'\n"
        "---\n"
    )


READING_NOTES = """## How to read this

- Every number on this dashboard carries its own definition, and they are all at
  `/faq.md` along with the reasons two figures that look like they should agree do not.
- Money is monthly. An annual plan counts as one twelfth of its price, so it
  never inflates MRR against a monthly subscriber.
- "Uninstalled" in the raw event feed covers two different things: a merchant who
  chose to leave, and a store Shopify closed or froze. Anything about why merchants
  leave counts only the first kind. The Churn page reports the second kind separately.
- Uninstall reasons are multi-select and Shopify serves the question in the merchant's
  own admin language. Reasons are grouped into canonical buckets here, and the bucket
  counts sum to more than the number of merchants. Shopify made answering mandatory
  partway through 2026, so reason data splits into a sparse era and a near-complete
  one; the `era` block on the Churn page carries both and the buckets count the
  mandatory era only.
- Merchant contact details (owner names, email addresses) are deliberately omitted
  from this document. Shop names and domains are not.
"""


def _overview(conn, settings, query: dict) -> str:
    months = choice(query.get("months"), MONEY_MONTHS, 12)
    s = stats.overview_stats(conn)
    health = sync_health(conn, settings.poll_interval_minutes)
    notes = anno.recent(conn)
    return "\n".join([
        "# Overview\n",
        f"{s['installed']} shop{'' if s['installed'] == 1 else 's'} currently "
        f"{'has' if s['installed'] == 1 else 'have'} the app installed. "
        f"{s['paying']} of them "
        f"pay, for {Decimal(s['active_mrr']):.2f} USD of monthly recurring revenue "
        f"({Decimal(s['arpu']):.2f} average per paying shop). In the last 30 days there "
        f"were {s['installs_30d']} installs and {s['uninstalls_30d']} uninstalls, a logo "
        f"churn rate of {s['churn_30d']}%.\n",
        _definitions("installed", "active_mrr", "paying", "arpu", "installs_30d",
                     "uninstalls_30d", "net_30d", "churn_30d", "ltv"),
        "## Headline numbers\n",
        _json(s),
        "\n## Against the previous period\n",
        "Each headline figure and what it was. `installed`, `active_mrr` and `paying` are "
        "states, so they compare to their own value 30 days ago; `installs_30d`, "
        "`uninstalls_30d` and `net_30d` are counts over a window, so they compare to the "
        "30 days before this one. `pct` is null when the earlier value was zero.\n",
        _json(stats.overview_comparison(conn, {**s, "net_30d": stats.collected_revenue(conn)["net_30d"]})),
        "\n## Annotations\n",
        "Hand-written notes marking why a number moved, newest first. These are the only rows "
        "on this dashboard a person typed, so they carry no more authority than whoever typed "
        "them. A note can be deleted but never edited, so what is here is what someone wrote "
        "rather than a revision of it. An author ending in `CHANGELOG` means the note was "
        "imported from the app's release notes, not observed on this data.\n",
        _json(notes),
        "\n## Pipeline health\n",
        "Data is only as fresh as the last successful Partner API poll.\n",
        _json(health),
        "\n## Unit economics\n",
        "`ltv` is `arpu` divided by the monthly subscription churn rate, measured over "
        "`window_days` and scaled to a month. `subs_at_start` and `churned_in_window` are "
        "the raw counts behind that rate: at this size it is a handful of events, so LTV "
        "is an order of magnitude, not a forecast. `ltv` is null when nobody churned in "
        "the window, because no departures is not evidence of no churn.\n",
        _json(stats.unit_economics(conn)),
        f"\n## MRR by month, last {months} months\n",
        "Sum of subscriptions converted by each month's end and not yet churned.\n",
        _json(stats.mrr_trend(conn, months)),
        "\n## What moved MRR\n",
        "Each month's change split into new, reactivation, expansion, contraction, and "
        "churned. The five sum exactly to the change in the MRR series above. A shop that "
        "paid before, stopped, and came back is a reactivation, not a new customer.\n",
        _json(stats.mrr_movements(conn, months)),
        "\n## Money actually collected\n",
        "Cash, not projection. Everything above is MRR (what the current subscriptions are "
        "worth per month); this is what Shopify billed and what reached the payout. Both "
        "are correct and they are not the same number.\n"
        "`taken` is `gross` minus `net`, NOT the `shopify_fee` column. `shopify_fee` is "
        "Shopify's revenue share, which is 0% below $1M of lifetime earnings and so reads "
        "0.00 on every transaction; the billing processing fee appears only in the gap "
        "between gross and net. That gap is not a flat rate either: identically priced charges "
        "settle between 2.9% and 5.9% depending on the merchant, so it is measured per "
        "transaction, never modelled.\n"
        "`refunded` is money that came back out, as a positive amount. Refunds, credits "
        "and adjustments exist ONLY in this feed -- no app event is emitted when money is "
        "returned -- so every other number on this dashboard is blind to them.\n",
        _json(stats.collected_revenue(conn)),
        "\n### By month\n",
        _json(stats.revenue_by_month(conn, months)),
        "\n## Installs and uninstalls by month\n",
        _json(stats.monthly_activity(conn)),
        "\n## Customers by country\n",
        "Country comes from the CSV importer, not the Partner API, and is frozen at "
        "as of 2026-08-07. Shops that installed after that date have none.\n",
        _json(stats.country_breakdown(conn)),
        "\n## Plan mix\n",
        _json(stats.plan_mix(conn)),
        "\n## Why merchants leave\n",
        _json(stats.uninstall_reasons(conn)),
    ])


def customer_markdown(conn, settings, shop_domain: str, detail: dict,
                      now: datetime | None = None) -> str:
    """One merchant's page as markdown. Not in PAGES: there is one of these per
    shop rather than one per route, so it gets its own frontmatter builder.

    Same contact rule as everywhere else here -- `customer_detail` never selects
    shops.owner_name or shops.email, so there is nothing to strip.
    """
    now = now or datetime.now(timezone.utc)
    base_url = settings.public_base_url.rstrip("/")
    shop = detail["shop"]
    name = shop["shop_name"] or shop_domain
    frontmatter = (
        "---\n"
        f"title: '{settings.dashboard_name}: {name}'\n"
        "description: >-\n"
        f"  Lifecycle timeline, payment history and product usage for the shop "
        f"{shop_domain}.\n"
        "source_url:\n"
        f"  html: '{base_url}/customers/{shop_domain}'\n"
        f"  md: '{base_url}/customers/{shop_domain}.md'\n"
        f"generated_at: '{now.isoformat(timespec='seconds')}'\n"
        "---\n"
    )
    body = "\n".join([
        f"# {name}\n",
        f"`{shop_domain}` is currently **{detail['current_state']}**"
        + (f", installed {detail['install_count']} separate times"
           if detail["install_count"] > 1 else "")
        + f". {detail['money']['count']} payments totalling "
          f"{Decimal(detail['money']['net']):.2f} USD net of fees.\n",
        "## Timeline\n",
        "Strict occurred_at order. The state above is read off the last lifecycle row "
        "here, so the two cannot disagree. `chose_to_leave` is false for an uninstall "
        "that was Shopify closing or freezing the store rather than the merchant "
        "leaving; those merchants are never shown the exit survey.\n",
        _json(detail["timeline"]),
        "\n## Shop\n",
        _json(shop),
        "\n## Subscription\n",
        _json(detail["subscription"]),
        "\n## Payments\n",
        "Newest first. `fee` is Shopify's revenue share, which is 0 below $1M of "
        "lifetime earnings; the real deduction is `gross` minus `net` and is not a flat "
        "rate. A negative row is a refund, credit or adjustment.\n",
        _json(detail["payments"]),
        "\n## Totals\n",
        _json(detail["money"]),
        "\n## Product usage\n",
        f"Reported by {settings.app_name} itself, not the Partner API. Empty means the shop "
        "predates tracking or never built an offer -- the two are not distinguishable "
        "from here.\n",
        _json(detail["usage"]),
    ])
    return "\n".join([frontmatter, body, "", READING_NOTES])


def _customers(conn, settings, query: dict) -> str:
    filters = {k: query.get(k) or None
               for k in ("industry", "country", "search", "install_state")}
    total = count_customers(conn, **filters)
    rows = list_customers(conn, **filters, limit=1000)
    slim = [
        {"shop_name": r["shop_name"], "shop_domain": r["shop_domain"],
         "country": r["country"], "industry": r["industry"],
         "install_state": r["install_state"],
         "installed_at": r["installed_at"], "uninstalled_at": r["uninstalled_at"]}
        for r in rows
    ]
    active = {k: v for k, v in filters.items() if v}
    return "\n".join([
        "# Customers\n",
        f"{total} shops match"
        + (f" the filters {json.dumps(active)}" if active else " (no filters applied)")
        + f". {len(slim)} are listed below; the page itself shows 50 at a time.\n",
        "No merchant contact details exist to export: the owner_name and email columns "
        "were emptied by migration 008 because their only source, a vendor export's Contact "
        "columns, listed agency and app-team staff rather than the merchant.\n",
        "## Shops\n",
        _json(slim),
        "\n## Available filter values\n",
        _json(distinct_facets(conn)),
    ])


CONTACT_KEYS = ("owner_name", "email")


def _no_contact(rows: list[dict]) -> list[dict]:
    """Drop contact details from rows that carry them for the HTML page.

    Stripping here rather than at the query means the page keeps what it needs
    to write to a merchant while the copyable document never carries it. Keyed
    on the field names so a new contact column has to be added deliberately.
    """
    return [{k: v for k, v in row.items() if k not in CONTACT_KEYS} for row in rows]


def _actions(conn, settings, query: dict) -> str:
    trial_days = choice(query.get("trial_days"), TRIAL_DAYS, 14)
    review = _no_contact(stats.review_candidates(conn))
    annual = stats.annual_upgrade_candidates(conn)
    trial = stats.trial_watch(conn, trial_days)
    tracking = has_usage_data(conn)
    at_risk = at_risk_shops(conn) if tracking else []
    quiet = []
    if tracking:
        quiet = [
            "## Paying, but gone quiet\n",
            "Active subscribers whose offers have served no impression in 14 days. Quietest "
            "first. Only shops whose app has ever reported an impression are eligible, so a "
            "shop that predates tracking is not accused of silence.\n",
            _json(at_risk),
            "",
        ]
    return "\n".join([
        "# Actions\n",
        f"{len(review)} merchants are due a review ask, {len(annual)} monthly subscribers "
        f"are worth pitching the annual plan, and {len(trial)} recent installs have not "
        f"subscribed yet."
        + (f" {len(at_risk)} paying shops have gone quiet.\n" if tracking else "\n"),
        *quiet,
        "## Review candidates\n",
        "Installed, paying, past 30 days, and not already an App Store reviewer "
        "(`shops.reviewed_at`, hand-maintained from the public listing). Longest-tenured "
        "first. No contact details anywhere: not here, and not on the page either, "
        "which is why the ask has to go out through the App Store listing.\n",
        _json(review),
        "\n## Annual upgrade candidates\n",
        "On a monthly plan for at least three months.\n",
        _json(annual),
        f"\n## Trial watch, last {trial_days} days\n",
        f"Installed in the last {trial_days} days with no subscription. This is a proxy "
        "for activation risk, not usage data: the Partner API carries no product usage at "
        "all. The window is the only one on this page that moves; the review and annual "
        "lists are business rules, not view windows.\n",
        _json(trial),
    ])


def _funnel(conn, settings, query: dict) -> str:
    parts = [
        "# Funnel\n",
        "Lifecycle funnel and install-month conversion. Percentages in the funnel are "
        "relative to every shop that ever installed.\n",
        "## Lifecycle funnel, all time\n",
        _json(stats.funnel_stats(conn)),
        "\n## Conversion by install month\n",
        _json(stats.monthly_conversion(conn)),
        "\n## Activation\n",
    ]
    if has_usage_data(conn):
        parts += [
            "Whether merchants build an offer, reported by the app itself: the Partner API "
            "carries no product usage. Only shops that installed after tracking started are "
            "counted, because an earlier shop has no first-offer event to find and would "
            "otherwise read as a merchant who never activated. `median_hours` is a median, not "
            "a mean, so one merchant activating a year late does not distort it.\n",
            _json(time_to_activation(conn)),
            "\n### By install cohort\n",
            "`within_48h` and `within_7d` count shops whose first `offer_created` landed inside "
            "that window after their install.\n",
            _json(activation_cohorts(conn)),
        ]
    else:
        parts.append(
            "No usage events have arrived yet, so activation is unknown rather than zero. The "
            "app has to report it; the contract is `docs/usage-events-integration.md`.\n")
    return "\n".join(parts)


def _churn(conn, settings, query: dict) -> str:
    since_days = choice(query.get("days"), CHURN_DAYS, None)
    rows = stats.churn_rows(conn, paid=query.get("paid"),
                            gave_reason=query.get("reason"),
                            bucket=query.get("bucket") or None,
                            since_days=since_days)
    deaths = stats.store_deaths(conn)
    reasons = stats.uninstall_reasons(conn)
    return "\n".join([
        "# Churn\n",
        f"{len(rows)} merchant-chosen uninstalls, and {deaths['count']} stores Shopify "
        f"closed or froze. Only the first group was ever shown an exit survey. Shopify "
        f"made answering it mandatory on {reasons['mandatory_from']}: "
        f"{reasons['era']['post']['with_reason']} of {reasons['era']['post']['total']} "
        f"({reasons['era']['post']['coverage_pct']}%) have answered since, against "
        f"{reasons['era']['pre']['with_reason']} of {reasons['era']['pre']['total']} "
        f"({reasons['era']['pre']['coverage_pct']}%) before.\n",
        "## Uninstall reasons\n",
        "`buckets` counts the mandatory era only. `era` carries both halves, and the "
        "top-level `total` / `with_reason` / `coverage_pct` are all time, which averages "
        "two different questions and should not be quoted on its own.\n",
        _json(reasons),
        "\n## In their own words\n",
        "The free-text notes, grouped by the first canonical reason bucket the merchant "
        "selected. Verbatim and untranslated. The same notes appear one per row under "
        "Every uninstall below; this is the short read.\n",
        _json(stats.uninstall_verbatims(conn)),
        "\n## Time installed before leaving\n",
        _json(stats.time_to_uninstall(conn)),
        "\n## Paid versus never paid\n",
        _json(stats.churn_composition(conn)),
        "\n## Every uninstall\n",
        "One row per uninstall event, newest first. `days` measures the stay that ended, "
        "so a shop that installed twice reports each stay separately. `note` is the "
        "merchant's own words, verbatim and untranslated.\n"
        + (f"Filtered to the last {since_days} days. The bars above are all time by "
           "construction and are unaffected by this window.\n" if since_days else "")
        + (f"Filtered to the reason bucket {query.get('bucket')!r}.\n"
           if query.get("bucket") else ""),
        _json(rows),
        "\n## Stores that closed\n",
        _json(deaths),
    ])


def _retention(conn, settings, query: dict) -> str:
    return "\n".join([
        "# Retention\n",
        "Two cohort grids. `cells` is indexed by months since the cohort started, and "
        "each value is the percentage still retained. A cohort only has cells for months "
        "that have actually elapsed.\n",
        "## Installed retention\n",
        "Every shop that ever installed, by first-install month. A shop that uninstalled "
        "and came back counts as retained.\n",
        _json(stats.install_retention_cohorts(conn)),
        "\n## Paying retention\n",
        "Subscriptions only, by the month the subscription started.\n",
        _json(stats.retention_cohorts(conn)),
    ])


def _traffic(conn, settings, query: dict) -> str:
    days = choice(query.get("days"), TRAFFIC_DAYS, 90)
    summary = stats.traffic_summary(conn, days)
    return "\n".join([
        "# Traffic\n",
        "App Store listing traffic from GA4 property "
        f"{settings.ga4_property_id}, last {days} days. Installs here are the listing's "
        "own server-side event and will not match the Partner API install count exactly: "
        "ad and tracking blockers suppress some sessions, and reinstalls count "
        "differently.\n",
        _definitions("sessions", "add_app_clicks", "listing_installs", "install_rate",
                     "click_to_install", "ad_clicks"),
        "## Listing funnel\n",
        _json(summary),
        "\n## GA4 against the Partner API\n",
        "The same window counted two ways. `ga4_installs` is the browser-side "
        "`shopify_app_install` event; `partner_installs` is the Partner API's own record "
        "and is the truth. A positive `gap` is measurement loss -- consent banners, ad "
        "and tracking blockers, EU traffic -- not lost merchants, so every conversion "
        "rate in the funnel above is a floor rather than an estimate.\n",
        _json(stats.install_reconciliation(conn, days)),
        "\n## By month\n",
        "Always twelve months. This is the history rather than the window, so the range "
        "above does not move it.\n",
        _json(stats.traffic_monthly(conn)),
        "\n## Breakdowns\n",
        "`language` is the listing visitor's browser language, which is the only "
        "language signal that exists here: no upstream source "
        "carries a merchant locale.\n",
        _json({key: stats.traffic_breakdown(conn, key, days)
               for key in ("channel", "source", "country", "language")}),
    ])


def _faq(conn, settings, query: dict) -> str:
    """The same list partner_dashboard/faq.py renders at /faq, as prose.

    No JSON block: this page has no data, it has answers, and wrapping prose in
    a code fence would make a model treat it as a payload rather than as
    context.
    """
    parts = ["# Why the numbers don't match\n",
             "Almost every question this dashboard produces is a version of that one, and "
             "almost every answer is that the two figures measure different things and "
             "are both right.\n"]
    for question, paragraphs in FAQ:
        parts.append(f"## {question}\n")
        parts.extend(f"{paragraph}\n" for paragraph in paragraphs)
    return "\n".join(parts)


def render_page(conn, page: str, settings, query: dict | None = None,
                now: datetime | None = None) -> str:
    """Build one page's markdown. `page` is a key of PAGES, whitelisted by the
    caller; nothing from the request reaches a query except as a bound value."""
    query = query or {}
    now = now or datetime.now(timezone.utc)
    base_url = settings.public_base_url.rstrip("/")

    # Every renderer takes the query now, so a window a reader picked on the page
    # is honoured by its markdown twin too. A twin that ignored ?days= would
    # quietly stop being a mirror of what is on screen.
    body = {
        "overview": _overview, "customers": _customers, "actions": _actions,
        "funnel": _funnel, "churn": _churn, "retention": _retention,
        "traffic": _traffic, "faq": _faq,
    }[page](conn, settings, query)

    return "\n".join([_frontmatter(page, base_url, now, settings.app_name,
                                settings.dashboard_name),
                   body, "", READING_NOTES])
