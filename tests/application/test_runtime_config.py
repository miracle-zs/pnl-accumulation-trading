from pathlib import Path
import tempfile
import unittest

from pnl_trader.runtime import (
    RuntimeConfig,
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
            )

            result = validate_runtime_config(config)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["classification_version"], "reviewed-v1")
            self.assertEqual(result["native_asset_count"], 1)
            self.assertNotIn("test-secret", str(result))


    def test_runtime_config_repr_does_not_expose_credentials(self) -> None:
        config = RuntimeConfig(
            account_id="primary",
            strategy_version="v1",
            data_dir=Path("/tmp/trader"),
            classification_file=Path("/tmp/classes.toml"),
            api_key="exchange-key-secret",
            api_secret="exchange-secret-secret",
        )
        representation = repr(config)
        self.assertNotIn("exchange-key-secret", representation)
        self.assertNotIn("exchange-secret-secret", representation)


if __name__ == "__main__":
    unittest.main()
