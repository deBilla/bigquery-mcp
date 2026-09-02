"""Helpers for rendering BigQuery numbers in terms a person can act on."""

from __future__ import annotations

GIB = 1024**3
TIB = 1024**4


def scan_estimate(num_bytes: int, cost_per_tib_usd: float) -> dict:
    """Human-friendly scan size and on-demand cost estimate for ``num_bytes``.

    ``estimated_scan`` picks its own unit. Fixing it to GiB rendered every
    query under ~5 MB as "0.00 GiB", which is what the cost-confirmation
    message quotes back to the user -- a prompt to approve an unknown amount.
    ``estimated_bytes`` stays exact for anything doing arithmetic.
    """
    num_bytes = num_bytes or 0
    return {
        "estimated_bytes": num_bytes,
        "estimated_scan": human_bytes(num_bytes),
        # Six places, not four: a few MiB costs ~1.7e-05, and rounding that
        # to 0.0 makes format_cost say "$0" for a query that is not free.
        "estimated_cost_usd": round(num_bytes / TIB * cost_per_tib_usd, 6),
    }


def format_cost(usd: float) -> str:
    """Render a dollar estimate, never as a misleading "$0.0"."""
    if usd <= 0:
        return "$0"
    if usd < 0.01:
        return "under $0.01"
    return f"${usd:,.2f}"


def human_bytes(num_bytes: int) -> str:
    """Render a byte count at a sensible unit, for use inside error messages."""
    num_bytes = num_bytes or 0
    for unit, size in (("TiB", TIB), ("GiB", GIB), ("MiB", 1024**2), ("KiB", 1024)):
        if num_bytes >= size:
            return f"{num_bytes / size:.2f} {unit}"
    return f"{num_bytes} B"
