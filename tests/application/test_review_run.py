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
    RiskAuthorization,
    SubmissionCertainty,
    SubmissionResult,
)
from pnl_trader.application.market_models import (
    FifteenMinuteCloseSnapshot,
    InstrumentRules,
    MarkPriceSnapshot,
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
    ExecutionReceipt,
    Lifecycle,
    Observation,
    SymbolState,
    Tier,
    TradeFillRecord,
    apply_confirmed_execution,
)


D = Decimal
SLOT_START_MS = 900_000
NOW_MS = SLOT_START_MS + 900_004


class ReviewRunAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_t2_at_50_percent_closes_and_reopens_t1_in_the_same_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                idle = store.ensure_idle_symbol_state(
                    "primary", "BTCUSDT", created_at_ms=100
                )
                t1 = apply_confirmed_execution(
                    idle,
                    ExecutionReceipt(Action.OPEN_T1, D("0.1"), D("10")),
                )
                store.save_symbol_state(
                    "primary", t1, expected_version=idle.version, updated_at_ms=200
                )
                t2 = apply_confirmed_execution(
                    t1,
                    ExecutionReceipt(Action.ADD_T2, D("0.4"), D("20")),
                )
                store.save_symbol_state(
                    "primary", t2, expected_version=t1.version, updated_at_ms=300
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
                    risk_gate=_ApprovedRiskGate(),
                    clock_ms=lambda: NOW_MS,
                )
                review_runs = ReviewRunCoordinator(
                    store=store,
                    execution=execution,
                    symbol_reviews=symbol_reviews,
                    batch_source=_OneSymbolBatchSource(),
                    process_lock=_HeldProcessLock(),
                    clock_ms=lambda: NOW_MS,
                )

                result = await review_runs.run_slot(
                    account_id="primary",
                    strategy_version="strategy-v1",
                    slot_start_ms=SLOT_START_MS,
                )

                final_state = store.load_symbol_state("primary", "BTCUSDT")
                self.assertEqual(result.failure_count, 0)
                self.assertEqual(result.processed_count, 1)
                self.assertEqual(exchange.submitted_sides, ["SELL", "BUY"])
                self.assertTrue(exchange.submitted_reduce_only[0])
                self.assertFalse(exchange.submitted_reduce_only[1])
                self.assertEqual(final_state.lifecycle, Lifecycle.OPEN)
                self.assertEqual(final_state.tier, Tier.T1)
                self.assertEqual(final_state.cycle_id, 2)
                self.assertEqual(final_state.avg_entry_price, D("90"))
                self.assertEqual(len(store.list_active_order_intents("primary")), 0)
            finally:
                store.close()


class _OneSymbolBatchSource:
    async def load_review_batch(
        self,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
    ) -> ReviewBatch:
        return ReviewBatch(
            account_id=account_id,
            strategy_version=strategy_version,
            slot_start_ms=slot_start_ms,
            sampled_at_ms=NOW_MS,
            candidates=(
                ReviewCandidate(
                    symbol="BTCUSDT",
                    signals=Observation(
                        mark_price=D("90"),
                        admitted=True,
                        reopen_authorized=True,
                        scale_in_authorized=True,
                    ),
                    priority=ReviewPriority.REDUCE,
                ),
            ),
        )


class _HeldProcessLock:
    @property
    def is_held(self) -> bool:
        return True


class _ApprovedRiskGate:
    async def authorize_order(self, *, account_id, state, decision, plan, price, rules):
        reserved_margin = D("1") if plan.side == "BUY" else D("0")
        return RiskAuthorization(
            account_id=account_id,
            symbol=state.symbol,
            state_version=state.version,
            action=plan.action,
            approved=True,
            reason="deterministic acceptance-test authorization",
            reserved_margin=reserved_margin,
            max_active_reserved_margin=D("100"),
            valid_until_ms=NOW_MS + 60_000,
        )


class _FilledExchange:
    def __init__(self) -> None:
        self.submitted_sides: list[str] = []
        self.submitted_reduce_only: list[bool] = []
        self._fills: dict[str, tuple[TradeFillRecord, ...]] = {}
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
                step_origin=D("0"),
            ),
            min_market_notional=D("5"),
        )

    async def get_mark_price(self, symbol: str) -> MarkPriceSnapshot:
        return MarkPriceSnapshot(symbol, D("90"), NOW_MS)

    async def get_fifteen_minute_close(
        self,
        symbol: str,
        slot_start_ms: int,
    ) -> FifteenMinuteCloseSnapshot:
        return FifteenMinuteCloseSnapshot(
            symbol=symbol,
            slot_start_ms=slot_start_ms,
            close_price=D("90"),
            close_time_ms=slot_start_ms + 899_999,
        )

    async def get_instrument_rules(self, symbol: str) -> InstrumentRules:
        return self._rules

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        self.submitted_sides.append(request.side)
        self.submitted_reduce_only.append(request.reduce_only)
        index = len(self.submitted_sides)
        order_id = f"sim-order-{index}"
        fill = TradeFillRecord(
            symbol=request.symbol,
            exchange_trade_id=f"sim-trade-{index}",
            client_order_id=request.client_order_id,
            side=request.side,
            price=D("90"),
            qty=request.quantity,
            realized_pnl=D("0"),
            commission=D("0"),
            commission_asset="USDT",
            traded_at_ms=NOW_MS,
            exchange_order_id=order_id,
            position_side="BOTH",
            margin_asset="USDT",
        )
        order = ExchangeOrderSnapshot(
            status=ExchangeOrderStatus.FILLED,
            executed_qty=request.quantity,
            cumulative_quote=request.quantity * D("90"),
            exchange_order_id=order_id,
        )
        self._fills[request.client_order_id] = (fill,)
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
        client_order_id: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]:
        return self._fills[client_order_id]


if __name__ == "__main__":
    unittest.main()
