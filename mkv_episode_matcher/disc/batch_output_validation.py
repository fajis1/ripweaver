"""Shared, saved-inventory-aware checks for MakeMKV batch output sizes."""

from __future__ import annotations

MINIMUM_SUBSTANTIAL_MKV_BYTES = 1_000_000


def is_inventory_planned_tiny_output(estimated_bytes: int | None) -> bool:
    """Return whether the saved inventory predicted a non-content-sized title."""

    return (
        estimated_bytes is not None
        and 0 < estimated_bytes < MINIMUM_SUBSTANTIAL_MKV_BYTES
    )


def is_complete_batch_output_size(
    *,
    actual_bytes: int,
    estimated_bytes: int | None,
) -> bool:
    """Accept a small nonzero output only when the inventory also predicted one."""

    if actual_bytes < 0:
        return False
    if actual_bytes == 0:
        return True
    if (
        actual_bytes < MINIMUM_SUBSTANTIAL_MKV_BYTES
        and not is_inventory_planned_tiny_output(estimated_bytes)
    ):
        return False
    if estimated_bytes is not None and estimated_bytes > 0:
        return actual_bytes * 2 >= estimated_bytes
    return True
