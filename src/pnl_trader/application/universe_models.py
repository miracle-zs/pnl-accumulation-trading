"""Versioned asset classification and contract-universe values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import tomllib

from .market_models import ExchangeInstrument, InstrumentRules


class AssetClassification(str, Enum):
    NATIVE_CRYPTO = "NATIVE_CRYPTO"
    NON_CRYPTO = "NON_CRYPTO"
    UNKNOWN = "UNKNOWN"


class ContractDirectiveKind(str, Enum):
    EXIT_ONLY = "EXIT_ONLY"
    EXCLUDE = "EXCLUDE"


class UniverseEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    EXIT_ONLY = "EXIT_ONLY"
    PENDING_REVIEW = "PENDING_REVIEW"
    BLOCKED = "BLOCKED"
    INELIGIBLE = "INELIGIBLE"


@dataclass(frozen=True, slots=True)
class AssetClassificationEntry:
    base_asset: str
    classification: AssetClassification
    source: str
    reviewed_at_ms: int
    note: str = ""

    def __post_init__(self) -> None:
        if not self.base_asset or self.base_asset != self.base_asset.upper():
            raise ValueError("base_asset must be a non-empty uppercase asset code")
        if not isinstance(self.classification, AssetClassification):
            raise TypeError("classification must be AssetClassification")
        if not self.source:
            raise ValueError("classification source must not be empty")
        if self.reviewed_at_ms < 0:
            raise ValueError("reviewed_at_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class ContractDirective:
    symbol: str
    kind: ContractDirectiveKind
    source: str
    recorded_at_ms: int
    note: str = ""

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be a non-empty uppercase contract code")
        if not isinstance(self.kind, ContractDirectiveKind):
            raise TypeError("kind must be ContractDirectiveKind")
        if not self.source:
            raise ValueError("directive source must not be empty")
        if self.recorded_at_ms < 0:
            raise ValueError("recorded_at_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class VersionedAssetClassificationDirectory:
    """An immutable, reviewable asset directory loaded from versioned config."""

    version: str
    assets: tuple[AssetClassificationEntry, ...]
    contract_directives: tuple[ContractDirective, ...] = ()

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("classification directory version must not be empty")
        if not isinstance(self.assets, tuple) or any(
            not isinstance(entry, AssetClassificationEntry) for entry in self.assets
        ):
            raise TypeError("assets must be a tuple of AssetClassificationEntry")
        if not isinstance(self.contract_directives, tuple) or any(
            not isinstance(item, ContractDirective) for item in self.contract_directives
        ):
            raise TypeError("contract_directives must be a tuple of ContractDirective")
        if len({entry.base_asset for entry in self.assets}) != len(self.assets):
            raise ValueError("classification directory cannot repeat a base asset")
        if len({item.symbol for item in self.contract_directives}) != len(
            self.contract_directives
        ):
            raise ValueError("classification directory cannot repeat a contract directive")

    @classmethod
    def from_toml(cls, path: str | Path) -> VersionedAssetClassificationDirectory:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        version = data.get("version")
        assets_data = data.get("assets", [])
        directives_data = data.get("contracts", [])
        if not isinstance(version, str) or not isinstance(assets_data, list):
            raise ValueError("classification TOML requires a string version and assets array")
        if not isinstance(directives_data, list):
            raise ValueError("classification TOML contracts must be an array")

        assets = tuple(
            AssetClassificationEntry(
                base_asset=_required_text(row, "base_asset"),
                classification=AssetClassification(_required_text(row, "classification")),
                source=_required_text(row, "source"),
                reviewed_at_ms=_required_int(row, "reviewed_at_ms"),
                note=_optional_text(row, "note"),
            )
            for row in _require_mapping_rows(assets_data, "assets")
        )
        directives = tuple(
            ContractDirective(
                symbol=_required_text(row, "symbol"),
                kind=ContractDirectiveKind(_required_text(row, "kind")),
                source=_required_text(row, "source"),
                recorded_at_ms=_required_int(row, "recorded_at_ms"),
                note=_optional_text(row, "note"),
            )
            for row in _require_mapping_rows(directives_data, "contracts")
        )
        return cls(version=version, assets=assets, contract_directives=directives)

    def asset_entry(self, base_asset: str) -> AssetClassificationEntry | None:
        return next((entry for entry in self.assets if entry.base_asset == base_asset), None)

    def contract_directive(self, symbol: str) -> ContractDirective | None:
        return next((item for item in self.contract_directives if item.symbol == symbol), None)


@dataclass(frozen=True, slots=True)
class UniverseInstrument:
    instrument: ExchangeInstrument
    classification: AssetClassification
    eligibility: UniverseEligibility
    eligibility_reason: str
    classification_source: str
    classification_reviewed_at_ms: int | None
    directive: ContractDirectiveKind | None = None
    directive_source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, ExchangeInstrument):
            raise TypeError("instrument must be ExchangeInstrument")
        if not isinstance(self.classification, AssetClassification):
            raise TypeError("classification must be AssetClassification")
        if not isinstance(self.eligibility, UniverseEligibility):
            raise TypeError("eligibility must be UniverseEligibility")
        if not self.eligibility_reason or not self.classification_source:
            raise ValueError("eligibility reason and classification source must not be empty")
        if self.classification_reviewed_at_ms is not None and self.classification_reviewed_at_ms < 0:
            raise ValueError("classification_reviewed_at_ms must be non-negative")
        if self.directive is None and self.directive_source is not None:
            raise ValueError("directive_source requires a directive")
        if self.directive is not None and not self.directive_source:
            raise ValueError("a directive requires its source")

    @property
    def symbol(self) -> str:
        return self.instrument.symbol

    @property
    def market_rules(self) -> InstrumentRules | None:
        return self.instrument.rules


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    snapshot_id: str
    directory_version: str
    sampled_at_ms: int
    exchange_snapshot_id: str
    instruments: tuple[UniverseInstrument, ...]

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.directory_version or not self.exchange_snapshot_id:
            raise ValueError("universe snapshot identities must not be empty")
        if self.sampled_at_ms < 0:
            raise ValueError("sampled_at_ms must be non-negative")
        if not isinstance(self.instruments, tuple) or any(
            not isinstance(item, UniverseInstrument) for item in self.instruments
        ):
            raise TypeError("instruments must be a tuple of UniverseInstrument")
        if not self.instruments:
            raise ValueError("universe snapshot must contain at least one instrument")
        if len({item.symbol for item in self.instruments}) != len(self.instruments):
            raise ValueError("universe snapshot cannot contain duplicate symbols")

    @property
    def eligible_symbols(self) -> tuple[str, ...]:
        return tuple(
            item.symbol
            for item in self.instruments
            if item.eligibility is UniverseEligibility.ELIGIBLE
        )


def _require_mapping_rows(rows: list[object], name: str) -> tuple[dict[str, object], ...]:
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"classification TOML {name} entries must be tables")
    return tuple(rows)  # type: ignore[return-value]


def _required_text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"classification TOML field {key!r} must be a non-empty string")
    return value


def _required_int(row: dict[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"classification TOML field {key!r} must be an integer")
    return value


def _optional_text(row: dict[str, object], key: str) -> str:
    value = row.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"classification TOML field {key!r} must be a string")
    return value
