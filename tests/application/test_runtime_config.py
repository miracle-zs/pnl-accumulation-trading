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
    def test_runtime_validation_checks_reviewed_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            classifications = root / "asset_classifications.toml"
            classifications.write_text(
                'version = "reviewed-v1"\n'
                '\n[[assets]]\n'
                'base_asset = "BTC"\n'
                'classification = "NATIVE_CRYPTO"\n',
                encoding="utf-8",
            )
            config = RuntimeConfig(
                data_dir=root / "state",
                classification_file=classifications,
            )

            result = validate_runtime_config(config)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["classification_version"], "reviewed-v1")
            self.assertEqual(result["native_asset_count"], 1)


class RuntimeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_builds_application_without_connecting_to_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            classifications = root / "assets.toml"
            classifications.write_text(
                'version = "reviewed-v1"\n'
                '\n[[assets]]\n'
                'base_asset = "BTC"\n'
                'classification = "NATIVE_CRYPTO"\n',
                encoding="utf-8",
            )
            config = RuntimeConfig(data_dir=root / "state", classification_file=classifications)
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
