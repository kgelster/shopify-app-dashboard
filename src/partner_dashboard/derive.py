import logging
from decimal import Decimal

import psycopg

from partner_dashboard.normalize import normalize_monthly
from partner_dashboard.shops import upsert_shop_state

logger = logging.getLogger(__name__)

INSTALL_TYPES = {
    "RELATIONSHIP_INSTALLED": "installed",
    "RELATIONSHIP_REACTIVATED": "reinstalled",
}
UNINSTALL_TYPES = {"RELATIONSHIP_DEACTIVATED", "RELATIONSHIP_UNINSTALLED"}
ACTIVATION_TYPES = {"SUBSCRIPTION_CHARGE_ACTIVATED", "SUBSCRIPTION_CHARGE_ACCEPTED"}
CHURN_TYPES = {
    "SUBSCRIPTION_CHARGE_CANCELED",
    "SUBSCRIPTION_CHARGE_EXPIRED",
    "SUBSCRIPTION_CHARGE_FROZEN",
}


def _get_charge(conn: psycopg.Connection, charge_gid: str) -> dict | None:
    row = conn.execute(
        """select amount, currency_code, subscription_id, plan_interval, plan_amount, flex_billing,
                  test
           from charges where gid = %s""",
        (charge_gid,),
    ).fetchone()
    if row is None:
        # Charges are upserted from the events feed itself (upsert_charges runs
        # before derivation in the pipeline), so a miss here means the event
        # arrived without an inline charge object. Skip instead of raising --
        # a raise used to abort derive_all_dirty entirely and permanently stall
        # the sync cursor.
        logger.warning(
            "charge %r not found; charges must be synced before derivation -- skipping event",
            charge_gid,
        )
        return None
    amount, currency_code, subscription_id, plan_interval, plan_amount, flex_billing, test = row
    if test:
        logger.debug("charge %r is a test charge -- excluding from derivation", charge_gid)
        return None
    basis = plan_amount if flex_billing else amount
    return {
        "subscription_id": subscription_id,
        "plan_interval": plan_interval,
        "plan_amount": basis,
        "currency_code": currency_code,
        "monthly": normalize_monthly(basis, plan_interval),
    }


def _insert_event(conn, shop_gid, platform_event_id, clean_type, occurred_at,
                   net_change, charge=None, previous_subscription_id=None,
                   uninstall_reason=None, uninstall_description=None):
    conn.execute(
        """
        insert into app_events
            (platform_event_id, type, occurred_at, net_change, plan_amount, plan_interval,
             plan_currency_code, previous_subscription_id, shop_gid,
             uninstall_reason, uninstall_description)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (platform_event_id) do update set
            -- Only the churn-feedback columns refresh on replay, so a widened
            -- query backfills history. Everything else stays immutable, and the
            -- row keeps its id, which derive_all_dirty's high-water mark needs.
            uninstall_reason = coalesce(excluded.uninstall_reason,
                                        app_events.uninstall_reason),
            uninstall_description = coalesce(excluded.uninstall_description,
                                             app_events.uninstall_description)
        """,
        (
            platform_event_id, clean_type, occurred_at, net_change,
            charge["plan_amount"] if charge else None,
            charge["plan_interval"] if charge else None,
            charge["currency_code"] if charge else None,
            previous_subscription_id,
            shop_gid,
            uninstall_reason,
            uninstall_description,
        ),
    )


def _upsert_subscription(conn, sub_id, shop_gid, monthly_amount=None,
                          converted_at=None, churned_at=None, clear_churn=False):
    """Upsert one subscription's state.

    `clear_churn` un-churns a subscription id that is activating again. The
    plain coalesce below cannot express that, and without it a shop that
    uninstalled (which now churns whatever was live) and later reinstalled onto
    the same subscription id would stay churned forever -- live money missing
    from every figure. Replay order makes this safe: a later cancel in the same
    pass re-writes churned_at after this clears it.
    """
    conn.execute(
        """
        insert into subscriptions (id, shop_gid, monthly_amount, converted_at, churned_at)
        values (%s, %s, %s, %s, %s)
        on conflict (id) do update set
            shop_gid = excluded.shop_gid,
            monthly_amount = coalesce(excluded.monthly_amount, subscriptions.monthly_amount),
            converted_at = coalesce(excluded.converted_at, subscriptions.converted_at),
            churned_at = case when %s then null
                              else coalesce(excluded.churned_at, subscriptions.churned_at) end
        """,
        (sub_id, shop_gid, monthly_amount, converted_at, churned_at, clear_churn),
    )


def derive_installation(conn: psycopg.Connection, shop_gid: str) -> list[str]:
    """Replay one install's raw events in order, upsert clean app_events + subscriptions.

    Re-running is idempotent: app_events insert is keyed on platform_event_id
    (on conflict do nothing); subscriptions state is recomputed from the full
    replay each run so it converges to the same values.
    """
    rows = conn.execute(
        """
        select id, type, occurred_at, charge_gid,
               payload->'shop'->>'myshopifyDomain' as shop_domain,
               payload->'shop'->>'name' as shop_name,
               payload->>'reason' as uninstall_reason,
               payload->>'description' as uninstall_description
        from raw_app_events
        where shop_gid = %s
        order by occurred_at, id
        """,
        (shop_gid,),
    ).fetchall()

    # What this shop is paying, per subscription id, not as one running total.
    #
    # A scalar was wrong in both directions on a plan change, because Shopify
    # does not edit a subscription when a merchant switches plans: it activates
    # a NEW AppSubscription and cancels the old one. In the feed that is
    # `subscribed -> upgraded -> unsubscribed`, usually within a day. Against a
    # scalar the second activation looked like an edit (so the new subscription
    # was stored with no converted_at) and the trailing cancel of the *old*
    # subscription zeroed the shop even though its replacement was live.
    live: dict[str, Decimal] = {}
    # Subscription ids already converted in this replay. converted_at is written
    # once, on a subscription's first activation; a later activation of the same
    # id must not push its conversion date forward.
    converted: set[str] = set()
    most_recent_subscription = None
    emitted: list[str] = []

    def total() -> Decimal:
        return sum(live.values(), Decimal("0"))

    for (raw_id, raw_type, occurred_at, charge_gid, shop_domain, shop_name,
         uninstall_reason, uninstall_description) in rows:
        if raw_type in INSTALL_TYPES:
            clean_type = INSTALL_TYPES[raw_type]
            _insert_event(conn, shop_gid, raw_id, clean_type, occurred_at, Decimal("0"))
            upsert_shop_state(conn, shop_gid, install_state="installed", at=occurred_at,
                              shop_domain=shop_domain, shop_name=shop_name)
            emitted.append(clean_type)

        elif raw_type in UNINSTALL_TYPES:
            net_change = -total()
            _insert_event(conn, shop_gid, raw_id, "uninstalled", occurred_at, net_change,
                          uninstall_reason=uninstall_reason,
                          uninstall_description=uninstall_description)
            upsert_shop_state(conn, shop_gid, install_state="uninstalled", at=occurred_at,
                              shop_domain=shop_domain, shop_name=shop_name,
                              uninstall_reason=uninstall_reason,
                              uninstall_description=uninstall_description)
            emitted.append("uninstalled")
            # An uninstall ends every subscription the shop still had. Shopify
            # usually sends a cancel or expiry alongside, but does not promise
            # one, and dropping the subscription from `live` without recording
            # churned_at left it live forever in every figure that reads
            # subscriptions without joining shops -- the MRR chart, mrr_at,
            # paying_at, the retention cohorts -- while the Active MRR tile
            # (which does join, on install_state) had already let it go. Two
            # tiles, one dataset, different answers.
            for sub_id in live:
                _upsert_subscription(conn, sub_id, shop_gid, churned_at=occurred_at)
            live.clear()

        elif raw_type in ACTIVATION_TYPES:
            charge = _get_charge(conn, charge_gid)
            if charge is None:
                continue
            new_monthly = charge["monthly"]
            sub_id = charge["subscription_id"]
            previous_subscription_id = (
                most_recent_subscription if most_recent_subscription != sub_id else None
            )

            was_paying = bool(live)
            before = total()
            live[sub_id] = new_monthly
            net_change = total() - before

            # Labels are unchanged from the scalar version on purpose: app_events
            # rows are immutable once written, so relabelling here would only
            # affect new rows and leave the history disagreeing with itself.
            if not was_paying:
                clean_type = "subscribed"
            elif net_change >= 0:
                clean_type = "upgraded"
            else:
                clean_type = "downgraded"

            # Every subscription gets a converted_at, including one minted by a
            # plan change. Without this the row is invisible to mrr_trend,
            # mrr_movements, retention_cohorts and paying_at, all of which filter
            # on converted_at -- so the MRR chart read below the MRR tile.
            _upsert_subscription(
                conn, sub_id, shop_gid, monthly_amount=new_monthly,
                converted_at=None if sub_id in converted else occurred_at,
                clear_churn=True,
            )
            converted.add(sub_id)

            _insert_event(conn, shop_gid, raw_id, clean_type, occurred_at, net_change,
                          charge=charge, previous_subscription_id=previous_subscription_id)
            emitted.append(clean_type)
            most_recent_subscription = sub_id

        elif raw_type in CHURN_TYPES:
            charge = _get_charge(conn, charge_gid)
            if charge is None:
                continue
            # Only this subscription stops. Cancelling the subscription a plan
            # change superseded must not zero out its replacement.
            net_change = -live.pop(charge["subscription_id"], Decimal("0"))
            _insert_event(conn, shop_gid, raw_id, "unsubscribed", occurred_at,
                          net_change, charge=charge)
            _upsert_subscription(conn, charge["subscription_id"], shop_gid,
                                  churned_at=occurred_at)
            emitted.append("unsubscribed")

    conn.commit()
    return emitted


def derive_all_dirty(conn: psycopg.Connection, since) -> dict[str, int]:
    installs = [
        row[0]
        for row in conn.execute(
            "select distinct shop_gid from raw_app_events where ingested_at >= %s",
            (since,),
        ).fetchall()
    ]
    # Snapshot the high-water mark before replaying: derive_installation
    # recomputes and re-emits the full history for every touched install on
    # every call, so counting its return value would double-count events
    # already recorded on a prior run. Only rows with a genuinely new
    # (post-snapshot) id are new app_events, since ON CONFLICT (platform_event_id)
    # DO NOTHING means already-seen events never get a new id.
    (snapshot,) = conn.execute("select coalesce(max(id), 0) from app_events").fetchone()

    for shop_gid in installs:
        derive_installation(conn, shop_gid)

    counts: dict[str, int] = {
        row[0]: row[1]
        for row in conn.execute(
            "select type, count(*) from app_events where id > %s group by type",
            (snapshot,),
        ).fetchall()
    }
    return counts


def derive_installations(conn: psycopg.Connection, ids) -> dict[str, int]:
    """Derive an explicit set of installs (e.g. those touched by the current
    sync), isolating failures so one bad install can't abort the batch --
    unlike derive_all_dirty's plain loop, a raise here is caught and logged
    per-install so the rest still land and the sync cursor keeps advancing.
    """
    (snapshot,) = conn.execute("select coalesce(max(id), 0) from app_events").fetchone()

    for shop_gid in ids:
        try:
            derive_installation(conn, shop_gid)
        except Exception:
            logger.warning(
                "derive_installation failed for %r; skipping", shop_gid, exc_info=True
            )

    counts: dict[str, int] = {
        row[0]: row[1]
        for row in conn.execute(
            "select type, count(*) from app_events where id > %s group by type",
            (snapshot,),
        ).fetchall()
    }
    return counts
