"""Reviewed asset classifications and contract directives."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import tomllib


class AssetClassification(str, Enum):
    NATIVE_CRYPTO = "NATIVE_CRYPTO"
    NON_CRYPTO = "NON_CRYPTO"


@dataclass(frozen=True, slots=True)
class AssetClassificationEntry:
    base_asset: str
    classification: AssetClassification

    def __post_init__(self) -> None:
        if self.base_asset != self.base_asset.upper():
            raise ValueError("base_asset must be uppercase")


@dataclass(frozen=True, slots=True)
class VersionedAssetClassificationDirectory:
    """An immutable, reviewable asset directory loaded from versioned config."""

    version: str
    assets: tuple[AssetClassificationEntry, ...]

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("classification directory version must not be empty")
        if len({entry.base_asset for entry in self.assets}) != len(self.assets):
            raise ValueError("classification directory cannot repeat a base asset")

    @classmethod
    def from_toml(cls, path: str | Path) -> VersionedAssetClassificationDirectory:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        version = data.get("version")
        assets_data = data.get("assets")
        if not isinstance(version, str) or not isinstance(assets_data, list):
            raise ValueError("classification TOML requires a string version and assets array")

        assets = tuple(
            AssetClassificationEntry(
                base_asset=_required_text(row, "base_asset"),
                classification=AssetClassification(_required_text(row, "classification")),
            )
            for row in _require_mapping_rows(assets_data, "assets")
        )
        return cls(version=version, assets=assets)

    def asset_entry(self, base_asset: str) -> AssetClassificationEntry | None:
        return next((entry for entry in self.assets if entry.base_asset == base_asset), None)

def _require_mapping_rows(rows: list[object], name: str) -> tuple[dict[str, object], ...]:
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"classification TOML {name} entries must be tables")
    return tuple(rows)  # type: ignore[return-value]


def _required_text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"classification TOML field {key!r} must be a non-empty string")
    return value
