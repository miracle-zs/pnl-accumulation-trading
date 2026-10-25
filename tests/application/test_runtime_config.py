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


if __name__ == "__main__":
    unittest.main()
