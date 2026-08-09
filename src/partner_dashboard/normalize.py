import logging
from decimal import Decimal, ROUND_HALF_UP

log = logging.getLogger(__name__)


def normalize_monthly(amount: Decimal, interval: str) -> Decimal:
    if interval == "ANNUAL":
        return (amount / 12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if interval != "EVERY_30_DAYS":
        log.warning("unknown plan interval %r, treating as monthly", interval)
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
