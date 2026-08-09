import logging
from datetime import datetime

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

from partner_dashboard.digest import send_weekly_digest
from partner_dashboard.ops import check_stale_sync
from partner_dashboard.partner_api import PartnerClient
from partner_dashboard.pipeline import run_sync, sync_transactions

logger = logging.getLogger(__name__)

WEEKLY_DIGEST_JOB_ID = "weekly_digest"


def run_sync_job(conn_factory, client, settings) -> None:
    """One scheduler tick: open a connection, run the sync, always close it.

    conn_factory in production is partner_dashboard.db.connect (a fresh connection per
    call); leaving it open would leak a Postgres connection every poll.
    """
    conn = conn_factory()
    try:
        summary = run_sync(conn, client, settings, http_post=httpx.post)
        logger.info("run_sync completed: %s", summary)
    finally:
        conn.close()


def run_transactions_job(conn_factory, client, settings) -> None:
    """Poll the money feed. Its own job, and its own try/except: a failure here
    must not take the lifecycle sync down, because the events feed is what the
    install/uninstall alerts run on."""
    conn = conn_factory()
    try:
        summary = sync_transactions(conn, client, settings)
        logger.info("sync_transactions completed: %s", summary)
    except Exception:
        logger.exception("transactions sync failed")
    finally:
        conn.close()


def run_stale_check_job(conn_factory, settings) -> None:
    """Shout in Slack if the Partner API sync has stopped. Runs on its own job:
    if run_sync is the thing that is broken, a check inside it never fires."""
    conn = conn_factory()
    try:
        check_stale_sync(conn, settings, http_post=httpx.post)
    except Exception:
        logger.exception("stale-sync check failed")
    finally:
        conn.close()


def run_digest_job(conn_factory, settings) -> None:
    conn = conn_factory()
    try:
        if send_weekly_digest(conn, settings, http_post=httpx.post):
            logger.info("posted weekly digest")
    except Exception:
        logger.exception("weekly digest failed")
    finally:
        conn.close()


def run_ga4_job(conn_factory, settings) -> None:
    """Refresh listing traffic. Skipped entirely when no key is configured, so
    the dashboard still runs without GA4 rather than erroring every hour."""
    if not settings.ga4_credentials_json:
        logger.info("GA4 credentials not set -- skipping traffic sync")
        return
    from partner_dashboard.ga4 import build_client, sync_ga4

    conn = conn_factory()
    try:
        client = build_client(settings.ga4_credentials_json)
        written = sync_ga4(conn, client, settings.ga4_property_id)
        logger.info("ga4 sync completed: %s rows", written)
    except Exception:
        # A GA4 outage or a revoked key must not take the Partner API sync
        # down with it; the traffic page just goes stale.
        logger.exception("ga4 sync failed")
    finally:
        conn.close()


def start_scheduler(conn_factory, settings) -> BackgroundScheduler:
    """Poll the Partner API on an interval via run_sync. Caller owns shutdown()."""
    client = PartnerClient(settings.partner_api_token, settings.partner_org_id)

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: run_sync_job(conn_factory, client, settings),
        "interval",
        minutes=settings.poll_interval_minutes,
        # First run at boot, not boot+interval: a fresh deploy should sync
        # immediately (the very first ever run replays full app history).
        next_run_time=datetime.now(),
    )
    # Money settles on Shopify's schedule, not ours: a charge is created, then
    # collected some hours later. Hourly is well inside that, and it keeps the
    # tight pagination loop away from the 15-minute lifecycle poll.
    scheduler.add_job(
        lambda: run_transactions_job(conn_factory, client, settings),
        "interval",
        hours=1,
        next_run_time=datetime.now(),
    )
    # GA4 aggregates move slowly and the API has a daily token quota, so hourly
    # is plenty; the first run still happens at boot.
    scheduler.add_job(
        lambda: run_ga4_job(conn_factory, settings),
        "interval",
        hours=1,
        next_run_time=datetime.now(),
    )
    # Every 15 minutes, but it only posts once per stale episode. Deliberately
    # a separate job from run_sync: a check that lives inside the thing it is
    # watching never runs when that thing is the failure.
    scheduler.add_job(
        lambda: run_stale_check_job(conn_factory, settings),
        "interval",
        minutes=15,
    )
    # DIGEST_DAY_OF_WEEK at DIGEST_HOUR in DIGEST_TIMEZONE. A cron trigger, not
    # an interval, so it lands at the same local time year round;
    # send_weekly_digest itself refuses to post twice in one week, which is what
    # makes a machine restart on digest morning harmless.
    scheduler.add_job(
        lambda: run_digest_job(conn_factory, settings),
        "cron",
        day_of_week=settings.digest_day_of_week,
        hour=settings.digest_hour,
        minute=0,
        timezone=settings.digest_timezone,
        id=WEEKLY_DIGEST_JOB_ID,
    )
    scheduler.start()
    return scheduler
