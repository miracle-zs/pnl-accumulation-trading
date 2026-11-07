"""Shared system clock values."""

from __future__ import annotations

import time


def current_epoch_ms() -> int:
    """Return the current Unix timestamp in milliseconds."""
    return time.time_ns() // 1_000_000
