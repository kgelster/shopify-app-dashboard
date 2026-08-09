import psycopg
from psycopg.rows import dict_row

SORTS = {
    "installed_at": "installed_at desc nulls last",
    "shop_name": "shop_name asc",
    "country": "country asc",
    "industry": "industry asc",
}


INSTALL_STATES = ("installed", "uninstalled")

# Billing intervals a shop can be filtered by. A whitelist rather than a facet
# query: these two are the whole plan catalogue, and an unrecognised value has
# to fall through to "no filter" rather than to an empty page.
PLAN_INTERVALS = ("EVERY_30_DAYS", "ANNUAL")


def _filters(industry, country, search, install_state, plan=None) -> tuple[str, list]:
    """Build the additive WHERE shared by the page query and its count.

    Every value is a bound parameter; nothing here is interpolated into SQL.
    """
    where = []
    params = []
    if install_state in INSTALL_STATES:
        where.append("install_state = %s")
        params.append(install_state)
    if plan in PLAN_INTERVALS:
        # Live subscriptions only: this is "who is on annual right now", which
        # is what the plan-mix bars on Overview count and therefore what they
        # have to link to. A shop that churned off annual is not on annual.
        where.append(
            """shop_gid in (
                   select sub.shop_gid from subscriptions sub
                   join charges c on c.gid = sub.id
                   where sub.churned_at is null and c.plan_interval = %s
               )"""
        )
        params.append(plan)
    if industry is not None:
        where.append("industry = %s")
        params.append(industry)
    if country is not None:
        where.append("country = %s")
        params.append(country)
    if search is not None:
        where.append("(shop_name ilike %s or shop_domain ilike %s)")
        needle = f"%{search}%"
        params.extend([needle, needle])
    return (f"where {' and '.join(where)}" if where else ""), params


def list_customers(conn: psycopg.Connection, *, industry=None, country=None,
                    search=None, install_state=None, plan=None,
                    sort="installed_at", limit=100, offset=0) -> list[dict]:
    """Filter shops by industry/country/state/plan/search (additive), sorted via whitelist."""
    where_sql, params = _filters(industry, country, search, install_state, plan)
    order_sql = SORTS.get(sort, SORTS["installed_at"])
    params.extend([limit, offset])

    cur = conn.cursor(row_factory=dict_row)
    # Columns named, not `select *`. shops still HAS owner_name and email; they
    # are emptied by migration 008 and the template does not render them, so
    # nothing leaks today. But `select *` means the no-PII rule is enforced by
    # two downstream strippers rather than by the query, and this dict is
    # rendered into a copyable .md document. Enforce it where the data is read.
    cur.execute(
        f"""select shop_gid, shop_domain, shop_name, country, industry,
                   install_state, installed_at, uninstalled_at,
                   uninstall_reason, uninstall_description, reviewed_at
            from shops {where_sql} order by {order_sql} limit %s offset %s""",
        params,
    )
    return cur.fetchall()


def count_customers(conn: psycopg.Connection, *, industry=None, country=None,
                    search=None, install_state=None, plan=None) -> int:
    """How many rows the same filters match, ignoring limit/offset. Without this
    the page can only say "here are 50 rows", not "50 of 119"."""
    where_sql, params = _filters(industry, country, search, install_state, plan)
    return conn.execute(f"select count(*) from shops {where_sql}", params).fetchone()[0]


# Lifecycle rows the timeline renders, and the label each gets. usage_events are
# folded in separately (they have their own table and their own vocabulary).
EVENT_LABELS = {
    "installed": "Installed",
    "reinstalled": "Reinstalled",
    "uninstalled": "Uninstalled",
    "subscribed": "Subscribed",
    "upgraded": "Upgraded",
    "downgraded": "Downgraded",
    "unsubscribed": "Subscription ended",
}


def customer_detail(conn: psycopg.Connection, shop_domain: str) -> dict | None:
    """Everything known about one merchant, on one page.

    The header is derived from the same rows the timeline draws, never from a
    separate query. Computing the two independently is how a page ends up saying
    "still around" above a timeline whose last event is an uninstall, and the
    only way to be sure they cannot disagree is to compute one from the other.

    Deliberately carries no contact details. shops.owner_name and shops.email
    exist for merchants backfilled by the CSV importer, and neither is read
    here; see migration 008 for why that data is not trustworthy anyway.
    """
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """select shop_gid, shop_name, shop_domain, country, industry,
                  install_state, installed_at, uninstalled_at,
                  uninstall_reason, uninstall_description, reviewed_at
           from shops where shop_domain = %s
           -- shop_domain has no unique constraint (shop_gid is the key), and a
           -- merchant who renamed their store could in principle free a domain
           -- for someone else. Ordering makes the page deterministic instead of
           -- picking whichever row the planner returned first.
           order by installed_at desc nulls last, shop_gid limit 1""",
        (shop_domain,),
    )
    shop = cur.fetchone()
    if shop is None:
        return None
    gid = shop["shop_gid"]

    lifecycle = conn.execute(
        """
        select e.type, e.occurred_at, e.plan_amount, e.plan_interval, e.net_change,
               e.uninstall_reason, e.uninstall_description,
               r.type as raw_type
        from app_events e
        left join raw_app_events r on r.id = e.platform_event_id
        where e.shop_gid = %s
        order by e.occurred_at, e.id
        """,
        (gid,),
    ).fetchall()

    timeline = []
    for (kind, at, amount, interval, net_change, reason, note, raw_type) in lifecycle:
        # RELATIONSHIP_DEACTIVATED is folded into 'uninstalled' upstream, but a
        # store Shopify closed did not choose to leave and was never surveyed.
        # Saying so here is the difference between "they left us" and "they went
        # out of business".
        label = EVENT_LABELS.get(kind, kind)
        if kind == "uninstalled" and raw_type == "RELATIONSHIP_DEACTIVATED":
            label = "Store closed or frozen by Shopify"
        timeline.append({
            "at": at, "kind": kind, "label": label,
            "amount": amount, "interval": interval, "net_change": net_change,
            "reason": reason, "note": note,
            "chose_to_leave": raw_type == "RELATIONSHIP_UNINSTALLED",
        })

    payments = conn.execute(
        """select id, type, created_at, gross_amount, shopify_fee, net_amount,
                  billing_interval
           from transactions where shop_gid = %s order by created_at desc""",
        (gid,),
    ).fetchall()
    money = {
        "gross": sum((p[3] or 0) for p in payments),
        "net": sum((p[5] or 0) for p in payments),
        "count": len(payments),
        "first_at": min((p[2] for p in payments), default=None),
        "last_at": max((p[2] for p in payments), default=None),
    }
    money["taken"] = money["gross"] - money["net"]

    subscription = conn.execute(
        """select sub.monthly_amount, sub.converted_at, sub.churned_at,
                  -- charges.plan_interval is inferred from the price (see
                  -- ingest_raw.plan_interval_for) and is null when no charge row
                  -- was ever captured. The transaction feed states the interval
                  -- outright, so it wins; without this fallback an annual plan
                  -- renders as "$16 /mo" and looks like a cheap monthly one.
                  coalesce(c.plan_interval, (
                      select t.billing_interval from transactions t
                      where t.shop_gid = sub.shop_gid and t.billing_interval is not null
                      order by t.created_at desc limit 1
                  ))
           from subscriptions sub left join charges c on c.gid = sub.id
           where sub.shop_gid = %s order by sub.converted_at desc nulls last limit 1""",
        (gid,),
    ).fetchone()

    usage = conn.execute(
        """select event_type, count(*), min(occurred_at), max(occurred_at)
           from usage_events where shop_gid = %s group by event_type
           order by 2 desc""",
        (gid,),
    ).fetchall()

    # The header, read off the rows above rather than queried separately.
    installs = [t for t in timeline if t["kind"] in ("installed", "reinstalled")]
    return {
        "shop": shop,
        "timeline": timeline,
        "payments": [
            {"id": i, "type": t, "at": at, "gross": g, "fee": f, "net": n,
             "interval": iv}
            for i, t, at, g, f, n, iv in payments
        ],
        "money": money,
        "subscription": (
            {"monthly_amount": subscription[0], "converted_at": subscription[1],
             "churned_at": subscription[2], "plan_interval": subscription[3]}
            if subscription else None
        ),
        "usage": [
            {"event_type": t, "count": n, "first_at": first, "last_at": last}
            for t, n, first, last in usage
        ],
        "first_install_at": installs[0]["at"] if installs else None,
        "install_count": len(installs),
        # Whatever the timeline's last lifecycle row says, so the two cannot
        # contradict each other. Collapsed to the two states a shop can be in:
        # "reinstalled" is an event, not a state, and reading it as one is how
        # a page ends up claiming a merchant is churned above a timeline that
        # says they came back.
        "current_state": (
            "uninstalled" if next(
                (t["kind"] for t in reversed(timeline)
                 if t["kind"] in ("installed", "reinstalled", "uninstalled")),
                shop["install_state"],
            ) == "uninstalled" else "installed"
        ),
    }


def distinct_facets(conn: psycopg.Connection) -> dict:
    """Distinct non-null industries/countries for filter dropdowns."""
    industries = [
        row[0] for row in conn.execute(
            "select distinct industry from shops where industry is not null order by industry"
        ).fetchall()
    ]
    countries = [
        row[0] for row in conn.execute(
            "select distinct country from shops where country is not null order by country"
        ).fetchall()
    ]
    states = [
        row[0] for row in conn.execute(
            "select distinct install_state from shops "
            "where install_state is not null order by install_state"
        ).fetchall()
    ]
    return {"industries": industries, "countries": countries, "states": states}
