import asyncio
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

            result = validate_runtime_config(config)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["data_dir"], str(root / "state"))


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
