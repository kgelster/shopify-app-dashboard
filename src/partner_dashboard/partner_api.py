import logging

import httpx

logger = logging.getLogger(__name__)

# Live-verified against the Partner API 2026-07 (2026-08-07): AppEvent types expose
# app/shop/occurredAt/type (no appInstallation field exists), and every
# SubscriptionCharge* type carries charge: AppSubscription { id amount billingOn
# name test }. `type` is the AppEventTypes enum, already SCREAMING_SNAKE.
_CHARGE_FRAGMENT_TYPES = (
    "SubscriptionChargeAccepted",
    "SubscriptionChargeActivated",
    "SubscriptionChargeCanceled",
    "SubscriptionChargeDeclined",
    "SubscriptionChargeExpired",
    "SubscriptionChargeFrozen",
    "SubscriptionChargeUnfrozen",
)

_CHARGE_FRAGMENTS = "\n".join(
    f"          ... on {t} {{ charge {{ id amount {{ amount currencyCode }} billingOn name test }} }}"
    for t in _CHARGE_FRAGMENT_TYPES
)

# RelationshipUninstalled is the only event type carrying churn feedback:
# `reason` is a comma-separated pick-list (localized to the merchant's admin
# language) and `description` is their free-text note. Both land in `payload`,
# which derivation reads.
_UNINSTALL_FRAGMENT = (
    "          ... on RelationshipUninstalled { reason description }"
)

_APP_EVENTS_QUERY = f"""
query AppEvents($appId: ID!, $after: String) {{
  app(id: $appId) {{
    events(first: 100, after: $after) {{
      pageInfo {{ hasNextPage }}
      edges {{
        cursor
        node {{
          type
          occurredAt
          shop {{ id myshopifyDomain name }}
{_CHARGE_FRAGMENTS}
{_UNINSTALL_FRAGMENT}
        }}
      }}
    }}
  }}
}}
"""


# The money feed. A different root query from `app.events`, and the only source
# of what was actually collected: events describe subscription state, so refunds,
# credits and adjustments never appear in them at all.
#
# `types` is filtered server-side to the five app-revenue kinds. The others in
# TransactionType (THEME_*, SERVICE_*, REFERRAL*, TAX, LEGACY) belong to other
# Partner businesses we do not have.
TRANSACTION_TYPES = (
    "APP_SUBSCRIPTION_SALE",
    "APP_ONE_TIME_SALE",
    "APP_USAGE_SALE",
    "APP_SALE_ADJUSTMENT",
    "APP_SALE_CREDIT",
)

# Live-verified against the Partner API 2026-07 (2026-08-08): all five carry the
# same field set -- app/chargeId/createdAt/grossAmount/id/netAmount/shop/shopifyFee
# -- and only AppSubscriptionSale adds billingInterval. Transaction is an
# interface, not a union, so id and createdAt are selectable on the node itself.
_TRANSACTION_FRAGMENT_TYPES = (
    "AppSubscriptionSale",
    "AppOneTimeSale",
    "AppUsageSale",
    "AppSaleAdjustment",
    "AppSaleCredit",
)

_TRANSACTION_FIELDS = """
            chargeId
            shop { id myshopifyDomain name }
            grossAmount { amount currencyCode }
            shopifyFee  { amount currencyCode }
            netAmount   { amount currencyCode }"""

_TRANSACTION_FRAGMENTS = "\n".join(
    f"          ... on {t} {{{_TRANSACTION_FIELDS}"
    + ("\n            billingInterval\n          }" if t == "AppSubscriptionSale" else "\n          }")
    for t in _TRANSACTION_FRAGMENT_TYPES
)

_TRANSACTIONS_QUERY = f"""
query Transactions($appId: ID!, $after: String, $createdAtMin: DateTime) {{
  transactions(appId: $appId, first: 100, after: $after,
               createdAtMin: $createdAtMin,
               types: [{", ".join(TRANSACTION_TYPES)}]) {{
    pageInfo {{ hasNextPage }}
    edges {{
      cursor
      node {{
        __typename
        id
        createdAt
{_TRANSACTION_FRAGMENTS}
      }}
    }}
  }}
}}
"""


# The Partner API version every query here is written against. Bumping it is a
# deliberate act: the field set this code reads has changed between versions.
API_VERSION = "2026-07"


class PartnerClient:
    def __init__(self, token: str, org_id: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=f"https://partners.shopify.com/{org_id}/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token},
            transport=transport,
        )


def fetch_app_events(
    client: PartnerClient, *, app_id: str, after_cursor: str | None = None
) -> tuple[list[dict], str | None]:
    response = client.http.post(
        "",
        json={
            "query": _APP_EVENTS_QUERY,
            "variables": {"appId": app_id, "after": after_cursor},
        },
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Partner API GraphQL errors: {body['errors']}")

    connection = body["data"]["app"]["events"]
    edges = connection["edges"]
    has_next_page = connection["pageInfo"]["hasNextPage"]

    events = []
    for edge in edges:
        try:
            node = edge["node"]
            charge = node.get("charge") or None
            event_type = node["type"]
            occurred_at = node["occurredAt"]
            shop = node["shop"]
            shop_gid = shop["id"]
            charge_gid = charge["id"] if charge else None
            events.append({
                # AppEvent has no id field of its own; compose one matching
                # the upsert_raw_events dedupe key (shop, type, time, charge).
                "id": f"{shop_gid}:{event_type}:{occurred_at}:{charge_gid or ''}",
                "type": event_type,
                "occurred_at": occurred_at,
                "shop_gid": shop_gid,
                "shop_domain": shop.get("myshopifyDomain"),
                "shop_name": shop.get("name"),
                "charge_gid": charge_gid,
                "charge": charge,
                "payload": node,
            })
        except (KeyError, TypeError):
            logger.exception("skipping unmappable app event node: %r", edge)

    next_cursor = edges[-1]["cursor"] if edges and has_next_page else None
    return events, next_cursor


def _money(node: dict, key: str) -> tuple[str | None, str | None]:
    """(amount, currencyCode) for one Money field. Only netAmount is non-null in
    the schema, so gross and fee have to survive being absent."""
    money = node.get(key) or {}
    return money.get("amount"), money.get("currencyCode")


def fetch_transactions(
    client: PartnerClient, *, app_id: str, after_cursor: str | None = None,
    created_at_min: str | None = None,
) -> tuple[list[dict], str | None]:
    """One page of the money feed.

    `created_at_min` is a real time bound, unlike the events cursor, so an
    incremental poll can rewind a window rather than trusting an opaque token.
    """
    response = client.http.post(
        "",
        json={
            "query": _TRANSACTIONS_QUERY,
            "variables": {
                "appId": app_id,
                "after": after_cursor,
                "createdAtMin": created_at_min,
            },
        },
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Partner API GraphQL errors: {body['errors']}")

    connection = body["data"]["transactions"]
    edges = connection["edges"]
    has_next_page = connection["pageInfo"]["hasNextPage"]

    rows = []
    for edge in edges:
        try:
            node = edge["node"]
            shop = node.get("shop") or {}
            gross, gross_currency = _money(node, "grossAmount")
            fee, _ = _money(node, "shopifyFee")
            net, net_currency = _money(node, "netAmount")
            rows.append({
                "id": node["id"],
                "type": node["__typename"],
                "created_at": node["createdAt"],
                "shop_gid": shop.get("id"),
                "shop_domain": shop.get("myshopifyDomain"),
                "shop_name": shop.get("name"),
                "charge_gid": node.get("chargeId"),
                "billing_interval": node.get("billingInterval"),
                "gross_amount": gross,
                "shopify_fee": fee,
                "net_amount": net,
                "currency_code": net_currency or gross_currency,
            })
        except (KeyError, TypeError):
            logger.exception("skipping unmappable transaction node: %r", edge)

    next_cursor = edges[-1]["cursor"] if edges and has_next_page else None
    return rows, next_cursor
