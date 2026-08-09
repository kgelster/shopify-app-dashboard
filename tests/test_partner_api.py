import httpx
import pytest

from partner_dashboard.partner_api import fetch_app_events, fetch_transactions, PartnerClient


def _client(payload, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request.read().decode())
        return httpx.Response(status, json=payload)

    return PartnerClient("tok", "1", transport=httpx.MockTransport(handler))


def test_maps_nodes_and_returns_cursor():
    payload = {"data": {"app": {"events": {
        "pageInfo": {"hasNextPage": True},
        "edges": [{"cursor": "cur1", "node": {
            "type": "RELATIONSHIP_INSTALLED",
            "occurredAt": "2026-06-01T00:00:00Z",
            "shop": {"id": "gid://partners/Shop/1", "myshopifyDomain": "x.myshopify.com",
                     "name": "X"}}}]}}}}
    events, cursor = fetch_app_events(_client(payload), app_id="2")
    assert cursor == "cur1"
    assert events[0]["type"] == "RELATIONSHIP_INSTALLED"
    assert events[0]["shop_gid"] == "gid://partners/Shop/1"
    assert events[0]["shop_domain"] == "x.myshopify.com"
    assert events[0]["charge"] is None


def test_maps_inline_charge():
    payload = {"data": {"app": {"events": {
        "pageInfo": {"hasNextPage": False},
        "edges": [{"cursor": "cur1", "node": {
            "type": "SUBSCRIPTION_CHARGE_ACTIVATED",
            "occurredAt": "2026-06-02T00:00:00Z",
            "shop": {"id": "gid://partners/Shop/1", "myshopifyDomain": "x.myshopify.com",
                     "name": "X"},
            "charge": {"id": "gid://partners/AppSubscription/9",
                       "amount": {"amount": "19.0", "currencyCode": "USD"},
                       "billingOn": None, "name": "Pro", "test": False}}}]}}}}
    events, cursor = fetch_app_events(_client(payload), app_id="2")
    assert cursor is None            # hasNextPage false -> drained
    assert events[0]["charge_gid"] == "gid://partners/AppSubscription/9"
    assert events[0]["charge"]["amount"]["amount"] == "19.0"


def test_graphql_errors_raise_instead_of_keyerror():
    payload = {"data": None, "errors": [{"message": "Invalid API version"}]}
    with pytest.raises(RuntimeError, match="Invalid API version"):
        fetch_app_events(_client(payload), app_id="2")


def _transactions(*nodes, has_next=False):
    return {"data": {"transactions": {
        "pageInfo": {"hasNextPage": has_next},
        "edges": [{"cursor": f"cur{i}", "node": n} for i, n in enumerate(nodes)],
    }}}


SUB_SALE = {
    "__typename": "AppSubscriptionSale",
    "id": "gid://partners/AppSubscriptionSale/764220980",
    "createdAt": "2026-08-07T07:02:51.000000Z",
    "chargeId": "gid://shopify/AppSubscription/31499911397",
    "billingInterval": "EVERY_30_DAYS",
    "shop": {"id": "gid://partners/Shop/1", "myshopifyDomain": "x.myshopify.com",
             "name": "X"},
    # The real shape, copied from a live response: shopifyFee is 0.00 (the
    # revenue share, which is 0% under $1M) while net is 2.9% below gross,
    # because the processing fee only ever shows up in that gap.
    "grossAmount": {"amount": "19.0", "currencyCode": "USD"},
    "shopifyFee": {"amount": "0.0", "currencyCode": "USD"},
    "netAmount": {"amount": "18.45", "currencyCode": "USD"},
}


def test_maps_transaction_and_keeps_real_id():
    rows, cursor = fetch_transactions(_client(_transactions(SUB_SALE)), app_id="2")
    assert cursor is None
    row = rows[0]
    # The Partner API id, verbatim -- not a composed key like app events need.
    assert row["id"] == "gid://partners/AppSubscriptionSale/764220980"
    assert row["type"] == "AppSubscriptionSale"
    assert row["shop_gid"] == "gid://partners/Shop/1"
    assert row["billing_interval"] == "EVERY_30_DAYS"
    assert (row["gross_amount"], row["shopify_fee"], row["net_amount"]) == \
        ("19.0", "0.0", "18.45")
    assert row["currency_code"] == "USD"


def test_adjustment_has_no_billing_interval():
    adjustment = {
        "__typename": "AppSaleAdjustment",
        "id": "gid://partners/AppSaleAdjustment/5",
        "createdAt": "2026-07-01T00:00:00Z",
        "chargeId": None,
        "shop": {"id": "gid://partners/Shop/1", "myshopifyDomain": "x.myshopify.com",
                 "name": "X"},
        "grossAmount": {"amount": "-19.0", "currencyCode": "USD"},
        "shopifyFee": {"amount": "0.0", "currencyCode": "USD"},
        "netAmount": {"amount": "-18.45", "currencyCode": "USD"},
    }
    rows, _ = fetch_transactions(_client(_transactions(adjustment)), app_id="2")
    assert rows[0]["billing_interval"] is None
    assert rows[0]["gross_amount"] == "-19.0"


def test_passes_created_at_min_and_paginates():
    seen = []
    client = _client(_transactions(SUB_SALE, has_next=True), seen=seen)
    rows, cursor = fetch_transactions(client, app_id="2",
                                      created_at_min="2026-08-01T00:00:00Z")
    assert cursor == "cur0"
    assert '"createdAtMin":"2026-08-01T00:00:00Z"' in seen[0]


def test_transactions_graphql_errors_raise():
    payload = {"data": None, "errors": [{"message": "Access denied"}]}
    with pytest.raises(RuntimeError, match="Access denied"):
        fetch_transactions(_client(payload), app_id="2")
