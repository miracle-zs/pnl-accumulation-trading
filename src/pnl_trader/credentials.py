"""Secret loading from process variables and systemd credential files."""

from __future__ import annotations

import os
from pathlib import Path


_SYSTEMD_CREDENTIALS = {
    "BINANCE_API_KEY": "binance_api_key",
    "BINANCE_API_SECRET": "binance_api_secret",
}


def read_secret(name: str) -> str:
    """Read a supported systemd credential first, then the process environment."""

    credential_name = _SYSTEMD_CREDENTIALS.get(name)
    credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if credential_name and credentials_directory:
        credential_path = Path(credentials_directory) / credential_name
        try:
            credential = credential_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            credential = ""
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"could not read systemd credential for {name}") from exc
        if credential:
            return credential
    return os.environ.get(name, "").strip()


def require_secret(name: str) -> str:
    value = read_secret(name)
    if not value:
        raise ValueError(
            f"required secret {name} is missing from the process environment "
            "and systemd credentials"
        )
    return value
