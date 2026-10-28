"""Refresh the contract universe and produce per-slot review authorizations."""

from __future__ import annotations

from .market_models import ExchangeInstrument
from .ports import ExchangeInstrumentCatalogPort
from .review_batch_models import ReviewSignal


_DELISTING_STATUSES = frozenset({
    "PRE_DELIVERING",
    "DELIVERING",
    "DELIVERED",
    "PRE_SETTLE",
    "SETTLING",
    "CLOSE",
})


class UniverseCatalog:
    """Select active COIN, INDEX, and RWA perpetuals from Binance metadata."""

    def __init__(
        self,
        *,
        exchange_info: ExchangeInstrumentCatalogPort,
    ) -> None:
        self._exchange_info = exchange_info

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
        if instrument.quote_asset != "USDT" or instrument.margin_asset != "USDT":
            return False
        if instrument.contract_type != "PERPETUAL":
            return False
        if instrument.status != "TRADING":
            return False
        return (
            instrument.underlying_type in {"COIN", "INDEX"}
            or "RWA" in instrument.underlying_sub_types
        )
