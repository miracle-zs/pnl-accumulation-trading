from pathlib import Path
from decimal import Decimal
from dataclasses import replace
import tempfile
import unittest

from pnl_trader.runtime import (
    RuntimeConfig,
    validate_runtime_config,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_runtime_validation_checks_reviewed_assets_and_risk_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            classifications = root / "asset_classifications.toml"
            classifications.write_text(
                'version = "reviewed-v1"\n'
                '\n[[assets]]\n'
                'base_asset = "BTC"\n'
                'classification = "NATIVE_CRYPTO"\n'
                'source = "reviewed source"\n'
                'reviewed_at_ms = 1\n',
                encoding="utf-8",
            )
            config = RuntimeConfig(
                account_id="test-account",
                strategy_version="v1",
                data_dir=root / "state",
                classification_file=classifications,
                api_key="test-key",
                api_secret="test-secret",
                reserve_buffer=Decimal("100"),
                slippage_buffer_rate=Decimal("0.005"),
                max_maintenance_margin_ratio=Decimal("0.50"),
            )

            result = validate_runtime_config(config)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["classification_version"], "reviewed-v1")
            self.assertEqual(result["native_asset_count"], 1)
            self.assertNotIn("test-secret", str(result))

            with self.assertRaisesRegex(ValueError, "reserve_buffer"):
                validate_runtime_config(replace(config, reserve_buffer=Decimal("-1")))

    def test_runtime_config_repr_does_not_expose_credentials(self) -> None:
        config = RuntimeConfig(
            account_id="primary",
            strategy_version="v1",
            data_dir=Path("/tmp/trader"),
            classification_file=Path("/tmp/classes.toml"),
            api_key="exchange-key-secret",
            api_secret="exchange-secret-secret",
            reserve_buffer=Decimal("0"),
            slippage_buffer_rate=Decimal("0"),
            max_maintenance_margin_ratio=Decimal("1"),
        )
        representation = repr(config)
        self.assertNotIn("exchange-key-secret", representation)
        self.assertNotIn("exchange-secret-secret", representation)


if __name__ == "__main__":
    unittest.main()
