from decimal import Decimal
from partner_dashboard.normalize import normalize_monthly


def test_monthly_passthrough():
    assert normalize_monthly(Decimal("29.00"), "EVERY_30_DAYS") == Decimal("29.00")


def test_annual_divided_by_twelve():
    assert normalize_monthly(Decimal("120.00"), "ANNUAL") == Decimal("10.00")


def test_annual_rounds_two_dp():
    assert normalize_monthly(Decimal("100.00"), "ANNUAL") == Decimal("8.33")
