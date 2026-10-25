"""Secret loading from process variables and systemd credential files."""

from __future__ import annotations

import os
from pathlib import Path


_SYSTEMD_CREDENTIALS = {
    "BINANCE_API_KEY": "binance_api_key",
    "BINANCE_API_SECRET": "binance_api_secret",
    "SERVERCHAN_SENDKEY": "serverchan_sendkey",
}


def require_secret(name: str) -> str:
    """Read a required secret from systemd credentials or the environment."""
    credential_name = _SYSTEMD_CREDENTIALS.get(name)
    credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if credential_name and credentials_directory:
        credential_path = Path(credentials_directory) / credential_name
        try:
            credential = credential_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise ValueError(f"systemd credential for {name} is missing") from None
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"could not read systemd credential for {name}") from exc
        if not credential:
            raise ValueError(f"systemd credential for {name} is empty")
        return credential

    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required secret {name} is missing from the process environment")
    return value


def optional_secret(name: str) -> str | None:
    """Read a secret when configured, without making it a startup requirement."""
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

    value = os.environ.get(name, "").strip()
    return value or None
