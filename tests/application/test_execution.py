import asyncio
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from .scripted_exchange import ScriptedExchangeGateway, ScriptedOrderScenario
from pnl_trader.adapters.sqlite import SQLiteStateStore
from pnl_trader.application.execution import ExecutionCoordinator
from pnl_trader.application.execution_models import (
    ExchangeOrderSnapshot,
    ExchangeOrderStatus,
    IntentStatus,
    MarketOrderRequest,
    OrderIntent,
    SubmissionCertainty,
    SubmissionResult,
)
from pnl_trader.application.position_models import ExchangePosition
from pnl_trader.application.review_batch import ReconciledReviewBatchSource
from pnl_trader.application.review_batch_models import ReviewSignal
from pnl_trader.application.store_errors import IntentConflict
from pnl_trader.domain import (
    Action,
    Lifecycle,
    Tier,
    TradeFillRecord,
)
from tests.helpers import seed_buy_execution


D = Decimal


class ExecutionAcceptanceTests(unittest.TestCase):
    def test_confirmed_dca_advances_state(self) -> None:
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
                    quantity=D("1"),
                    price=D("100"),
                    intent_id="seed-t1",
                    timestamp_ms=1_000,
                )
                intent = OrderIntent(
                    intent_id="intent-t2",
                    symbol="BTCUSDT",
                    action=Action.ADD_T2,
                    created_at_ms=2_000,
                )
                store.create_order_intent(intent)
                requested_qty = D("1")
                fill = TradeFillRecord(
                    symbol="BTCUSDT",
                    exchange_trade_id="sim-trade-t2",
                    side="BUY",
                    price=D("50"),
                    qty=D("1"),
                    realized_pnl=D("0"),
                    commission=D("0.05"),
                    commission_asset="USDT",
                    traded_at_ms=2_000,
                )
                order = ExchangeOrderSnapshot(
                    status=ExchangeOrderStatus.FILLED,
                    executed_qty=D("1"),
                    cumulative_quote=D("50"),
                    exchange_order_id="sim-order-t2",
                )
                gateway = ScriptedExchangeGateway(
                    {
                        intent.intent_id: ScriptedOrderScenario(
                            submission=SubmissionResult(
                                certainty=SubmissionCertainty.ACCEPTED,
                                order=order,
                            ),
                            fills=(fill,),
                        )
                    }
                )
                execution = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 3_000,
                )
                first = asyncio.run(execution.execute_prepared(intent, requested_qty))
                duplicate = asyncio.run(execution.execute_prepared(intent, requested_qty))

                state = store.load_symbol_state("BTCUSDT")
                self.assertEqual(first.status, IntentStatus.FILLED)
                self.assertEqual(duplicate.status, IntentStatus.FILLED)
                self.assertEqual(state.tier, Tier.T2)
                self.assertEqual(state.avg_entry_price, D("75"))
                self.assertEqual(store.list_active_order_intents(), ())
            finally:
                store.close()

    def test_unknown_submit_is_not_retried_and_restart_recovers_by_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database_path)
            try:
                state = store.ensure_idle_symbol_state(
                    "BTCUSDT", created_at_ms=100
                )
                intent = OrderIntent(
                    intent_id="intent-unknown-open",
                    symbol="BTCUSDT",
                    action=Action.OPEN_T1,
                    created_at_ms=2_000,
                )
                store.create_order_intent(intent)
                requested_qty = D("1")
                fill = TradeFillRecord(
                    symbol="BTCUSDT",
                    exchange_trade_id="unknown-trade-open",
                    side="BUY",
                    price=D("10"),
                    qty=D("1"),
                    realized_pnl=D("0"),
                    commission=D("0.01"),
                    commission_asset="USDT",
                    traded_at_ms=2_500,
                )
                terminal_order = ExchangeOrderSnapshot(
                        status=ExchangeOrderStatus.FILLED,
                        executed_qty=D("1"),
                        cumulative_quote=D("10"),
                        exchange_order_id="exchange-order-open",
                    )
                gateway = _ControllableExchangeGateway(
                    order=None,
                    fills=(fill,),
                )
                first_process = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 3_000,
                )

                submitted = asyncio.run(
                    first_process.execute_prepared(intent, requested_qty)
                )
                asyncio.run(first_process.recover_pending())
                repeated_execution = asyncio.run(
                    first_process.execute_prepared(intent, requested_qty)
                )

                self.assertEqual(submitted.status, IntentStatus.UNKNOWN)
                self.assertEqual(repeated_execution.status, IntentStatus.UNKNOWN)
                self.assertEqual(
                    store.get_intent_status(intent.intent_id),
                    IntentStatus.UNKNOWN,
                )
                self.assertEqual(gateway.submit_calls, 1)
                self.assertEqual(
                    store.load_symbol_state("BTCUSDT").qty,
                    D("0"),
                )
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                gateway.order = terminal_order
                second_process = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 4_000,
                )
                asyncio.run(second_process.recover_pending())

                self.assertEqual(
                    store.get_intent_status(intent.intent_id),
                    IntentStatus.FILLED,
                )
                self.assertEqual(gateway.submit_calls, 1)
                self.assertEqual(
                    store.load_symbol_state("BTCUSDT").qty,
                    D("1"),
                )
                asyncio.run(second_process.recover_pending())
                self.assertEqual(store.list_active_order_intents(), ())
            finally:
                store.close()

    def test_partial_fill_intent_survives_restart_until_terminal_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database_path)
            btc_state = store.ensure_idle_symbol_state(
                "BTCUSDT", created_at_ms=100
            )
            store.ensure_idle_symbol_state("ETHUSDT", created_at_ms=100)
            intent = OrderIntent(
                intent_id="intent-partial-open",
                symbol="BTCUSDT",
                action=Action.OPEN_T1,
                created_at_ms=2_000,
            )
            store.create_order_intent(intent)
            requested_qty = D("0.1")
            partial_order = ExchangeOrderSnapshot(
                status=ExchangeOrderStatus.PARTIALLY_FILLED,
                executed_qty=D("0.04"),
                cumulative_quote=D("4"),
                exchange_order_id="partial-open-order",
            )
            canceled_order = ExchangeOrderSnapshot(
                status=ExchangeOrderStatus.CANCELED,
                executed_qty=D("0.04"),
                cumulative_quote=D("4"),
                exchange_order_id="partial-open-order",
            )
            fill = TradeFillRecord(
                symbol="BTCUSDT",
                exchange_trade_id="partial-open-trade",
                side="BUY",
                price=D("100"),
                qty=D("0.04"),
                realized_pnl=D("0"),
                commission=D("0.002"),
                commission_asset="USDT",
                traded_at_ms=2_500,
            )
            gateway = _DelayedOrderFillsGateway(
                {
                    intent.intent_id: ScriptedOrderScenario(
                        submission=SubmissionResult(
                            certainty=SubmissionCertainty.ACCEPTED,
                            order=partial_order,
                        ),
                        lookup_snapshot=canceled_order,
                        fills=(fill,),
                    )
                }
            )
            first_process = ExecutionCoordinator(
                store=store,
                exchange=gateway,
                clock_ms=lambda: 3_000,
            )
            try:
                submitted = asyncio.run(
                    first_process.execute_prepared(intent, requested_qty)
                )
                self.assertEqual(submitted.status, IntentStatus.PARTIALLY_FILLED)
                self.assertEqual(
                    store.load_symbol_state("BTCUSDT").lifecycle,
                    Lifecycle.IDLE,
                )
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                recovered_before_terminal = store.list_active_order_intents()
                self.assertEqual(len(recovered_before_terminal), 1)
                self.assertEqual(
                    recovered_before_terminal[0].status,
                    IntentStatus.PARTIALLY_FILLED,
                )

                second_process = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 4_000,
                )
                asyncio.run(second_process.recover_pending())

                self.assertEqual(gateway.fill_queries, 1)
                still_active = store.list_active_order_intents()
                self.assertEqual(len(still_active), 1)
                self.assertEqual(still_active[0].status, IntentStatus.UNKNOWN)
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                active_after_second_restart = store.list_active_order_intents()
                self.assertEqual(len(active_after_second_restart), 1)
                self.assertEqual(
                    active_after_second_restart[0].status,
                    IntentStatus.UNKNOWN,
                )
                third_process = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 5_000,
                )
                asyncio.run(third_process.recover_pending())

                self.assertEqual(
                    store.get_intent_status(intent.intent_id),
                    IntentStatus.CANCELED,
                )
                self.assertEqual(gateway.fill_queries, 2)
                self.assertEqual(
                    store.load_symbol_state("BTCUSDT").qty,
                    D("0.04"),
                )
                self.assertEqual(store.list_active_order_intents(), ())
            finally:
                store.close()

    def test_terminal_fill_mismatch_keeps_intent_until_reconciled_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database_path)
            btc_state = store.ensure_idle_symbol_state(
                "BTCUSDT", created_at_ms=100
            )
            store.ensure_idle_symbol_state("ETHUSDT", created_at_ms=100)
            intent = OrderIntent(
                intent_id="intent-inconsistent-open",
                symbol="BTCUSDT",
                action=Action.OPEN_T1,
                created_at_ms=2_000,
            )
            store.create_order_intent(intent)
            requested_qty = D("0.1")
            terminal_order = ExchangeOrderSnapshot(
                status=ExchangeOrderStatus.CANCELED,
                executed_qty=D("0.04"),
                cumulative_quote=D("4"),
                exchange_order_id="inconsistent-open-order",
            )
            correct_fill = TradeFillRecord(
                symbol="BTCUSDT",
                exchange_trade_id="inconsistent-open-trade",
                side="BUY",
                price=D("100"),
                qty=D("0.04"),
                realized_pnl=D("0"),
                commission=D("0.002"),
                commission_asset="USDT",
                traded_at_ms=2_500,
            )
            mismatched_fill = TradeFillRecord(
                symbol="BTCUSDT",
                exchange_trade_id="inconsistent-open-trade",
                side="BUY",
                price=D("99"),
                qty=D("0.04"),
                realized_pnl=D("0"),
                commission=D("0.002"),
                commission_asset="USDT",
                traded_at_ms=2_500,
            )
            gateway = _MismatchedOnceFillsGateway(
                {
                    intent.intent_id: ScriptedOrderScenario(
                        submission=SubmissionResult(
                            certainty=SubmissionCertainty.ACCEPTED,
                            order=terminal_order,
                        ),
                        lookup_snapshot=terminal_order,
                        fills=(correct_fill,),
                    )
                },
                mismatched_fills=(mismatched_fill,),
            )
            execution = ExecutionCoordinator(
                store=store,
                exchange=gateway,
                clock_ms=lambda: 3_000,
            )
            try:
                with self.assertRaisesRegex(IntentConflict, "do not match"):
                    asyncio.run(execution.execute_prepared(intent, requested_qty))

                self.assertEqual(store.get_intent_status(intent.intent_id), IntentStatus.PENDING)
                self.assertEqual(
                    store.load_symbol_state("BTCUSDT").lifecycle,
                    Lifecycle.IDLE,
                )
                self.assertEqual(gateway.fill_queries, 1)
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                active = store.list_active_order_intents()
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0].status, IntentStatus.PENDING)

                recovery = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 4_000,
                )
                asyncio.run(recovery.recover_pending())

                self.assertEqual(
                    store.get_intent_status(intent.intent_id),
                    IntentStatus.CANCELED,
                )
                self.assertEqual(gateway.fill_queries, 2)
                self.assertEqual(
                    store.load_symbol_state("BTCUSDT").qty,
                    D("0.04"),
                )
                self.assertEqual(store.list_active_order_intents(), ())
            finally:
                store.close()

    def test_terminal_partial_cancel_applies_only_confirmed_fill_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                state = _seed_open_t1(store)
                intent = OrderIntent(
                    intent_id="intent-partial-close",
                    symbol="BTCUSDT",
                    action=Action.CLOSE_T1,
                    created_at_ms=2_000,
                    target_cumulative_sold_qty=state.tier_entry_qty,
                )
                store.create_order_intent(intent)
                requested_qty = state.qty
                initial_order = ExchangeOrderSnapshot(
                    status=ExchangeOrderStatus.PARTIALLY_FILLED,
                    executed_qty=D("0.2"),
                    cumulative_quote=D("2"),
                    exchange_order_id="exchange-order-partial-close",
                )
                canceled_order = ExchangeOrderSnapshot(
                    status=ExchangeOrderStatus.CANCELED,
                    executed_qty=D("0.4"),
                    cumulative_quote=D("4"),
                    exchange_order_id="exchange-order-partial-close",
                )
                fill = TradeFillRecord(
                    symbol="BTCUSDT",
                    exchange_trade_id="partial-close-trade",
                    side="SELL",
                    price=D("10"),
                    qty=D("0.4"),
                    realized_pnl=D("0"),
                    commission=D("0.004"),
                    commission_asset="USDT",
                    traded_at_ms=2_500,
                )
                gateway = ScriptedExchangeGateway(
                    {
                        intent.intent_id: ScriptedOrderScenario(
                            submission=SubmissionResult(
                                certainty=SubmissionCertainty.ACCEPTED,
                                order=initial_order,
                            ),
                            lookup_snapshot=canceled_order,
                            fills=(fill,),
                        )
                    }
                )
                execution = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 3_000,
                )

                partial = asyncio.run(
                    execution.execute_prepared(intent, requested_qty)
                )
                unchanged = store.load_symbol_state("BTCUSDT")
                asyncio.run(execution.recover_pending())
                applied = store.load_symbol_state("BTCUSDT")
                duplicate = asyncio.run(
                    execution.execute_prepared(intent, requested_qty)
                )

                self.assertEqual(partial.status, IntentStatus.PARTIALLY_FILLED)
                self.assertEqual(unchanged.qty, D("1"))
                self.assertEqual(
                    store.get_intent_status(intent.intent_id),
                    IntentStatus.CANCELED,
                )
                self.assertEqual(applied.qty, D("0.6"))
                self.assertEqual(applied.tier_sold_qty, D("0.4"))
                self.assertEqual(applied.lifecycle, Lifecycle.CLOSING)
                self.assertEqual(duplicate.status, IntentStatus.CANCELED)
                self.assertEqual(store.load_symbol_state("BTCUSDT"), applied)
            finally:
                store.close()

    def test_reopen_review_rejects_nonzero_exchange_position_after_full_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                state = _seed_open_t1(store)
                intent = OrderIntent(
                    intent_id="intent-full-close",
                    symbol="BTCUSDT",
                    action=Action.CLOSE_T1,
                    created_at_ms=2_000,
                    target_cumulative_sold_qty=state.tier_entry_qty,
                )
                store.create_order_intent(intent)
                requested_qty = state.qty
                partial_order = ExchangeOrderSnapshot(
                    status=ExchangeOrderStatus.PARTIALLY_FILLED,
                    executed_qty=D("0.4"),
                    cumulative_quote=D("4"),
                    exchange_order_id="exchange-order-full-close",
                )
                filled_order = ExchangeOrderSnapshot(
                    status=ExchangeOrderStatus.FILLED,
                    executed_qty=D("1"),
                    cumulative_quote=D("10"),
                    exchange_order_id="exchange-order-full-close",
                )
                fills = (
                    TradeFillRecord(
                        symbol="BTCUSDT",
                        exchange_trade_id="full-close-trade-1",
                        side="SELL",
                        price=D("10"),
                        qty=D("0.4"),
                        realized_pnl=D("0"),
                        commission=D("0.004"),
                        commission_asset="USDT",
                        traded_at_ms=2_500,
                    ),
                    TradeFillRecord(
                        symbol="BTCUSDT",
                        exchange_trade_id="full-close-trade-2",
                        side="SELL",
                        price=D("10"),
                        qty=D("0.6"),
                        realized_pnl=D("0"),
                        commission=D("0.006"),
                        commission_asset="USDT",
                        traded_at_ms=2_600,
                    ),
                )
                gateway = ScriptedExchangeGateway(
                    {
                        intent.intent_id: ScriptedOrderScenario(
                            submission=SubmissionResult(
                                certainty=SubmissionCertainty.ACCEPTED,
                                order=partial_order,
                            ),
                            lookup_snapshot=filled_order,
                            fills=fills,
                        )
                    }
                )
                execution = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 3_000,
                )

                in_flight = asyncio.run(
                    execution.execute_prepared(intent, requested_qty)
                )
                before_terminal = store.load_symbol_state("BTCUSDT")
                asyncio.run(execution.recover_pending())
                after_terminal = store.load_symbol_state("BTCUSDT")

                self.assertEqual(in_flight.status, IntentStatus.PARTIALLY_FILLED)
                self.assertEqual(before_terminal.lifecycle, Lifecycle.OPEN)
                self.assertEqual(before_terminal.qty, D("1"))
                self.assertEqual(
                    store.get_intent_status(intent.intent_id),
                    IntentStatus.FILLED,
                )
                self.assertEqual(after_terminal.lifecycle, Lifecycle.REOPEN_PENDING)
                self.assertEqual(after_terminal.qty, D("0"))

                class PositionSource:
                    async def get_position_snapshot(self):
                        return (
                            ExchangePosition(
                                symbol="BTCUSDT",
                                position_side="BOTH",
                                position_amt=D("0.1"),
                            ),
                        )

                class SignalSource:
                    async def load_review_signals(
                        self,
                        *,
                        managed_symbols: tuple[str, ...],
                    ):
                        return (
                            ReviewSignal(
                                symbol="BTCUSDT",
                                admitted=True,
                            ),
                        )

                review_source = ReconciledReviewBatchSource(
                    store=store,
                    positions=PositionSource(),
                    signals=SignalSource(),
                    clock_ms=lambda: 4_000,
                )
                with self.assertRaisesRegex(RuntimeError, "position mismatch"):
                    asyncio.run(review_source.load_review_batch(0))
            finally:
                store.close()


class _ControllableExchangeGateway:
    """External exchange boundary with delayed order visibility."""

    def __init__(
        self,
        *,
        order: ExchangeOrderSnapshot | None,
        fills: tuple[TradeFillRecord, ...],
    ) -> None:
        self.order = order
        self.fills = fills
        self.submit_calls = 0

    async def reserve_order_submission(self) -> None:
        return None

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult:
        self.submit_calls += 1
        return SubmissionResult(
            certainty=SubmissionCertainty.UNKNOWN,
            detail="response lost after the exchange accepted the order",
        )

    async def lookup_order(
        self,
        symbol: str,
        client_order_id: str,
    ) -> ExchangeOrderSnapshot | None:
        if any(fill.symbol != symbol for fill in self.fills):
            raise AssertionError("lookup request does not match the scripted order")
        if self.order is None:
            return None
        return self.order

    async def list_order_fills(
        self,
        symbol: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]:
        if any(fill.symbol != symbol for fill in self.fills):
            raise AssertionError("fill request does not match the scripted order")
        return self.fills


class _DelayedOrderFillsGateway(ScriptedExchangeGateway):
    """Fail the first terminal fill lookup, then expose the reconciled fills."""

    def __init__(self, scenarios) -> None:
        super().__init__(scenarios)
        self.fill_queries = 0

    async def list_order_fills(
        self,
        symbol: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]:
        self.fill_queries += 1
        if self.fill_queries == 1:
            raise TimeoutError("delayed trade history unavailable")
        return await super().list_order_fills(
            symbol,
            order_snapshot,
            order_side,
        )


class _MismatchedOnceFillsGateway(ScriptedExchangeGateway):
    """Return one inconsistent fill page, then the exchange's correct records."""

    def __init__(self, scenarios, *, mismatched_fills: tuple[TradeFillRecord, ...]) -> None:
        super().__init__(scenarios)
        self._mismatched_fills = mismatched_fills
        self.fill_queries = 0

    async def list_order_fills(
        self,
        symbol: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]:
        self.fill_queries += 1
        if self.fill_queries == 1:
            return self._mismatched_fills
        return await super().list_order_fills(
            symbol,
            order_snapshot,
            order_side,
        )


def _seed_open_t1(store: SQLiteStateStore):
    idle = store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
    return seed_buy_execution(
        store,
        idle,
        action=Action.OPEN_T1,
        quantity=D("1"),
        price=D("10"),
        intent_id="seed-open-t1",
        timestamp_ms=1_000,
    )


if __name__ == "__main__":
    unittest.main()
