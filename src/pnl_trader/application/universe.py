"""Refresh the contract universe and produce per-slot review authorizations."""

from __future__ import annotations

from hashlib import sha256
import time
from typing import Callable

from ..domain.models import Observation
from .market_models import ExchangeInfoSnapshot, ExchangeInstrument
from .ports import ExchangeInstrumentCatalogPort
from .review_batch_models import ReviewSignal, ReviewSignalSnapshot
from .universe_models import (
    AssetClassification,
    ContractDirectiveKind,
    UniverseEligibility,
    UniverseInstrument,
    UniverseSnapshot,
    VersionedAssetClassificationDirectory,
)


class UniverseCatalogError(RuntimeError):
    """The current listing or reviewed classification cannot be used safely."""


class UniverseCatalog:
    """Classify Binance contracts without inferring asset type from symbol text.

    The reviewed classification file is loaded at startup. Contracts absent
    from it remain pending review until the file is updated and the process is
    restarted; only explicit native-crypto entries become opening candidates.
    Contract-level EXIT_ONLY directives keep a managed symbol in reviews while
    disabling increases and reopens.
    """

    def __init__(
        self,
        *,
        exchange_info: ExchangeInstrumentCatalogPort,
        classifications: VersionedAssetClassificationDirectory,
        future_clock_skew_ms: int = 1_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if future_clock_skew_ms < 0:
            raise ValueError("future_clock_skew_ms must be non-negative")
        self._exchange_info = exchange_info
        self._classifications = classifications
        self._future_clock_skew_ms = future_clock_skew_ms
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def refresh(self) -> UniverseSnapshot:
        """Classify the current exchange listing; the gateway owns the API cache."""

        exchange_snapshot = await self._exchange_info.get_exchange_info_snapshot()
        if exchange_snapshot.sampled_at_ms - self._clock_ms() > self._future_clock_skew_ms:
            raise UniverseCatalogError("exchangeInfo snapshot timestamp is in the future")
        return self._build_snapshot(exchange_snapshot)

    async def load_review_signals(
        self,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
        *,
        managed_symbols: tuple[str, ...],
    ) -> ReviewSignalSnapshot:
        if not account_id or not strategy_version:
            raise ValueError("account_id and strategy_version must not be empty")
        if slot_start_ms < 0 or slot_start_ms % 900_000 != 0:
            raise ValueError("slot_start_ms must be a UTC 15-minute boundary")
        if not isinstance(managed_symbols, tuple) or any(not symbol for symbol in managed_symbols):
            raise ValueError("managed_symbols must be a tuple of non-empty symbols")
        if len(set(managed_symbols)) != len(managed_symbols):
            raise ValueError("managed_symbols cannot contain duplicates")

        universe = await self.refresh()
        now_ms = self._clock_ms()

        instruments_by_symbol = {item.symbol: item for item in universe.instruments}
        symbols_to_review = set(managed_symbols)
        symbols_to_review.update(universe.eligible_symbols)
        symbols = sorted(symbols_to_review)
        if not symbols:
            raise UniverseCatalogError("no eligible contracts or managed symbols are available")

        signals: list[ReviewSignal] = []
        for symbol in symbols:
            entry = instruments_by_symbol.get(symbol)
            admitted = (
                entry is not None
                and entry.eligibility is UniverseEligibility.ELIGIBLE
            )
            signals.append(
                ReviewSignal(
                    symbol=symbol,
                    observation=Observation(
                        admitted=admitted,
                        reopen_authorized=admitted,
                        scale_in_authorized=admitted,
                        valid=True,
                    ),
                )
            )

        signal_identity = [
            account_id,
            strategy_version,
            str(slot_start_ms),
            universe.snapshot_id,
        ]
        signal_identity.extend(symbols)
        snapshot_id = sha256("\0".join(signal_identity).encode("utf-8")).hexdigest()
        return ReviewSignalSnapshot(
            account_id=account_id,
            strategy_version=strategy_version,
            slot_start_ms=slot_start_ms,
            snapshot_id=snapshot_id,
            sampled_at_ms=now_ms,
            signals=tuple(signals),
        )

    def _build_snapshot(self, exchange_snapshot: ExchangeInfoSnapshot) -> UniverseSnapshot:
        entries = tuple(
            self._classify_instrument(instrument)
            for instrument in exchange_snapshot.instruments
        )
        directory_identity: list[str] = [self._classifications.version]
        directory_identity.extend(
            f"{item.base_asset}:{item.classification.value}:{item.source}:"
            f"{item.reviewed_at_ms}:{item.note}"
            for item in sorted(self._classifications.assets, key=lambda item: item.base_asset)
        )
        directory_identity.extend(
            f"{item.symbol}:{item.kind.value}:{item.source}:{item.recorded_at_ms}:{item.note}"
            for item in sorted(
                self._classifications.contract_directives,
                key=lambda item: item.symbol,
            )
        )
        directory_digest = sha256("\0".join(directory_identity).encode("utf-8")).hexdigest()
        snapshot_id = sha256(
            "\0".join(
                (
                    exchange_snapshot.snapshot_id,
                    str(exchange_snapshot.sampled_at_ms),
                    directory_digest,
                )
            ).encode("utf-8")
        ).hexdigest()
        return UniverseSnapshot(
            snapshot_id=snapshot_id,
            directory_version=self._classifications.version,
            sampled_at_ms=exchange_snapshot.sampled_at_ms,
            exchange_snapshot_id=exchange_snapshot.snapshot_id,
            instruments=entries,
        )

    def _classify_instrument(self, instrument: ExchangeInstrument) -> UniverseInstrument:
        classification_entry = self._classifications.asset_entry(instrument.base_asset)
        classification = (
            classification_entry.classification
            if classification_entry is not None
            else AssetClassification.UNKNOWN
        )
        classification_source = (
            classification_entry.source if classification_entry is not None else "unreviewed"
        )
        reviewed_at_ms = (
            classification_entry.reviewed_at_ms if classification_entry is not None else None
        )
        directive = self._classifications.contract_directive(instrument.symbol)

        if instrument.quote_asset != "USDT" or instrument.margin_asset != "USDT":
            eligibility = UniverseEligibility.INELIGIBLE
            reason = "contract must be quoted and margined in USDT"
        elif instrument.contract_type != "PERPETUAL":
            eligibility = UniverseEligibility.INELIGIBLE
            reason = "contract is not a perpetual"
        elif instrument.status != "TRADING":
            eligibility = UniverseEligibility.INELIGIBLE
            reason = f"exchange status is {instrument.status}"
        elif directive is not None and directive.kind is ContractDirectiveKind.EXCLUDE:
            eligibility = UniverseEligibility.INELIGIBLE
            reason = "contract is explicitly excluded by the reviewed directory"
        elif directive is not None and directive.kind is ContractDirectiveKind.EXIT_ONLY:
            eligibility = UniverseEligibility.EXIT_ONLY
            reason = "contract has an active exit-only directive"
        elif classification is AssetClassification.UNKNOWN:
            eligibility = UniverseEligibility.PENDING_REVIEW
            reason = "base asset has no reviewed native/non-crypto classification"
        elif classification is AssetClassification.NON_CRYPTO:
            eligibility = UniverseEligibility.INELIGIBLE
            reason = "base asset is classified as non-crypto"
        elif instrument.rules is None:
            eligibility = UniverseEligibility.BLOCKED
            reason = "market-order quantity rules are missing or invalid"
        else:
            eligibility = UniverseEligibility.ELIGIBLE
            reason = "native asset, supported USDT perpetual and valid market rules"

        return UniverseInstrument(
            instrument=instrument,
            classification=classification,
            eligibility=eligibility,
            eligibility_reason=reason,
            classification_source=classification_source,
            classification_reviewed_at_ms=reviewed_at_ms,
            directive=None if directive is None else directive.kind,
            directive_source=None if directive is None else directive.source,
        )
