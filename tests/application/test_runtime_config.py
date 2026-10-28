import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pnl_trader.runtime import (
    RuntimeConfig,
    run_application,
    validate_runtime_config,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_runtime_validation_checks_local_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RuntimeConfig(data_dir=root / "state")

            with patch.dict(os.environ, {"BINANCE_API_KEY": "key", "BINANCE_API_SECRET": "secret"}, clear=True):
                result = validate_runtime_config(config)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["data_dir"], str(root / "state"))
            self.assertNotIn("key", str(result))
            self.assertNotIn("secret", str(result))

    def test_runtime_validation_rejects_missing_credentials(self) -> None:
        config = RuntimeConfig(data_dir=Path("/tmp/state"))
        for environment, missing in [({}, "BINANCE_API_KEY"), ({"BINANCE_API_KEY": "key"}, "BINANCE_API_SECRET")]:
            with self.subTest(missing=missing), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, missing):
                    validate_runtime_config(config)

    def test_runtime_validation_rejects_file_in_data_directory_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "not-a-directory"
            file_path.write_text("unchanged", encoding="utf-8")
            with patch.dict(os.environ, {"BINANCE_API_KEY": "key", "BINANCE_API_SECRET": "secret"}, clear=True):
                for data_dir in (file_path, file_path / "state"):
                    with self.subTest(data_dir=data_dir), self.assertRaisesRegex(ValueError, "non-directory"):
                        validate_runtime_config(RuntimeConfig(data_dir=data_dir))
            self.assertEqual(file_path.read_text(encoding="utf-8"), "unchanged")


class RuntimeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_builds_application_without_connecting_to_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RuntimeConfig(data_dir=root / "state")
            loop = asyncio.get_running_loop()
            with (
                patch("pnl_trader.runtime.require_secret", return_value="test-credential"),
                patch("pnl_trader.runtime.optional_secret", return_value=None),
                patch.object(loop, "add_signal_handler"),
                patch("pnl_trader.runtime.PeriodicReviewScheduler.run_until_stopped", new_callable=AsyncMock) as run_scheduler,
            ):
                await run_application(config)
            run_scheduler.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
