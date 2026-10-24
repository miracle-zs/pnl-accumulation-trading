from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from pnl_trader.credentials import read_secret, require_secret


class CredentialLoadingTests(unittest.TestCase):
    def test_systemd_credential_takes_precedence_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "binance_api_key").write_text(
                "systemd-key\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "CREDENTIALS_DIRECTORY": directory,
                    "BINANCE_API_KEY": "environment-key",
                },
                clear=True,
            ):
                self.assertEqual(read_secret("BINANCE_API_KEY"), "systemd-key")

    def test_process_environment_remains_supported(self) -> None:
        with patch.dict(
            os.environ,
            {"BINANCE_API_SECRET": "environment-secret"},
            clear=True,
        ):
            self.assertEqual(
                require_secret("BINANCE_API_SECRET"),
                "environment-secret",
            )

    def test_required_secret_fails_when_both_sources_are_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "BINANCE_API_KEY"):
                require_secret("BINANCE_API_KEY")


if __name__ == "__main__":
    unittest.main()
