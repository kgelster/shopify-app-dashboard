import logging

import httpx
import psycopg

logger = logging.getLogger(__name__)

HEADERS = {
    "installed": ":tada: New install",
    "reinstalled": ":recycle: Reinstalled",
    "uninstalled": ":wave: Uninstalled",
}

# Bulk-replay guard: a first-ever derivation over a fresh database emits the
# entire event history as "new"; never turn that into an alert storm.
MAX_ALERTS_PER_SYNC = 20


def escape(text: str) -> str:
    """Escape a merchant-controlled string for Slack mrkdwn.

    Shop names come from the merchant and go straight into an alert. The three
    characters Slack reserves are &, < and > (escaped in that order, so the
    ampersands introduced by the last two are not escaped again). `|` is dropped
    rather than escaped because Slack defines no escape for it and it is the
    label separator inside a link.

    Without this a shop named `x|https://evil.example` or one containing `>`
    could close the link built below early and put an arbitrary label on a URL
    pointing somewhere else -- an internal alert that reads as one merchant and
    navigates to another. Merchants do put these characters in store names.
    """
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("|", "-"))


def build_event_message(shop: dict, kind: str, base_url: str | None = None) -> dict:
    header = HEADERS.get(kind, kind)
    name = escape(shop.get("shop_name") or shop.get("shop_domain") or "Unknown")
    domain = shop.get("shop_domain")
    # Link the headline to the merchant's own page, so an alert is one click
    # from their timeline, payments and uninstall reason instead of a name to
    # go and search for. The domain is a Shopify-issued myshopify hostname, but
    # it is escaped as well rather than trusted by provenance.
    if base_url and domain:
        name = f"<{base_url.rstrip('/')}/customers/{escape(domain)}|{name}>"
    fields = [
        f"*Shop:*\n{escape(shop.get('shop_name') or 'Unknown')}",
        f"*Domain:*\n{escape(domain or 'Unknown')}",
        f"*Country:*\n{escape(shop.get('country') or 'Unknown')}",
        f"*Plan:*\n{escape(shop.get('plan') or 'Unknown')}",
    ]
    return {
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{header}: {name}"},
            },
            {
                "type": "section",
                "fields": [{"type": "mrkdwn", "text": field} for field in fields],
            },
        ]
    }


def post_alert(webhook_url: str, payload: dict, http_post=httpx.post) -> bool:
    response = http_post(webhook_url, json=payload)
    if 200 <= response.status_code < 300:
        return True
    logger.warning("slack alert failed: status=%s", response.status_code)
    return False


def _load_shop(conn: psycopg.Connection, shop_gid: str) -> dict | None:
    row = conn.execute(
        """
        select s.shop_name, s.shop_domain, s.country,
               sub.monthly_amount
        from shops s
        left join subscriptions sub on sub.shop_gid = s.shop_gid
        where s.shop_gid = %s
        order by (sub.churned_at is null) desc, sub.converted_at desc nulls last
        limit 1
        """,
        (shop_gid,),
    ).fetchone()
    if row is None:
        return None
    shop_name, shop_domain, country, monthly_amount = row
    return {
        "shop_name": shop_name,
        "shop_domain": shop_domain,
        "country": country,
        "plan": f"${monthly_amount}/mo" if monthly_amount is not None else "Unknown",
    }


def notify_events(conn: psycopg.Connection, events: list[tuple[str, str]],
                   webhook_url: str | None, http_post=httpx.post,
                   base_url: str | None = None) -> int:
    """Post one Slack alert per (shop_gid, clean_type) event.

    Missing fields render as Unknown: a fresh live install has shop name and
    domain from the events feed, but country arrives only with enrichment.
    """
    if not webhook_url:
        logger.info("SLACK_WEBHOOK_URL unset; skipping %d alert(s)", len(events))
        return 0
    if len(events) > MAX_ALERTS_PER_SYNC:
        logger.warning(
            "%d alertable events in one sync (bulk replay?); capping at %d",
            len(events), MAX_ALERTS_PER_SYNC,
        )
        events = events[:MAX_ALERTS_PER_SYNC]

    sent = 0
    for shop_gid, kind in events:
        shop = _load_shop(conn, shop_gid)
        if shop is None:
            logger.warning("notify_events: no shop row for %s", shop_gid)
            continue
        message = build_event_message(shop, kind, base_url)
        if post_alert(webhook_url, message, http_post=http_post):
            sent += 1
    return sent
