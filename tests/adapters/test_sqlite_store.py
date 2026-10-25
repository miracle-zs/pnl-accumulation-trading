from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest

from pnl_trader.adapters.sqlite import (
    ActiveIntentConflict,
    IntentConflict,
    IntentStatus,
    IntentTransitionError,
    OrderAttempt,
    OrderIntent,
    ReservationLimitExceeded,
    SQLiteStateStore,
    StateVersionConflict,
)
from pnl_trader.domain import (
    Action,
    ExecutionReceipt,
    SymbolState,
    TradeFillRecord,
    apply_confirmed_execution,
)


D = Decimal


class SQLiteStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"
        self.store = SQLiteStateStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_uses_wal_and_round_trips_decimal_position_state(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

        idle = SymbolState(symbol="BTCUSDT")
        self.store.save_symbol_state("primary", idle, expected_version=None, updated_at_ms=100)
        opened = apply_confirmed_execution(
            idle,
            ExecutionReceipt(Action.OPEN_T1, D("0.1"), D("10")),
        )
        self.store.save_symbol_state("primary", opened, expected_version=0, updated_at_ms=200)

        self.assertEqual(self.store.load_symbol_state("primary", "BTCUSDT"), opened)

    def test_stale_state_version_cannot_overwrite_a_newer_row(self) -> None:
        idle = SymbolState(symbol="BTCUSDT")
        self.store.save_symbol_state("primary", idle, expected_version=None, updated_at_ms=100)
        opened = apply_confirmed_execution(
            idle,
            ExecutionReceipt(Action.OPEN_T1, D("0.1"), D("10")),
        )
        self.store.save_symbol_state("primary", opened, expected_version=0, updated_at_ms=200)

        with self.assertRaises(StateVersionConflict):
            self.store.save_symbol_state("primary", opened, expected_version=0, updated_at_ms=300)

    def test_only_one_active_intent_can_reserve_a_symbol(self) -> None:
        first = self.buy_intent("intent-1", "slot-1")
        second = self.buy_intent("intent-2", "slot-2")
        self.seed_idle_symbol()

        self.assertTrue(self.store.create_order_intent(first))
        self.assertFalse(self.store.create_order_intent(first))
        with self.assertRaises(ActiveIntentConflict):
            self.store.create_order_intent(second)

    def test_account_reservation_limit_is_atomic_across_concurrent_symbols(self) -> None:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            self.store.save_symbol_state(
                "primary",
                SymbolState(symbol=symbol),
                expected_version=None,
                updated_at_ms=100,
            )
        intents = (
            self.buy_intent(
                "intent-btc",
                "slot-btc",
                symbol="BTCUSDT",
                reserved_margin=D("1.055"),
            ),
            self.buy_intent(
                "intent-eth",
                "slot-eth",
                symbol="ETHUSDT",
                reserved_margin=D("1.055"),
            ),
        )
        other_connection = SQLiteStateStore(self.db_path)
        barrier = Barrier(2)

        def reserve(store: SQLiteStateStore, intent: OrderIntent) -> str:
            barrier.wait(timeout=5)
            try:
                store.create_order_intent(
                    intent,
                    max_active_reserved_margin=D("1.5"),
                )
            except ReservationLimitExceeded:
                return "rejected"
            return "accepted"

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(reserve, self.store, intents[0]),
                    executor.submit(reserve, other_connection, intents[1]),
                )
                outcomes = [future.result(timeout=10) for future in futures]

            self.assertCountEqual(("accepted", "rejected"), outcomes)
            active = self.store.list_active_order_intents("primary")
            self.assertEqual(len(active), 1)
            self.assertLessEqual(active[0].intent.reserved_margin, D("1.5"))
        finally:
            other_connection.close()

    def test_unknown_order_keeps_reservation_until_terminal_no_fill_is_confirmed(self) -> None:
        self.seed_idle_symbol("BTCUSDT")
        self.seed_idle_symbol("ETHUSDT")
        first = self.buy_intent(
            "intent-btc",
            "slot-btc",
            symbol="BTCUSDT",
            reserved_margin=D("1.055"),
        )
        next_symbol = self.buy_intent(
            "intent-eth",
            "slot-eth",
            symbol="ETHUSDT",
            reserved_margin=D("1.055"),
        )
        self.store.create_order_intent(first, max_active_reserved_margin=D("1.5"))
        self.store.create_order_attempt(
            OrderAttempt(
                attempt_id="attempt-btc",
                intent_id=first.intent_id,
                attempt_no=1,
                client_order_id="bot-intent-btc",
                requested_qty=D("0.1"),
                sent_at_ms=1_001,
            )
        )
        self.store.transition_intent(
            first.intent_id,
            expected=IntentStatus.SUBMITTING,
            target=IntentStatus.UNKNOWN,
        )

        self.store.close()
        self.store = SQLiteStateStore(self.db_path)
        recovered = self.store.list_active_order_intents("primary")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, IntentStatus.UNKNOWN)
        self.assertIsNotNone(recovered[0].attempt)
        self.assertEqual(recovered[0].attempt.status, IntentStatus.UNKNOWN)

        with self.assertRaises(ReservationLimitExceeded):
            self.store.create_order_intent(
                next_symbol,
                max_active_reserved_margin=D("1.5"),
            )

        self.store.resolve_no_fill(
            first.intent_id,
            receipt_id="receipt-btc-no-fill",
            expected=IntentStatus.UNKNOWN,
            terminal_status=IntentStatus.CANCELED,
            resolved_at_ms=1_002,
            confirmed_filled_qty=D("0"),
            confirmed_quote_qty=D("0"),
        )

        self.assertEqual(self.store.list_active_order_intents("primary"), ())
        self.assertTrue(
            self.store.create_order_intent(
                next_symbol,
                max_active_reserved_margin=D("1.5"),
            )
        )

    def test_partial_fill_keeps_reservation_until_terminal_execution_is_applied(self) -> None:
        self.seed_idle_symbol("BTCUSDT")
        self.seed_idle_symbol("ETHUSDT")
        first = self.buy_intent(
            "intent-btc",
            "slot-btc",
            symbol="BTCUSDT",
            reserved_margin=D("1.055"),
        )
        next_symbol = self.buy_intent(
            "intent-eth",
            "slot-eth",
            symbol="ETHUSDT",
            reserved_margin=D("1.055"),
        )
        self.store.create_order_intent(first, max_active_reserved_margin=D("1.5"))
        self.store.create_order_attempt(
            OrderAttempt(
                attempt_id="attempt-btc",
                intent_id=first.intent_id,
                attempt_no=1,
                client_order_id="bot-intent-btc",
                requested_qty=D("0.1"),
                sent_at_ms=1_001,
            )
        )
        self.store.transition_intent(
            first.intent_id,
            expected=IntentStatus.SUBMITTING,
            target=IntentStatus.PARTIALLY_FILLED,
        )

        with self.assertRaises(ReservationLimitExceeded):
            self.store.create_order_intent(
                next_symbol,
                max_active_reserved_margin=D("1.5"),
            )

        receipt = ExecutionReceipt(Action.OPEN_T1, D("0.04"), D("4"))
        fill = TradeFillRecord(
            symbol="BTCUSDT",
            exchange_trade_id="trade-btc-1",
            client_order_id="bot-intent-btc",
            side="BUY",
            price=D("100"),
            qty=D("0.04"),
            realized_pnl=D("0"),
            commission=D("0.002"),
            commission_asset="USDT",
            traded_at_ms=1_002,
        )
        self.store.commit_confirmed_execution(
            receipt_id="receipt-btc-partial-cancel",
            intent_id=first.intent_id,
            receipt=receipt,
            fills=(fill,),
            expected_intent_status=IntentStatus.PARTIALLY_FILLED,
            terminal_status=IntentStatus.CANCELED,
            applied_at_ms=1_003,
        )

        self.assertEqual(self.store.get_intent_status(first.intent_id), IntentStatus.CANCELED)
        self.assertEqual(
            self.store.load_symbol_state("primary", "BTCUSDT").qty,
            D("0.04"),
        )
        self.assertTrue(
            self.store.create_order_intent(
                next_symbol,
                max_active_reserved_margin=D("1.5"),
            )
        )

    def test_business_key_cannot_be_reused_for_a_different_intent(self) -> None:
        first = self.buy_intent("intent-1", "slot-1")
        reused_key = replace(first, intent_id="intent-2", buy_notional=D("20"))
        self.seed_idle_symbol()
        self.store.create_order_intent(first)

        with self.assertRaises(IntentConflict):
            self.store.create_order_intent(reused_key)

    def test_intent_status_requires_an_allowed_compare_and_swap(self) -> None:
        intent = self.buy_intent("intent-1", "slot-1")
        self.seed_idle_symbol()
        self.store.create_order_intent(intent)
        self.store.create_order_attempt(
            OrderAttempt(
                attempt_id="attempt-1",
                intent_id=intent.intent_id,
                attempt_no=1,
                client_order_id="bot-intent-1",
                requested_qty=D("0.1"),
                sent_at_ms=1_001,
            )
        )
        self.store.transition_intent(
            intent.intent_id,
            expected=IntentStatus.SUBMITTING,
            target=IntentStatus.UNKNOWN,
        )

        self.assertEqual(self.store.get_intent_status(intent.intent_id), IntentStatus.UNKNOWN)
        active = self.store.list_active_order_intents("primary")
        self.assertEqual(active[0].attempt.status, IntentStatus.UNKNOWN)
        with self.assertRaises(IntentTransitionError):
            self.store.transition_intent(
                intent.intent_id,
                expected=IntentStatus.SUBMITTING,
                target=IntentStatus.FILLED,
            )

    def test_reopening_database_keeps_state_and_schema_version(self) -> None:
        state = SymbolState(symbol="BTCUSDT")
        self.store.save_symbol_state("primary", state, expected_version=None, updated_at_ms=100)
        self.store.close()

        self.store = SQLiteStateStore(self.db_path)

        self.assertEqual(self.store.load_symbol_state("primary", "BTCUSDT"), state)

    def test_stale_strategy_decision_cannot_reserve_an_intent(self) -> None:
        idle = SymbolState(symbol="BTCUSDT")
        self.store.save_symbol_state("primary", idle, expected_version=None, updated_at_ms=100)
        opened = apply_confirmed_execution(
            idle,
            ExecutionReceipt(Action.OPEN_T1, D("0.1"), D("10")),
        )
        self.store.save_symbol_state("primary", opened, expected_version=0, updated_at_ms=200)

        with self.assertRaises(StateVersionConflict):
            self.store.create_order_intent(self.buy_intent("stale", "slot-stale"))

    def seed_idle_symbol(self, symbol: str = "BTCUSDT") -> None:
        self.store.save_symbol_state(
            "primary",
            SymbolState(symbol=symbol),
            expected_version=None,
            updated_at_ms=100,
        )

    @staticmethod
    def buy_intent(
        intent_id: str,
        business_key: str,
        *,
        symbol: str = "BTCUSDT",
        reserved_margin: Decimal = D("1"),
    ) -> OrderIntent:
        return OrderIntent(
            intent_id=intent_id,
            account_id="primary",
            symbol=symbol,
            cycle_id=0,
            action=Action.OPEN_T1,
            business_key=business_key,
            expected_state_version=0,
            reserved_margin=reserved_margin,
            created_at_ms=1_000,
            buy_notional=D("10"),
        )


if __name__ == "__main__":
    unittest.main()
