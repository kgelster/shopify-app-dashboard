import csv
import logging
import sys

import psycopg

logger = logging.getLogger(__name__)

# The header of the CSV you are importing. These four keys are what the
# importer fills; the values are the column titles to read them from. Retitle
# the values to match your export. Analytics vendors ship 50+ column exports
# and only the identity fields are worth carrying over.
#
# Contact and email columns are deliberately NOT mapped. Exports like these
# list every staff account on the shop, in no useful order: agencies,
# freelancers, and the app's own team. Contact 1 is not the merchant, no other
# index is either, and a dashboard column headed "who to write to" that names
# somebody else's agency is worse than a blank. See migration 008.
COLUMN_MAP = {
    "shop_domain": "Shopify Domain",
    "shop_name": "Name",
    "country": "Country Code",
    "industry": "Industry",
}


def import_shops_csv(conn: psycopg.Connection, path: str) -> int:
    """Update shop identity fields from a CSV export, matched on shop_domain.

    Backfills the fields the Partner API does not expose (country, industry,
    display name) from an export you already have, typically from whatever
    analytics vendor you are migrating off.

    Update-only: shops rows are created by derivation with a shop GID primary
    key (third-party exports don't carry Partner shop GIDs), so rows are matched
    by myshopify domain and unmatched export rows are logged and skipped. Only
    fills identity columns; empty export cells never blank an existing value
    (nullif + coalesce). Never touches install_state/installed_at/
    uninstalled_at: those are derivation output and must survive a re-import.
    """
    imported = 0
    unmatched = 0
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            values = {k: (row.get(src) or "").strip() for k, src in COLUMN_MAP.items()}
            if not values["shop_domain"]:
                continue
            cur = conn.execute(
                """
                update shops set
                    shop_name = coalesce(nullif(%(shop_name)s, ''), shop_name),
                    country = coalesce(nullif(%(country)s, ''), country),
                    industry = coalesce(nullif(%(industry)s, ''), industry)
                where shop_domain = %(shop_domain)s
                """,
                values,
            )
            if cur.rowcount:
                imported += 1
            else:
                unmatched += 1
                logger.warning("no shop row matching domain %r; skipped", values["shop_domain"])
    conn.commit()
    if unmatched:
        logger.warning("%d export rows had no matching shop row", unmatched)
    return imported


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "shops":
        print("usage: python -m partner_dashboard.import_shops_csv shops <path>", file=sys.stderr)
        sys.exit(1)
    from partner_dashboard.db import connect

    logging.basicConfig(level=logging.INFO)
    n = import_shops_csv(connect(), sys.argv[2])
    print(f"imported {n} shop rows")
