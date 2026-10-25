from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from pnl_trader.adapters.sqlite import SQLiteStateStore
from pnl_trader.application.market_models import ExchangeInstrument
from pnl_trader.application.position_models import ExchangePosition
from pnl_trader.application.review_batch import ReconciledReviewBatchSource
from pnl_trader.application.review_batch_models import ReviewSignal
from pnl_trader.application.universe import UniverseCatalog
from pnl_trader.application.universe_models import (
    AssetClassification,
    AssetClassificationEntry,
    VersionedAssetClassificationDirectory,
)
from pnl_trader.domain import Action, Lifecycle
from tests.helpers import seed_buy_execution


D = Decimal


class ReconciledReviewBatchDelistingTests(unittest.IsolatedAsyncioTestCase):
    async def test_settled_delisted_position_is_cleared_and_not_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                idle = store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
                seed_buy_execution(
                    store,
                    idle,
                    action=Action.OPEN_T1,
                    quantity=D("1"),
                    price=D("10"),
                    intent_id="open-btc",
                    timestamp_ms=200,
                )
                source = ReconciledReviewBatchSource(
                    store=store,
                    positions=_PositionSource(D("0")),
                    signals=_DelistedSignalSource(),
                    clock_ms=lambda: 300,
                )

                batch = await source.load_review_batch(0)

                state = store.load_symbol_state("BTCUSDT")
                self.assertEqual(state.lifecycle, Lifecycle.IDLE)
                self.assertEqual(state.qty, D("0"))
                self.assertEqual(batch.candidates, ())
            finally:
                store.close()

    async def test_exchange_status_marks_delisting_and_removed_contracts(self) -> None:
        catalog = UniverseCatalog(
            exchange_info=_InstrumentCatalog(
                (
                    ExchangeInstrument(
                        symbol="BTCUSDT",
                        base_asset="BTC",
                        quote_asset="USDT",
                        margin_asset="USDT",
                        contract_type="PERPETUAL",
                        status="DELIVERING",
                        rules=None,
                    ),
                )
            ),
            classifications=_native_btc_directory(),
        )
        signals = await catalog.load_review_signals(managed_symbols=("BTCUSDT", "ETHUSDT"))
        signals_by_symbol = {signal.symbol: signal for signal in signals}

        self.assertTrue(signals_by_symbol["BTCUSDT"].delisted)
        self.assertFalse(signals_by_symbol["BTCUSDT"].admitted)
        self.assertTrue(signals_by_symbol["ETHUSDT"].delisted)

    async def test_delisted_position_is_kept_until_exchange_quantity_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                idle = store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
                opened = seed_buy_execution(
                    store,
                    idle,
                    action=Action.OPEN_T1,
                    quantity=D("1"),
                    price=D("10"),
                    intent_id="open-btc",
                    timestamp_ms=200,
                )
                source = ReconciledReviewBatchSource(
                    store=store,
                    positions=_PositionSource(D("1")),
                    signals=_DelistedSignalSource(),
                    clock_ms=lambda: 300,
                )

                batch = await source.load_review_batch(0)

                self.assertEqual(store.load_symbol_state("BTCUSDT"), opened)
                self.assertEqual(batch.candidates, ())
            finally:
                store.close()


class _PositionSource:
    def __init__(self, quantity: Decimal) -> None:
        self._quantity = quantity

    async def get_position_snapshot(self) -> tuple[ExchangePosition, ...]:
        return (
            ExchangePosition(
                symbol="BTCUSDT",
                position_side="BOTH",
                position_amt=self._quantity,
            ),
        )


class _DelistedSignalSource:
    async def load_review_signals(
        self,
        *,
        managed_symbols: tuple[str, ...],
    ) -> tuple[ReviewSignal, ...]:
        return (ReviewSignal(symbol="BTCUSDT", admitted=False, delisted=True),)


class _InstrumentCatalog:
    def __init__(self, instruments: tuple[ExchangeInstrument, ...]) -> None:
        self._instruments = instruments

    async def get_exchange_info_snapshot(self) -> tuple[ExchangeInstrument, ...]:
        return self._instruments


def _native_btc_directory() -> VersionedAssetClassificationDirectory:
    return VersionedAssetClassificationDirectory(
        version="test",
        assets=(
            AssetClassificationEntry(
                base_asset="BTC",
                classification=AssetClassification.NATIVE_CRYPTO,
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
