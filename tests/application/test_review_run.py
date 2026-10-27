"""Review-cycle acceptance tests with a deterministic fake exchange."""

from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from pnl_trader.adapters.sqlite import SQLiteStateStore
from pnl_trader.application.execution import ExecutionCoordinator
from pnl_trader.application.execution_models import (
    ExchangeOrderSnapshot,
    ExchangeOrderStatus,
    MarketOrderRequest,
    SubmissionCertainty,
    SubmissionResult,
)
from pnl_trader.application.market_models import (
    FifteenMinuteCloseSnapshot,
    InstrumentRules,
    MarketQuantityRules,
)
from pnl_trader.application.review_executor import SymbolReviewExecutor
from pnl_trader.application.review_run import ReviewRunCoordinator
from pnl_trader.application.review_run_models import (
    ReviewBatch,
    ReviewCandidate,
    ReviewPriority,
)
from pnl_trader.domain import (
    Action,
    Lifecycle,
    Tier,
    TradeFillRecord,
)
from tests.helpers import seed_buy_execution


D = Decimal
SLOT_START_MS = 900_000
NOW_MS = SLOT_START_MS + 900_004


class ReviewRunAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_t2_at_50_percent_closes_and_reopens_t1_in_the_same_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                idle = store.ensure_idle_symbol_state(
                    "BTCUSDT", created_at_ms=100
                )
                t1 = seed_buy_execution(
                    store,
                    idle,
                    action=Action.OPEN_T1,
                    quantity=D("0.1"),
                    price=D("10"),
                    intent_id="seed-review-t1",
                    timestamp_ms=200,
                )
                t2 = seed_buy_execution(
                    store,
                    t1,
                    action=Action.ADD_T2,
                    quantity=D("0.4"),
                    price=D("50"),
                    intent_id="seed-review-t2",
                    timestamp_ms=300,
                )

                exchange = _FilledExchange()
                execution = ExecutionCoordinator(
                    store=store,
                    exchange=exchange,
                    clock_ms=lambda: NOW_MS,
                )
                symbol_reviews = SymbolReviewExecutor(
                    store=store,
                    exchange=exchange,
                    execution=execution,
                )
                review_runs = ReviewRunCoordinator(
                    store=store,
                    execution=execution,
                    symbol_reviews=symbol_reviews,
                    batch_source=_OneSymbolBatchSource(),
                )

                result = await review_runs.run_slot(
                    slot_start_ms=SLOT_START_MS,
                )

                final_state = store.load_symbol_state("BTCUSDT")
                counts = dict(result.result_counts)
                self.assertEqual(result.candidate_count, 1)
                self.assertEqual(counts.get("ERROR", 0), 0)
                self.assertEqual(exchange.submitted_sides, ["SELL", "BUY"])
                self.assertEqual(final_state.lifecycle, Lifecycle.OPEN)
                self.assertEqual(final_state.tier, Tier.T1)
                self.assertEqual(final_state.avg_entry_price, D("90"))
                self.assertEqual(len(store.list_active_order_intents()), 0)
            finally:
                store.close()


class _OneSymbolBatchSource:
    async def load_review_batch(
        self,
        slot_start_ms: int,
    ) -> ReviewBatch:
        return ReviewBatch(
            slot_start_ms=slot_start_ms,
            candidates=(
                ReviewCandidate(
                    symbol="BTCUSDT",
                    admitted=True,
                    priority=ReviewPriority.POSITION,
                ),
            ),
        )


class _FilledExchange:
    def __init__(self) -> None:
        self.submitted_sides: list[str] = []
        self._fills_by_exchange_order_id: dict[str, tuple[TradeFillRecord, ...]] = {}
        self._order_snapshots: dict[str, ExchangeOrderSnapshot] = {}
        self._rules = InstrumentRules(
            symbol="BTCUSDT",
            contract_type="PERPETUAL",
            status="TRADING",
            quote_asset="USDT",
            margin_asset="USDT",
            market_quantity=MarketQuantityRules(
                min_qty=D("0.001"),
                max_qty=D("1000"),
                step_size=D("0.001"),
            ),
            min_market_notional=D("5"),
        )

    async def set_leverage(self, symbol: str, leverage: int) -> int:
        return leverage

    async def get_fifteen_minute_close(
        self,
        symbol: str,
        slot_start_ms: int,
    ) -> FifteenMinuteCloseSnapshot:
        return FifteenMinuteCloseSnapshot(
            symbol=symbol,
            close_price=D("90"),
        )

    async def get_instrument_rules(self, symbol: str) -> InstrumentRules:
        return self._rules

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        self.submitted_sides.append(request.side)
        index = len(self.submitted_sides)
        order_id = f"sim-order-{index}"
        fill = TradeFillRecord(
            symbol=request.symbol,
            exchange_trade_id=f"sim-trade-{index}",
            side=request.side,
            price=D("90"),
            qty=request.quantity,
            realized_pnl=D("0"),
            commission=D("0"),
            commission_asset="USDT",
            traded_at_ms=NOW_MS,
        )
        order = ExchangeOrderSnapshot(
            status=ExchangeOrderStatus.FILLED,
            executed_qty=request.quantity,
            cumulative_quote=request.quantity * D("90"),
            exchange_order_id=order_id,
        )
        self._fills_by_exchange_order_id[order_id] = (fill,)
        self._order_snapshots[request.client_order_id] = order
        return SubmissionResult(SubmissionCertainty.ACCEPTED, order=order)

    async def lookup_order(
        self,
        symbol: str,
        client_order_id: str,
    ) -> ExchangeOrderSnapshot | None:
        return self._order_snapshots.get(client_order_id)

    async def list_order_fills(
        self,
        symbol: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]:
        return self._fills_by_exchange_order_id[order_snapshot.exchange_order_id]


if __name__ == "__main__":
    unittest.main()
