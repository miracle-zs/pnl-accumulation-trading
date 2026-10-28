import os
import unittest
from unittest.mock import patch

from pnl_trader.credentials import optional_secret, require_secret


class CredentialLoadingTests(unittest.TestCase):
    def test_secrets_are_read_from_the_environment(self) -> None:
        with patch.dict(os.environ, {"BINANCE_API_KEY": " key ", "SERVERCHAN_SENDKEY": " sendkey "}, clear=True):
            self.assertEqual(require_secret("BINANCE_API_KEY"), "key")
            self.assertEqual(optional_secret("SERVERCHAN_SENDKEY"), "sendkey")

    def test_missing_required_and_optional_secrets(self) -> None:
        with patch.dict(os.environ, {"BINANCE_API_KEY": "  ", "SERVERCHAN_SENDKEY": " "}, clear=True):
            with self.assertRaisesRegex(ValueError, "BINANCE_API_KEY"):
                require_secret("BINANCE_API_KEY")
            self.assertIsNone(optional_secret("SERVERCHAN_SENDKEY"))
