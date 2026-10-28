from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pnl_trader.adapters.sqlite import SQLiteStateStore
from pnl_trader.application.execution_models import IntentStatus, OrderIntent
from pnl_trader.application.store_errors import ActiveIntentConflict
from pnl_trader.domain import (
    Action,
    ExecutionReceipt,
    TradeFillRecord,
)
from tests.helpers import seed_buy_execution


D = Decimal


class SQLiteStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"
        self.store = SQLiteStateStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_old_versions_are_rejected_without_changing_the_database(self) -> None:
        for version in (19, 20):
            with self.subTest(version=version):
                old_path = Path(self.temp_dir.name) / f"v{version}.sqlite3"
                with closing(sqlite3.connect(old_path)) as connection, connection:
                    connection.execute("CREATE TABLE existing_data (value TEXT)")
                    connection.execute("INSERT INTO existing_data VALUES ('preserved')")
                    connection.execute(f"PRAGMA user_version = {version}")
                with self.assertRaisesRegex(RuntimeError, f"unsupported database schema version: {version}"):
                    SQLiteStateStore(old_path)
                with closing(sqlite3.connect(old_path)) as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], version)
                    self.assertEqual(connection.execute("SELECT value FROM existing_data").fetchone()[0], "preserved")

    def test_uses_wal_and_round_trips_decimal_position_state(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

        idle = self.store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
        opened = seed_buy_execution(
            self.store,
            idle,
            action=Action.OPEN_T1,
            quantity=D("0.1"),
            price=D("10"),
            intent_id="round-trip-open",
            timestamp_ms=200,
        )

        self.assertEqual(self.store.load_symbol_state("BTCUSDT"), opened)

    def test_only_one_active_intent_can_reserve_a_symbol(self) -> None:
        first = self.buy_intent("intent-1")
        second = self.buy_intent("intent-2")
        self.seed_idle_symbol()

        self.store.create_order_intent(first)
        self.store.create_order_intent(first)
        self.assertEqual(
            len(self.store.list_active_order_intents("BTCUSDT")),
            1,
        )
        with self.assertRaises(ActiveIntentConflict):
            self.store.create_order_intent(second)

    def test_unknown_order_blocks_its_symbol_but_not_other_symbols(self) -> None:
        self.seed_idle_symbol("BTCUSDT")
        self.seed_idle_symbol("ETHUSDT")
        first = self.buy_intent(
            "intent-btc",
            symbol="BTCUSDT",
        )
        next_symbol = self.buy_intent(
            "intent-eth",
            symbol="ETHUSDT",
        )
        self.store.create_order_intent(first)
        self.store.create_order_attempt(first.intent_id, requested_qty=D("0.1"))
        self.store.transition_intent(
            first.intent_id,
            expected=IntentStatus.PENDING,
            target=IntentStatus.UNKNOWN,
        )

        self.store.close()
        self.store = SQLiteStateStore(self.db_path)
        recovered = self.store.list_active_order_intents()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, IntentStatus.UNKNOWN)
        self.assertEqual(recovered[0].requested_qty, D("0.1"))

        self.store.create_order_intent(next_symbol)

        self.store.resolve_no_fill(
            first.intent_id,
            expected=IntentStatus.UNKNOWN,
            terminal_status=IntentStatus.CANCELED,
        )

        active = self.store.list_active_order_intents()
        self.assertEqual([record.intent.symbol for record in active], ["ETHUSDT"])

    def test_partial_fill_keeps_its_symbol_active_until_terminal_execution_is_applied(self) -> None:
        self.seed_idle_symbol("BTCUSDT")
        self.seed_idle_symbol("ETHUSDT")
        first = self.buy_intent("intent-btc", symbol="BTCUSDT")
        next_symbol = self.buy_intent("intent-eth", symbol="ETHUSDT")
        self.store.create_order_intent(first)
        self.store.create_order_attempt(first.intent_id, requested_qty=D("0.1"))
        self.store.transition_intent(
            first.intent_id,
            expected=IntentStatus.PENDING,
            target=IntentStatus.PARTIALLY_FILLED,
        )

        self.store.create_order_intent(next_symbol)

        receipt = ExecutionReceipt(Action.OPEN_T1, D("0.04"), D("4"))
        fill = TradeFillRecord(
            symbol="BTCUSDT",
            exchange_trade_id="trade-btc-1",
            side="BUY",
            price=D("100"),
            qty=D("0.04"),
            realized_pnl=D("0"),
            commission=D("0.002"),
            commission_asset="USDT",
            traded_at_ms=1_002,
        )
        self.store.commit_confirmed_execution(
            intent_id=first.intent_id,
            receipt=receipt,
            fills=(fill,),
            expected_intent_status=IntentStatus.PARTIALLY_FILLED,
            terminal_status=IntentStatus.CANCELED,
            updated_at_ms=1_003,
        )

        self.assertEqual(self.store.get_intent_status(first.intent_id), IntentStatus.CANCELED)
        self.assertEqual(
            self.store.load_symbol_state("BTCUSDT").qty,
            D("0.04"),
        )
        active = self.store.list_active_order_intents()
        self.assertEqual([record.intent.symbol for record in active], ["ETHUSDT"])

    def test_intent_status_requires_an_allowed_compare_and_swap(self) -> None:
        intent = self.buy_intent("intent-1")
        self.seed_idle_symbol()
        self.store.create_order_intent(intent)
        self.store.create_order_attempt(intent.intent_id, requested_qty=D("0.1"))
        self.store.transition_intent(
            intent.intent_id,
            expected=IntentStatus.PENDING,
            target=IntentStatus.UNKNOWN,
        )

        self.assertEqual(self.store.get_intent_status(intent.intent_id), IntentStatus.UNKNOWN)
        active = self.store.list_active_order_intents()
        self.assertEqual(active[0].status, IntentStatus.UNKNOWN)
        with self.assertRaises(RuntimeError):
            self.store.transition_intent(
                intent.intent_id,
                expected=IntentStatus.PENDING,
                target=IntentStatus.FILLED,
            )

    def test_reopening_database_keeps_symbol_state(self) -> None:
        state = self.store.ensure_idle_symbol_state("BTCUSDT", created_at_ms=100)
        self.store.close()

        self.store = SQLiteStateStore(self.db_path)

        self.assertEqual(self.store.load_symbol_state("BTCUSDT"), state)

    def seed_idle_symbol(self, symbol: str = "BTCUSDT") -> None:
        self.store.ensure_idle_symbol_state(symbol, created_at_ms=100)

    @staticmethod
    def buy_intent(
        intent_id: str,
        *,
        symbol: str = "BTCUSDT",
    ) -> OrderIntent:
        return OrderIntent(
            intent_id=intent_id,
            symbol=symbol,
            action=Action.OPEN_T1,
            created_at_ms=1_000,
        )


if __name__ == "__main__":
    unittest.main()
