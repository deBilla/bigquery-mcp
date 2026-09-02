"""Rendering of sizes and costs -- the numbers a user is asked to approve."""

from __future__ import annotations

import pytest

from data_platform_mcp.formatting import format_cost, human_bytes, scan_estimate

GIB = 1024**3


@pytest.mark.parametrize(
    "num_bytes,expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (2048, "2.00 KiB"),
        (3 * 1024**2, "3.00 MiB"),
        (213 * GIB, "213.00 GiB"),
        (5 * 1024**4, "5.00 TiB"),
    ],
)
def test_sizes_pick_a_readable_unit(num_bytes, expected):
    assert human_bytes(num_bytes) == expected


def test_a_few_megabytes_does_not_render_as_zero_gib():
    """The defect this replaced.

    estimated_scan was fixed to GiB, so the cost-confirmation prompt read
    "will scan about 0.00 GiB" -- asking the user to approve an unknown amount.
    """
    assert scan_estimate(2_957_162, 6.25)["estimated_scan"] == "2.82 MiB"


def test_estimated_bytes_stays_exact_for_arithmetic():
    assert scan_estimate(2_957_162, 6.25)["estimated_bytes"] == 2_957_162


def test_a_terabyte_scan_is_priced_visibly():
    est = scan_estimate(5 * 1024**4, 6.25)
    assert est["estimated_scan"] == "5.00 TiB"
    assert format_cost(est["estimated_cost_usd"]) == "$31.25"


@pytest.mark.parametrize(
    "usd,expected",
    [
        (0, "$0"),
        (0.000017, "under $0.01"),
        (0.004, "under $0.01"),
        (1.3339, "$1.33"),
        (128.5, "$128.50"),
    ],
)
def test_costs_never_render_a_misleading_zero(usd, expected):
    assert format_cost(usd) == expected


def test_a_nonzero_scan_is_never_priced_at_exactly_zero():
    # Rounding to four places turned ~1.7e-05 into 0.0, which format_cost then
    # rendered as "$0" -- free, for a query that is not.
    cost = scan_estimate(2_957_162, 6.25)["estimated_cost_usd"]
    assert cost > 0
    assert format_cost(cost) == "under $0.01"
