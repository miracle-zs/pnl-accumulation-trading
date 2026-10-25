import asyncio
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from pnl_trader.adapters.simulated import ScriptedExchangeGateway, SimulatedOrderScenario
from pnl_trader.adapters.sqlite import (
    IntentConflict,
    OrderAttempt,
    OrderIntent,
    ReservationLimitExceeded,
    SQLiteStateStore,
)
from pnl_trader.application.execution import ExecutionCoordinator, ExecutionResolution
from pnl_trader.application.execution_models import (
    ExchangeOrderSnapshot,
    ExchangeOrderStatus,
    IntentStatus,
    MarketOrderRequest,
    SubmissionCertainty,
    SubmissionResult,
)
from pnl_trader.application.order_reconciliation_models import ExchangeOpenOrdersSnapshot
from pnl_trader.application.position_models import ExchangePosition, ExchangePositionSnapshot
from pnl_trader.application.review_batch import (
    ReconciledReviewBatchSource,
    ReviewBatchSourceError,
)
from pnl_trader.application.review_batch_models import ReviewSignal, ReviewSignalSnapshot
from pnl_trader.domain import (
    Action,
    ExecutionReceipt,
    Lifecycle,
    Observation,
    Tier,
    TradeFillRecord,
    apply_confirmed_execution,
)


D = Decimal


class SimulatedExecutionAcceptanceTests(unittest.TestCase):
    def test_confirmed_simulated_dca_advances_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                idle = store.ensure_idle_symbol_state(
                    "primary", "BTCUSDT", created_at_ms=100
                )
                t1 = apply_confirmed_execution(
                    idle,
                    ExecutionReceipt(Action.OPEN_T1, D("1"), D("100")),
                )
                store.save_symbol_state(
                    "primary",
                    t1,
                    expected_version=idle.version,
                    updated_at_ms=1_000,
                )
                intent = OrderIntent(
                    intent_id="intent-simulated-t2",
                    account_id="primary",
                    symbol="BTCUSDT",
                    cycle_id=t1.cycle_id,
                    action=Action.ADD_T2,
                    business_key="simulated-review-slot-1",
                    expected_state_version=t1.version,
                    reserved_margin=D("1"),
                    created_at_ms=2_000,
                    buy_notional=D("50"),
                )
                self.assertTrue(store.create_order_intent(intent))
                attempt = OrderAttempt(
                    attempt_id="attempt-simulated-t2",
                    intent_id=intent.intent_id,
                    attempt_no=1,
                    client_order_id="sim-client-t2",
                    requested_qty=D("1"),
                    sent_at_ms=2_000,
                )
                fill = TradeFillRecord(
                    symbol="BTCUSDT",
                    exchange_trade_id="sim-trade-t2",
                    client_order_id=attempt.client_order_id,
                    side="BUY",
                    price=D("50"),
                    qty=D("1"),
                    realized_pnl=D("0"),
                    commission=D("0.05"),
                    commission_asset="USDT",
                    traded_at_ms=2_000,
                    exchange_order_id="sim-order-t2",
                    position_side="LONG",
                    margin_asset="USDT",
                )
                order = ExchangeOrderSnapshot(
                    status=ExchangeOrderStatus.FILLED,
                    executed_qty=D("1"),
                    cumulative_quote=D("50"),
                    exchange_order_id="sim-order-t2",
                )
                gateway = ScriptedExchangeGateway(
                    {
                        attempt.client_order_id: SimulatedOrderScenario(
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
                request = MarketOrderRequest(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=D("1"),
                    client_order_id=attempt.client_order_id,
                    reduce_only=False,
                )

                first = asyncio.run(execution.execute_prepared(intent, attempt, request))
                duplicate = asyncio.run(execution.execute_prepared(intent, attempt, request))

                state = store.load_symbol_state("primary", "BTCUSDT")
                self.assertEqual(first.status, IntentStatus.FILLED)
                self.assertEqual(first.resolution, ExecutionResolution.FILLS_APPLIED)
                self.assertEqual(duplicate.resolution, ExecutionResolution.ALREADY_TERMINAL)
                self.assertEqual(state.tier, Tier.T2)
                self.assertEqual(state.avg_entry_price, D("75"))
                self.assertEqual(store.list_active_order_intents("primary"), ())
            finally:
                store.close()

    def test_unknown_submit_is_not_retried_and_restart_recovers_by_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database_path)
            try:
                state = store.ensure_idle_symbol_state(
                    "primary", "BTCUSDT", created_at_ms=100
                )
                intent = OrderIntent(
                    intent_id="intent-unknown-open",
                    account_id="primary",
                    symbol="BTCUSDT",
                    cycle_id=state.cycle_id,
                    action=Action.OPEN_T1,
                    business_key="unknown-submit-slot",
                    expected_state_version=state.version,
                    reserved_margin=D("1"),
                    created_at_ms=2_000,
                    buy_notional=D("10"),
                )
                self.assertTrue(store.create_order_intent(intent))
                attempt = OrderAttempt(
                    attempt_id="attempt-unknown-open",
                    intent_id=intent.intent_id,
                    attempt_no=1,
                    client_order_id="unknown-client-open",
                    requested_qty=D("1"),
                    sent_at_ms=2_000,
                )
                request = MarketOrderRequest(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=D("1"),
                    client_order_id=attempt.client_order_id,
                    reduce_only=False,
                )
                fill = TradeFillRecord(
                    symbol="BTCUSDT",
                    exchange_trade_id="unknown-trade-open",
                    client_order_id=attempt.client_order_id,
                    side="BUY",
                    price=D("10"),
                    qty=D("1"),
                    realized_pnl=D("0"),
                    commission=D("0.01"),
                    commission_asset="USDT",
                    traded_at_ms=2_500,
                    exchange_order_id="exchange-order-open",
                    position_side="LONG",
                    margin_asset="USDT",
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
                    first_process.execute_prepared(intent, attempt, request)
                )
                missing_lookup = asyncio.run(first_process.recover_pending("primary"))
                repeated_execution = asyncio.run(
                    first_process.execute_prepared(intent, attempt, request)
                )

                self.assertEqual(submitted.status, IntentStatus.UNKNOWN)
                self.assertEqual(missing_lookup[0].status, IntentStatus.UNKNOWN)
                self.assertEqual(repeated_execution.status, IntentStatus.UNKNOWN)
                self.assertEqual(gateway.submit_calls, 1)
                self.assertEqual(
                    store.load_symbol_state("primary", "BTCUSDT").qty,
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
                recovered = asyncio.run(second_process.recover_pending("primary"))

                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].status, IntentStatus.FILLED)
                self.assertEqual(gateway.submit_calls, 1)
                self.assertEqual(
                    store.load_symbol_state("primary", "BTCUSDT").qty,
                    D("1"),
                )
                self.assertEqual(asyncio.run(second_process.recover_pending("primary")), ())
            finally:
                store.close()

    def test_partial_fill_reservation_survives_restart_until_terminal_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database_path)
            btc_state = store.ensure_idle_symbol_state(
                "primary", "BTCUSDT", created_at_ms=100
            )
            store.ensure_idle_symbol_state("primary", "ETHUSDT", created_at_ms=100)
            intent = OrderIntent(
                intent_id="intent-partial-open",
                account_id="primary",
                symbol="BTCUSDT",
                cycle_id=btc_state.cycle_id,
                action=Action.OPEN_T1,
                business_key="partial-open-slot",
                expected_state_version=btc_state.version,
                reserved_margin=D("1.055"),
                created_at_ms=2_000,
                buy_notional=D("10"),
            )
            competitor = OrderIntent(
                intent_id="intent-eth-open",
                account_id="primary",
                symbol="ETHUSDT",
                cycle_id=0,
                action=Action.OPEN_T1,
                business_key="eth-open-slot",
                expected_state_version=0,
                reserved_margin=D("1.055"),
                created_at_ms=2_001,
                buy_notional=D("10"),
            )
            self.assertTrue(
                store.create_order_intent(
                    intent,
                    max_active_reserved_margin=D("1.5"),
                )
            )
            attempt = OrderAttempt(
                attempt_id="attempt-partial-open",
                intent_id=intent.intent_id,
                attempt_no=1,
                client_order_id="partial-client-open",
                requested_qty=D("0.1"),
                sent_at_ms=2_000,
            )
            request = MarketOrderRequest(
                symbol="BTCUSDT",
                side="BUY",
                quantity=D("0.1"),
                client_order_id=attempt.client_order_id,
                reduce_only=False,
            )
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
                client_order_id=attempt.client_order_id,
                side="BUY",
                price=D("100"),
                qty=D("0.04"),
                realized_pnl=D("0"),
                commission=D("0.002"),
                commission_asset="USDT",
                traded_at_ms=2_500,
                exchange_order_id="partial-open-order",
            )
            gateway = _DelayedOrderFillsGateway(
                {
                    attempt.client_order_id: SimulatedOrderScenario(
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
                    first_process.execute_prepared(intent, attempt, request)
                )
                self.assertEqual(submitted.status, IntentStatus.PARTIALLY_FILLED)
                self.assertEqual(
                    store.load_symbol_state("primary", "BTCUSDT").lifecycle,
                    Lifecycle.IDLE,
                )
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                recovered_before_terminal = store.list_active_order_intents("primary")
                self.assertEqual(len(recovered_before_terminal), 1)
                self.assertEqual(
                    recovered_before_terminal[0].status,
                    IntentStatus.PARTIALLY_FILLED,
                )
                self.assertEqual(
                    recovered_before_terminal[0].intent.reserved_margin,
                    D("1.055"),
                )
                with self.assertRaises(ReservationLimitExceeded):
                    store.create_order_intent(
                        competitor,
                        max_active_reserved_margin=D("1.5"),
                    )

                second_process = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 4_000,
                )
                missing_fills = asyncio.run(second_process.recover_pending("primary"))

                self.assertEqual(len(missing_fills), 1)
                self.assertEqual(missing_fills[0].status, IntentStatus.UNKNOWN)
                self.assertEqual(gateway.fill_queries, 1)
                still_active = store.list_active_order_intents("primary")
                self.assertEqual(len(still_active), 1)
                self.assertEqual(still_active[0].status, IntentStatus.UNKNOWN)
                self.assertEqual(
                    still_active[0].intent.reserved_margin,
                    D("1.055"),
                )
                with self.assertRaises(ReservationLimitExceeded):
                    store.create_order_intent(
                        competitor,
                        max_active_reserved_margin=D("1.5"),
                    )
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                active_after_second_restart = store.list_active_order_intents("primary")
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
                recovered = asyncio.run(third_process.recover_pending("primary"))

                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].status, IntentStatus.CANCELED)
                self.assertEqual(gateway.fill_queries, 2)
                self.assertEqual(
                    store.load_symbol_state("primary", "BTCUSDT").qty,
                    D("0.04"),
                )
                self.assertEqual(store.list_active_order_intents("primary"), ())
                self.assertTrue(
                    store.create_order_intent(
                        competitor,
                        max_active_reserved_margin=D("1.5"),
                    )
                )
            finally:
                store.close()

    def test_terminal_fill_mismatch_keeps_reservation_until_reconciled_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database_path)
            btc_state = store.ensure_idle_symbol_state(
                "primary", "BTCUSDT", created_at_ms=100
            )
            store.ensure_idle_symbol_state("primary", "ETHUSDT", created_at_ms=100)
            intent = OrderIntent(
                intent_id="intent-inconsistent-open",
                account_id="primary",
                symbol="BTCUSDT",
                cycle_id=btc_state.cycle_id,
                action=Action.OPEN_T1,
                business_key="inconsistent-open-slot",
                expected_state_version=btc_state.version,
                reserved_margin=D("1.055"),
                created_at_ms=2_000,
                buy_notional=D("10"),
            )
            competitor = OrderIntent(
                intent_id="intent-eth-after-mismatch",
                account_id="primary",
                symbol="ETHUSDT",
                cycle_id=0,
                action=Action.OPEN_T1,
                business_key="eth-after-mismatch-slot",
                expected_state_version=0,
                reserved_margin=D("1.055"),
                created_at_ms=2_001,
                buy_notional=D("10"),
            )
            self.assertTrue(
                store.create_order_intent(
                    intent,
                    max_active_reserved_margin=D("1.5"),
                )
            )
            attempt = OrderAttempt(
                attempt_id="attempt-inconsistent-open",
                intent_id=intent.intent_id,
                attempt_no=1,
                client_order_id="inconsistent-client-open",
                requested_qty=D("0.1"),
                sent_at_ms=2_000,
            )
            request = MarketOrderRequest(
                symbol="BTCUSDT",
                side="BUY",
                quantity=D("0.1"),
                client_order_id=attempt.client_order_id,
                reduce_only=False,
            )
            terminal_order = ExchangeOrderSnapshot(
                status=ExchangeOrderStatus.CANCELED,
                executed_qty=D("0.04"),
                cumulative_quote=D("4"),
                exchange_order_id="inconsistent-open-order",
            )
            correct_fill = TradeFillRecord(
                symbol="BTCUSDT",
                exchange_trade_id="inconsistent-open-trade",
                client_order_id=attempt.client_order_id,
                side="BUY",
                price=D("100"),
                qty=D("0.04"),
                realized_pnl=D("0"),
                commission=D("0.002"),
                commission_asset="USDT",
                traded_at_ms=2_500,
                exchange_order_id="inconsistent-open-order",
            )
            mismatched_fill = TradeFillRecord(
                symbol="BTCUSDT",
                exchange_trade_id="inconsistent-open-trade",
                client_order_id=attempt.client_order_id,
                side="BUY",
                price=D("99"),
                qty=D("0.04"),
                realized_pnl=D("0"),
                commission=D("0.002"),
                commission_asset="USDT",
                traded_at_ms=2_500,
                exchange_order_id="inconsistent-open-order",
            )
            gateway = _MismatchedOnceFillsGateway(
                {
                    attempt.client_order_id: SimulatedOrderScenario(
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
                    asyncio.run(execution.execute_prepared(intent, attempt, request))

                self.assertEqual(store.get_intent_status(intent.intent_id), IntentStatus.SUBMITTING)
                self.assertEqual(
                    store.load_symbol_state("primary", "BTCUSDT").lifecycle,
                    Lifecycle.IDLE,
                )
                self.assertEqual(gateway.fill_queries, 1)
                with self.assertRaises(ReservationLimitExceeded):
                    store.create_order_intent(
                        competitor,
                        max_active_reserved_margin=D("1.5"),
                    )
            finally:
                store.close()

            store = SQLiteStateStore(database_path)
            try:
                active = store.list_active_order_intents("primary")
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0].status, IntentStatus.SUBMITTING)
                self.assertEqual(active[0].intent.reserved_margin, D("1.055"))

                recovery = ExecutionCoordinator(
                    store=store,
                    exchange=gateway,
                    clock_ms=lambda: 4_000,
                )
                reconciled = asyncio.run(recovery.recover_pending("primary"))

                self.assertEqual(len(reconciled), 1)
                self.assertEqual(reconciled[0].status, IntentStatus.CANCELED)
                self.assertEqual(gateway.fill_queries, 2)
                self.assertEqual(
                    store.load_symbol_state("primary", "BTCUSDT").qty,
                    D("0.04"),
                )
                self.assertEqual(store.list_active_order_intents("primary"), ())
                self.assertTrue(
                    store.create_order_intent(
                        competitor,
                        max_active_reserved_margin=D("1.5"),
                    )
                )
            finally:
                store.close()

    def test_terminal_partial_cancel_applies_only_confirmed_fill_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                state = _seed_open_t1(store)
                intent = OrderIntent(
                    intent_id="intent-partial-close",
                    account_id="primary",
                    symbol="BTCUSDT",
                    cycle_id=state.cycle_id,
                    action=Action.CLOSE_T1,
                    business_key="partial-close-slot",
                    expected_state_version=state.version,
                    reserved_margin=D("0"),
                    created_at_ms=2_000,
                    target_cumulative_sold_qty=state.tier_entry_qty,
                )
                self.assertTrue(store.create_order_intent(intent))
                attempt = OrderAttempt(
                    attempt_id="attempt-partial-close",
                    intent_id=intent.intent_id,
                    attempt_no=1,
                    client_order_id="partial-client-close",
                    requested_qty=state.qty,
                    sent_at_ms=2_000,
                )
                request = MarketOrderRequest(
                    symbol="BTCUSDT",
                    side="SELL",
                    quantity=state.qty,
                    client_order_id=attempt.client_order_id,
                    reduce_only=True,
                )
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
                    client_order_id=attempt.client_order_id,
                    side="SELL",
                    price=D("10"),
                    qty=D("0.4"),
                    realized_pnl=D("0"),
                    commission=D("0.004"),
                    commission_asset="USDT",
                    traded_at_ms=2_500,
                    exchange_order_id="exchange-order-partial-close",
                    position_side="LONG",
                    margin_asset="USDT",
                )
                gateway = ScriptedExchangeGateway(
                    {
                        attempt.client_order_id: SimulatedOrderScenario(
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
                    execution.execute_prepared(intent, attempt, request)
                )
                unchanged = store.load_symbol_state("primary", "BTCUSDT")
                canceled = asyncio.run(execution.recover_pending("primary"))
                applied = store.load_symbol_state("primary", "BTCUSDT")
                duplicate = asyncio.run(
                    execution.execute_prepared(intent, attempt, request)
                )

                self.assertEqual(partial.status, IntentStatus.PARTIALLY_FILLED)
                self.assertEqual(unchanged.qty, D("1"))
                self.assertEqual(unchanged.version, state.version)
                self.assertEqual(canceled[0].status, IntentStatus.CANCELED)
                self.assertEqual(applied.qty, D("0.6"))
                self.assertEqual(applied.tier_sold_qty, D("0.4"))
                self.assertEqual(applied.lifecycle, Lifecycle.OPEN)
                self.assertEqual(applied.version, state.version + 1)
                self.assertEqual(duplicate.resolution, ExecutionResolution.ALREADY_TERMINAL)
                self.assertEqual(
                    store.load_symbol_state("primary", "BTCUSDT").version,
                    applied.version,
                )
            finally:
                store.close()

    def test_reopen_review_rejects_nonzero_exchange_position_after_full_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                state = _seed_open_t1(store)
                intent = OrderIntent(
                    intent_id="intent-full-close",
                    account_id="primary",
                    symbol="BTCUSDT",
                    cycle_id=state.cycle_id,
                    action=Action.CLOSE_T1,
                    business_key="full-close-slot",
                    expected_state_version=state.version,
                    reserved_margin=D("0"),
                    created_at_ms=2_000,
                    target_cumulative_sold_qty=state.tier_entry_qty,
                )
                self.assertTrue(store.create_order_intent(intent))
                attempt = OrderAttempt(
                    attempt_id="attempt-full-close",
                    intent_id=intent.intent_id,
                    attempt_no=1,
                    client_order_id="full-client-close",
                    requested_qty=state.qty,
                    sent_at_ms=2_000,
                )
                request = MarketOrderRequest(
                    symbol="BTCUSDT",
                    side="SELL",
                    quantity=state.qty,
                    client_order_id=attempt.client_order_id,
                    reduce_only=True,
                )
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
                        client_order_id=attempt.client_order_id,
                        side="SELL",
                        price=D("10"),
                        qty=D("0.4"),
                        realized_pnl=D("0"),
                        commission=D("0.004"),
                        commission_asset="USDT",
                        traded_at_ms=2_500,
                        exchange_order_id="exchange-order-full-close",
                        position_side="LONG",
                        margin_asset="USDT",
                    ),
                    TradeFillRecord(
                        symbol="BTCUSDT",
                        exchange_trade_id="full-close-trade-2",
                        client_order_id=attempt.client_order_id,
                        side="SELL",
                        price=D("10"),
                        qty=D("0.6"),
                        realized_pnl=D("0"),
                        commission=D("0.006"),
                        commission_asset="USDT",
                        traded_at_ms=2_600,
                        exchange_order_id="exchange-order-full-close",
                        position_side="LONG",
                        margin_asset="USDT",
                    ),
                )
                gateway = ScriptedExchangeGateway(
                    {
                        attempt.client_order_id: SimulatedOrderScenario(
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
                    execution.execute_prepared(intent, attempt, request)
                )
                before_terminal = store.load_symbol_state("primary", "BTCUSDT")
                terminal = asyncio.run(execution.recover_pending("primary"))
                after_terminal = store.load_symbol_state("primary", "BTCUSDT")

                self.assertEqual(in_flight.status, IntentStatus.PARTIALLY_FILLED)
                self.assertEqual(before_terminal.lifecycle, Lifecycle.OPEN)
                self.assertEqual(before_terminal.qty, D("1"))
                self.assertEqual(terminal[0].status, IntentStatus.FILLED)
                self.assertEqual(after_terminal.lifecycle, Lifecycle.REOPEN_PENDING)
                self.assertEqual(after_terminal.qty, D("0"))

                class PositionSource:
                    async def get_position_snapshot(self, account_id: str):
                        return ExchangePositionSnapshot(
                            account_id=account_id,
                            sampled_at_ms=4_000,
                            positions=(
                                ExchangePosition(
                                    symbol="BTCUSDT",
                                    position_side="BOTH",
                                    position_amt=D("0.1"),
                                    entry_price=D("10"),
                                    update_time_ms=4_000,
                                ),
                            ),
                        )

                class OpenOrdersSource:
                    async def get_open_orders_snapshot(self, account_id: str):
                        return ExchangeOpenOrdersSnapshot(
                            account_id=account_id,
                            sampled_at_ms=4_000,
                            orders=(),
                        )

                class SignalSource:
                    async def load_review_signals(
                        self,
                        account_id: str,
                        strategy_version: str,
                        slot_start_ms: int,
                        *,
                        managed_symbols: tuple[str, ...],
                    ):
                        return ReviewSignalSnapshot(
                            account_id=account_id,
                            strategy_version=strategy_version,
                            slot_start_ms=slot_start_ms,
                            snapshot_id="reopen-signal-snapshot",
                            sampled_at_ms=4_000,
                            signals=(
                                ReviewSignal(
                                    symbol="BTCUSDT",
                                    observation=Observation(
                                        mark_price=D("10"),
                                        admitted=True,
                                        reopen_authorized=True,
                                    ),
                                ),
                            ),
                        )

                review_source = ReconciledReviewBatchSource(
                    store=store,
                    positions=PositionSource(),
                    open_orders=OpenOrdersSource(),
                    signals=SignalSource(),
                    clock_ms=lambda: 4_000,
                )
                with self.assertRaisesRegex(ReviewBatchSourceError, "position mismatch"):
                    asyncio.run(review_source.load_review_batch("primary", "v1", 0))
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
        if any(fill.symbol != symbol or fill.client_order_id != client_order_id for fill in self.fills):
            raise AssertionError("lookup request does not match the scripted order")
        if self.order is None:
            return None
        return self.order

    async def list_order_fills(
        self,
        symbol: str,
        client_order_id: str,
    ) -> tuple[TradeFillRecord, ...]:
        if any(fill.symbol != symbol or fill.client_order_id != client_order_id for fill in self.fills):
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
        client_order_id: str,
    ) -> tuple[TradeFillRecord, ...]:
        self.fill_queries += 1
        if self.fill_queries == 1:
            raise TimeoutError("simulated delayed trade history")
        return await super().list_order_fills(symbol, client_order_id)


class _MismatchedOnceFillsGateway(ScriptedExchangeGateway):
    """Return one inconsistent fill page, then the exchange's correct records."""

    def __init__(self, scenarios, *, mismatched_fills: tuple[TradeFillRecord, ...]) -> None:
        super().__init__(scenarios)
        self._mismatched_fills = mismatched_fills
        self.fill_queries = 0

    async def list_order_fills(
        self,
        symbol: str,
        client_order_id: str,
    ) -> tuple[TradeFillRecord, ...]:
        self.fill_queries += 1
        if self.fill_queries == 1:
            return self._mismatched_fills
        return await super().list_order_fills(symbol, client_order_id)


def _seed_open_t1(store: SQLiteStateStore):
    idle = store.ensure_idle_symbol_state("primary", "BTCUSDT", created_at_ms=100)
    opened = apply_confirmed_execution(
        idle,
        ExecutionReceipt(Action.OPEN_T1, D("1"), D("10")),
    )
    store.save_symbol_state(
        "primary",
        opened,
        expected_version=idle.version,
        updated_at_ms=1_000,
    )
    return opened


if __name__ == "__main__":
    unittest.main()
