"""Review-cycle acceptance tests with a deterministic fake exchange."""

from decimal import Decimal
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from pnl_trader.adapters.sqlite import SQLiteStateStore
from pnl_trader.application.account_models import AccountSnapshot
from pnl_trader.application.execution import ExecutionCoordinator
from pnl_trader.application.execution_models import (
    ExchangeOrderSnapshot,
    ExchangeOrderStatus,
    MarketOrderRequest,
    SubmissionCertainty,
    SubmissionResult,
)
from pnl_trader.application.market_models import (
    InstrumentRules,
    MarketQuantityRules,
)
from pnl_trader.application.position_models import ExchangePosition
from pnl_trader.application.review_batch import ReconciledReviewBatchSource
from pnl_trader.application.review_batch_models import ReviewSignal
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
from pnl_trader.web.service import DashboardQueryService
from tests.helpers import seed_buy_execution


D = Decimal
SLOT_START_MS = 900_000
NOW_MS = SLOT_START_MS + 900_004


class ReviewRunAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_below_minimum_order_logs_concisely_and_is_checked_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteStateStore(Path(directory) / "state.sqlite3") as store:
                store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
                exchange = _FilledExchange()
                exchange._rules = replace(exchange._rules, min_market_notional=D("20"))
                execution = ExecutionCoordinator(store=store, exchange=exchange)
                reviews = ReviewRunCoordinator(
                    store=store,
                    execution=execution,
                    symbol_reviews=SymbolReviewExecutor(store=store, exchange=exchange, execution=execution),
                    batch_source=_OneSymbolBatchSource(),
                    market_prices=exchange,
                )
                for slot in (SLOT_START_MS, SLOT_START_MS + 900_000):
                    with self.assertLogs("pnl_trader.application.review_run", level="WARNING") as logs:
                        result = await reviews.run_slot(slot_start_ms=slot)
                    self.assertEqual(dict(result.result_counts)["ERROR"], 1)
                    self.assertIn("below 20", logs.output[0])
                    self.assertNotIn("Traceback", logs.output[0])
                self.assertEqual(exchange.submitted_sides, [])

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
                    market_prices=exchange,
                    account_equity=exchange,
                )

                result = await review_runs.run_slot(
                    slot_start_ms=SLOT_START_MS,
                )

                final_state = store.load_symbol_state("BTCUSDT")
                counts = dict(result.result_counts)
                self.assertEqual(result.candidate_count, 1)
                self.assertEqual(counts.get("ERROR", 0), 0)
                self.assertEqual(exchange.submitted_sides, ["SELL", "BUY"])
                self.assertLess(exchange.events.index("submit BUY"), exchange.events.index("fills SELL"))
                self.assertEqual(final_state.lifecycle, Lifecycle.OPEN)
                self.assertEqual(final_state.tier, Tier.T1)
                self.assertEqual(final_state.avg_entry_price, D("90"))
                self.assertEqual(len(store.list_active_order_intents()), 0)
                latest = DashboardQueryService(Path(directory) / "state.sqlite3").get_equity_history("24h")["latest"]
                self.assertEqual(latest["equity"], 100.0)
                self.assertEqual(latest["position_notional"], 200.0)
                self.assertEqual(latest["leverage"], 2.0)
            finally:
                store.close()

    async def test_t3_writeoff_below_minimum_notional_closes_and_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                idle = store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
                t1 = seed_buy_execution(
                    store, idle, action=Action.OPEN_T1, quantity=D("1"),
                    price=D("10"), intent_id="seed-writeoff-t1", timestamp_ms=200,
                )
                t2 = seed_buy_execution(
                    store, t1, action=Action.ADD_T2, quantity=D("4"),
                    price=D("5"), intent_id="seed-writeoff-t2", timestamp_ms=300,
                )
                t3 = seed_buy_execution(
                    store, t2, action=Action.ADD_T3, quantity=D("24"),
                    price=D("2.5"), intent_id="seed-writeoff-t3", timestamp_ms=400,
                )
                exchange = _FilledExchange(price=D("0.16"))
                execution = ExecutionCoordinator(store=store, exchange=exchange)
                reviews = ReviewRunCoordinator(
                    store=store,
                    execution=execution,
                    symbol_reviews=SymbolReviewExecutor(store=store, exchange=exchange, execution=execution),
                    batch_source=_OneSymbolBatchSource(),
                    market_prices=exchange,
                )

                result = await reviews.run_slot(slot_start_ms=SLOT_START_MS)

                self.assertLess(t3.qty * D("0.16"), exchange._rules.min_market_notional)
                self.assertEqual(dict(result.result_counts).get("ERROR", 0), 0)
                self.assertEqual(exchange.submitted_sides, ["SELL", "BUY"])
                self.assertLess(exchange.events.index("submit BUY"), exchange.events.index("fills SELL"))
                final_state = store.load_symbol_state("BTCUSDT")
                self.assertEqual(final_state.tier, Tier.T1)
                self.assertEqual(final_state.qty, D("62.5"))
            finally:
                store.close()

    async def test_partial_full_close_retries_after_restart_and_price_falls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            exchange = _PartialTerminalThenFilledExchange(price=D("11"))
            store = SQLiteStateStore(database_path)
            try:
                idle = store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
                seed_buy_execution(
                    store, idle, action=Action.OPEN_T1, quantity=D("1"),
                    price=D("10"), intent_id="seed-partial-t1", timestamp_ms=200,
                )
                reviews = _reviews_with_matching_snapshot(store, exchange)
                with self.assertLogs("pnl_trader.application.review_run", level="WARNING"):
                    await reviews.run_slot(slot_start_ms=SLOT_START_MS)
                self.assertEqual(store.load_symbol_state("BTCUSDT").lifecycle, Lifecycle.CLOSING)
                self.assertEqual(store.load_symbol_state("BTCUSDT").qty, D("0.6"))
                self.assertEqual(exchange.submitted_sides, ["SELL"])
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                exchange.price = D("9")
                exchange.reject_next_sell = True
                reviews = _reviews_with_matching_snapshot(store, exchange)
                with self.assertLogs("pnl_trader.application.review_run", level="WARNING"):
                    await reviews.run_slot(slot_start_ms=SLOT_START_MS + 900_000)
                self.assertEqual(store.load_symbol_state("BTCUSDT").lifecycle, Lifecycle.CLOSING)
                self.assertEqual(store.load_symbol_state("BTCUSDT").qty, D("0.6"))
                self.assertEqual(exchange.submitted_sides, ["SELL", "SELL"])
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                reviews = _reviews_with_matching_snapshot(store, exchange)
                await reviews.run_slot(slot_start_ms=SLOT_START_MS + 1_800_000)
                self.assertEqual(exchange.submitted_sides, ["SELL", "SELL", "SELL", "BUY"])
                self.assertEqual(exchange.submitted_requests[2].quantity, D("0.6"))
                state = store.load_symbol_state("BTCUSDT")
                self.assertEqual(state.lifecycle, Lifecycle.OPEN)
                self.assertEqual(state.tier, Tier.T1)
                self.assertEqual(state.qty, D("1.111"))
            finally:
                store.close()

    async def test_unavailable_close_fills_are_recovered_after_reopening_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database_path)
            try:
                idle = store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
                t1 = seed_buy_execution(
                    store, idle, action=Action.OPEN_T1, quantity=D("0.1"),
                    price=D("10"), intent_id="seed-t1", timestamp_ms=200,
                )
                seed_buy_execution(
                    store, t1, action=Action.ADD_T2, quantity=D("0.4"),
                    price=D("50"), intent_id="seed-t2", timestamp_ms=300,
                )
                exchange = _FilledExchange()
                exchange.close_fills_available = False
                execution = ExecutionCoordinator(store=store, exchange=exchange)
                reviews = ReviewRunCoordinator(
                    store=store,
                    execution=execution,
                    symbol_reviews=SymbolReviewExecutor(store=store, exchange=exchange, execution=execution),
                    batch_source=_OneSymbolBatchSource(),
                    market_prices=exchange,
                )
                with self.assertLogs("pnl_trader.application.execution", level="WARNING"):
                    await reviews.run_slot(slot_start_ms=SLOT_START_MS)
                self.assertEqual(exchange.submitted_sides, ["SELL", "BUY"])
                self.assertEqual(store.load_symbol_state("BTCUSDT").tier, Tier.T1)
                self.assertEqual(len(store.list_full_closes_missing_fills()), 1)
                self.assertEqual(DashboardQueryService(database_path).get_overview()["pending_close_fill_count"], 1)
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                exchange.close_fills_available = True
                recovery = ExecutionCoordinator(store=store, exchange=exchange)
                await recovery.recover_full_close_fills()
                await recovery.recover_full_close_fills()
                self.assertEqual(store.list_full_closes_missing_fills(), ())
                self.assertEqual(store.load_symbol_state("BTCUSDT").tier, Tier.T1)
                self.assertEqual(DashboardQueryService(database_path).get_overview()["pending_close_fill_count"], 0)
            finally:
                store.close()

    async def test_unconfirmed_close_does_not_reopen(self) -> None:
        for exchange_type, expected_status in (
            (_PartialCloseExchange, "PARTIALLY_FILLED"),
            (_UnknownCloseExchange, "UNKNOWN"),
        ):
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as directory:
                store = SQLiteStateStore(Path(directory) / "state.sqlite3")
                try:
                    idle = store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
                    t1 = seed_buy_execution(
                        store, idle, action=Action.OPEN_T1, quantity=D("0.1"),
                        price=D("10"), intent_id="seed-t1", timestamp_ms=200,
                    )
                    seed_buy_execution(
                        store, t1, action=Action.ADD_T2, quantity=D("0.4"),
                        price=D("50"), intent_id="seed-t2", timestamp_ms=300,
                    )
                    exchange = exchange_type()
                    execution = ExecutionCoordinator(store=store, exchange=exchange)
                    reviews = ReviewRunCoordinator(
                        store=store,
                        execution=execution,
                        symbol_reviews=SymbolReviewExecutor(store=store, exchange=exchange, execution=execution),
                        batch_source=_OneSymbolBatchSource(),
                        market_prices=exchange,
                    )
                    await reviews.run_slot(slot_start_ms=SLOT_START_MS)
                    self.assertEqual(exchange.submitted_sides, ["SELL"])
                    self.assertEqual(store.load_symbol_state("BTCUSDT").tier, Tier.T2)
                    self.assertEqual(store.list_active_order_intents()[0].status.value, expected_status)
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


def _reviews_with_matching_snapshot(store, exchange):
    inputs = _MatchingReviewInputs(store)
    execution = ExecutionCoordinator(store=store, exchange=exchange)
    return ReviewRunCoordinator(
        store=store,
        execution=execution,
        symbol_reviews=SymbolReviewExecutor(store=store, exchange=exchange, execution=execution),
        batch_source=ReconciledReviewBatchSource(store=store, positions=inputs, signals=inputs),
        market_prices=exchange,
    )


class _MatchingReviewInputs:
    def __init__(self, store):
        self._store = store

    async def get_position_snapshot(self):
        state = self._store.load_symbol_state("BTCUSDT")
        return (ExchangePosition("BTCUSDT", "BOTH", state.qty),)

    async def load_review_signals(self, *, managed_symbols):
        if "BTCUSDT" not in managed_symbols:
            return ()
        return (ReviewSignal("BTCUSDT", admitted=True),)


class _FilledExchange:
    def __init__(self, *, price: Decimal = D("90")) -> None:
        self.price = price
        self.submitted_sides: list[str] = []
        self.submitted_requests: list[MarketOrderRequest] = []
        self.events: list[str] = []
        self.close_fills_available = True
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

    async def reserve_order_submission(self) -> None:
        return None

    async def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(D("100"), D("-5"), D("75"), D("200"))

    async def get_latest_prices(self) -> dict[str, Decimal]:
        return {"BTCUSDT": self.price}

    async def get_instrument_rules(self, symbol: str) -> InstrumentRules:
        return self._rules

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        self.submitted_sides.append(request.side)
        self.submitted_requests.append(request)
        self.events.append(f"submit {request.side}")
        index = len(self.submitted_sides)
        order_id = f"sim-order-{index}"
        fill = TradeFillRecord(
            symbol=request.symbol,
            exchange_trade_id=f"sim-trade-{index}",
            side=request.side,
            price=self.price,
            qty=request.quantity,
            realized_pnl=D("0"),
            commission=D("0"),
            commission_asset="USDT",
            traded_at_ms=NOW_MS,
        )
        order = ExchangeOrderSnapshot(
            status=ExchangeOrderStatus.FILLED,
            executed_qty=request.quantity,
            cumulative_quote=request.quantity * self.price,
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
        self.events.append(f"fills {order_side}")
        if order_side == "SELL" and not self.close_fills_available:
            raise RuntimeError("close fills are not available yet")
        return self._fills_by_exchange_order_id[order_snapshot.exchange_order_id]


class _PartialTerminalThenFilledExchange(_FilledExchange):
    def __init__(self, *, price: Decimal) -> None:
        super().__init__(price=price)
        self.reject_next_sell = False

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        if not self.submitted_sides:
            self.submitted_sides.append(request.side)
            self.submitted_requests.append(request)
            self.events.append(f"submit {request.side}")
            quantity = D("0.4")
            order = ExchangeOrderSnapshot(
                status=ExchangeOrderStatus.EXPIRED,
                executed_qty=quantity,
                cumulative_quote=quantity * self.price,
                exchange_order_id="partial-terminal",
            )
            self._fills_by_exchange_order_id[order.exchange_order_id] = (
                TradeFillRecord(
                    symbol=request.symbol, exchange_trade_id="partial-terminal-fill",
                    side="SELL", price=self.price, qty=quantity, realized_pnl=D("0"),
                    commission=D("0"), commission_asset="USDT", traded_at_ms=NOW_MS,
                ),
            )
            self._order_snapshots[request.client_order_id] = order
            return SubmissionResult(SubmissionCertainty.ACCEPTED, order=order)
        if request.side == "SELL" and self.reject_next_sell:
            self.reject_next_sell = False
            self.submitted_sides.append(request.side)
            self.submitted_requests.append(request)
            self.events.append(f"submit {request.side}")
            return SubmissionResult(SubmissionCertainty.REJECTED, detail="simulated rejection")
        return await super().submit_market_order(request)


class _PartialCloseExchange(_FilledExchange):
    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        self.submitted_sides.append(request.side)
        quantity = request.quantity / 2
        return SubmissionResult(
            SubmissionCertainty.ACCEPTED,
            order=ExchangeOrderSnapshot(
                status=ExchangeOrderStatus.PARTIALLY_FILLED,
                executed_qty=quantity,
                cumulative_quote=quantity * D("90"),
                exchange_order_id="partial-close",
            ),
        )


class _UnknownCloseExchange(_FilledExchange):
    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        self.submitted_sides.append(request.side)
        return SubmissionResult(SubmissionCertainty.UNKNOWN, detail="submission timed out")


if __name__ == "__main__":
    unittest.main()
