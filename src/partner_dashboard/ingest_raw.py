import json
from decimal import Decimal

import psycopg

from partner_dashboard.config import get_settings

# AppSubscription carries no billing-interval field (confirmed by introspecting
# the Partner API 2026-08: it exposes only amount/billingOn/id/name/test), so the
# interval has to be inferred from the price. List your annual prices in
# ANNUAL_PLAN_AMOUNTS; everything else is treated as a 30-day plan.
#
# Getting this wrong is silent and expensive in both directions. An annual price
# missing from the list is counted at twelve times its real MRR. The way to
# confirm a price is annual is its charges: billingOn lands ~370 days after
# activation rather than ~30.
DEFAULT_PLAN_INTERVAL = "EVERY_30_DAYS"


def plan_interval_for(amount) -> str:
    annual = get_settings().annual_plan_amounts_set
    return "ANNUAL" if Decimal(str(amount)) in annual else DEFAULT_PLAN_INTERVAL


def upsert_raw_events(conn: psycopg.Connection, events: list[dict]) -> int:
    if not events:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for e in events:
            cur.execute(
                """
                insert into raw_app_events
                    (id, type, occurred_at, shop_gid, charge_gid, payload)
                values (%(id)s, %(type)s, %(occurred_at)s, %(shop_gid)s,
                        %(charge_gid)s, %(payload)s)
                on conflict (shop_gid, type, occurred_at, coalesce_charge)
                do update set payload = excluded.payload
                returning (xmax = 0) as was_inserted
                """,
                {**e, "payload": json.dumps(e.get("payload") or {})},
            )
            # Refreshing payload on conflict lets a widened GraphQL query
            # backfill fields we didn't originally ask for (uninstall reasons
            # were added this way) without a bespoke migration script. xmax = 0
            # distinguishes a real insert from that update, so raw_inserted
            # stays an honest count of genuinely new events.
            if cur.fetchone()[0]:
                inserted += 1
    conn.commit()
    return inserted


def upsert_transactions(conn: psycopg.Connection, rows: list[dict]) -> int:
    """Store the money feed, keyed on the Partner API's own transaction id.

    Amounts are refreshed on conflict rather than left alone: Shopify settles a
    charge some time after creating it, so a row can legitimately gain a
    net_amount after we first saw it. Returns the count of genuinely new rows,
    the same xmax = 0 trick upsert_raw_events uses, so an overlap-window replay
    does not read as a hundred fresh payments.
    """
    if not rows:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                insert into transactions
                    (id, type, created_at, shop_gid, charge_gid, billing_interval,
                     gross_amount, shopify_fee, net_amount, currency_code)
                values (%(id)s, %(type)s, %(created_at)s, %(shop_gid)s, %(charge_gid)s,
                        %(billing_interval)s, %(gross_amount)s, %(shopify_fee)s,
                        %(net_amount)s, %(currency_code)s)
                on conflict (id) do update set
                    gross_amount = excluded.gross_amount,
                    shopify_fee = excluded.shopify_fee,
                    net_amount = excluded.net_amount,
                    billing_interval = excluded.billing_interval
                returning (xmax = 0) as was_inserted
                """,
                row,
            )
            if cur.fetchone()[0]:
                inserted += 1
    conn.commit()
    return inserted


def upsert_charges(conn: psycopg.Connection, events: list[dict]) -> int:
    """Upsert charge rows from the AppSubscription objects inline on
    subscription events. The AppSubscription IS the subscription, so its gid
    doubles as subscription_id. plan_interval is inferred from the amount (see
    plan_interval_for) and is part of the update set, so correcting the mapping
    repairs charges already stored.
    """
    upserted = 0
    with conn.cursor() as cur:
        for e in events:
            charge = e.get("charge")
            if not charge:
                continue
            amount = charge["amount"]["amount"]
            cur.execute(
                """
                insert into charges
                    (gid, amount, currency_code, subscription_id,
                     plan_interval, plan_amount, test)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (gid) do update set
                    amount = excluded.amount,
                    currency_code = excluded.currency_code,
                    plan_interval = excluded.plan_interval,
                    plan_amount = excluded.plan_amount,
                    test = excluded.test
                """,
                (
                    charge["id"],
                    amount,
                    charge["amount"]["currencyCode"],
                    charge["id"],
                    plan_interval_for(amount),
                    amount,
                    bool(charge.get("test")),
                ),
            )
            upserted += cur.rowcount
    conn.commit()
    return upserted
