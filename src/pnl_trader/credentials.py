"""Read secrets from the process environment."""

from __future__ import annotations

import os
def require_secret(name: str) -> str:
    """Read a required secret from the environment."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required secret {name} is missing from the process environment")
    return value


def optional_secret(name: str) -> str | None:
    """Read a secret when configured, without making it a startup requirement."""
    value = os.environ.get(name, "").strip()
    return value or None
