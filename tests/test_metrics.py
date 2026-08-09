"""The definition registry is the thing that stops a number and its explanation
drifting apart, so it gets checked for the ways it could quietly stop being
true: a blank field, a comparison label for a kind that does not exist, a
direction that is neither up nor down."""

from decimal import Decimal

import pytest

from partner_dashboard.metrics import COMPARE_LABEL, METRICS, signed
from partner_dashboard.stats import COMPARED


@pytest.mark.parametrize("key", sorted(METRICS))
def test_every_metric_is_fully_defined(key):
    m = METRICS[key]
    for field in ("name", "definition", "rule", "source"):
        assert getattr(m, field).strip(), f"{key}.{field} is empty"
    # A rule that just restates the name explains nothing. The whole point of
    # the field is that it is checkable against the query.
    assert m.rule.strip().lower() != m.name.strip().lower()
    assert m.kind in COMPARE_LABEL, f"{key} has no comparison label for {m.kind!r}"
    assert m.unit in ("count", "usd", "pct")
    assert m.better in (None, "up", "down")


def test_every_compared_figure_has_a_definition():
    """stats.COMPARED drives which tiles get a delta. A key there with no entry
    here would render a comparison with no explanation beside it."""
    assert set(COMPARED) <= set(METRICS)


def test_definitions_do_not_reuse_a_display_name():
    names = [m.name for m in METRICS.values()]
    assert len(names) == len(set(names)), "two metrics share a label"


@pytest.mark.parametrize("value,unit,expected", [
    (3, "count", "+3"),
    (-3, "count", "-3"),
    (0, "count", "+0"),
    (1200, "count", "+1,200"),
    (Decimal("210.5"), "usd", "+$210.50"),
    (Decimal("-12"), "usd", "-$12.00"),
    (Decimal("0"), "usd", "+$0.00"),
    (1.5, "pct", "+1.5 pts"),
    (-1.5, "pct", "-1.5 pts"),
])
def test_signed_always_carries_its_sign(value, unit, expected):
    assert signed(value, unit) == expected


def test_zero_is_rendered_rather_than_blanked():
    """"No change" is information. An empty slot where five other tiles have a
    number reads as broken."""
    assert signed(0, "count") == "+0"
    assert signed(Decimal("0.00"), "usd") == "+$0.00"
