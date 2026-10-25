"""Refresh the contract universe and produce per-slot review authorizations."""

from __future__ import annotations

from .market_models import ExchangeInstrument
from .ports import ExchangeInstrumentCatalogPort
from .review_batch_models import ReviewSignal
from .universe_models import (
    AssetClassification,
    VersionedAssetClassificationDirectory,
)


_DELISTING_STATUSES = frozenset({
    "PRE_DELIVERING",
    "DELIVERING",
    "DELIVERED",
    "PRE_SETTLE",
    "SETTLING",
    "CLOSE",
})


class UniverseCatalog:
    """Classify Binance contracts without inferring asset type from symbol text.

    The reviewed classification file is loaded at startup. Contracts absent
    from it remain pending review until the file is updated and the process is
    restarted; only explicit native-crypto entries become opening candidates.
    """

    def __init__(
        self,
        *,
        exchange_info: ExchangeInstrumentCatalogPort,
        classifications: VersionedAssetClassificationDirectory,
    ) -> None:
        self._exchange_info = exchange_info
        self._classifications = classifications

    async def load_review_signals(
        self,
        *,
        managed_symbols: tuple[str, ...],
    ) -> tuple[ReviewSignal, ...]:
        instruments = await self._exchange_info.get_exchange_info_snapshot()
        instruments_by_symbol = {instrument.symbol: instrument for instrument in instruments}
        eligible_symbols = {
            instrument.symbol
            for instrument in instruments
            if self._is_eligible(instrument)
        }
        symbols_to_review = set(managed_symbols)
        symbols_to_review.update(eligible_symbols)

        return tuple(
            ReviewSignal(
                symbol=symbol,
                admitted=symbol in eligible_symbols,
                delisted=(
                    symbol not in instruments_by_symbol
                    or instruments_by_symbol[symbol].status in _DELISTING_STATUSES
                ),
            )
            for symbol in symbols_to_review
        )

    def _is_eligible(self, instrument: ExchangeInstrument) -> bool:
        classification_entry = self._classifications.asset_entry(instrument.base_asset)

        if instrument.quote_asset != "USDT" or instrument.margin_asset != "USDT":
            return False
        if instrument.contract_type != "PERPETUAL":
            return False
        if instrument.status != "TRADING":
            return False
        if (
            classification_entry is None
            or classification_entry.classification is not AssetClassification.NATIVE_CRYPTO
        ):
            return False
        return True
