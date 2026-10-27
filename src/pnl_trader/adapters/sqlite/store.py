"""Transactional SQLite adapter for durable trading state."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Iterator

from ...application.account_models import AccountSnapshot
from ...application.execution_models import (
    IntentStatus,
    OrderIntent,
    PersistedOrderIntent,
)
from ...application.store_errors import (
    ActiveIntentConflict,
    IntentConflict,
)
from ...domain.models import (
    Action,
    BUY_ACTIONS,
    ExecutionReceipt,
    FULL_CLOSE_ACTIONS,
    InvalidExecution,
    Lifecycle,
    SymbolState,
    Tier,
    TradeFillRecord,
    ZERO,
)
from ...domain.transitions import apply_confirmed_execution, validate_execution_intent


_SCHEMA_VERSION = 21
_ACCOUNT_EQUITY_SCHEMA = """
CREATE TABLE account_equity_snapshots (
    captured_at_ms INTEGER PRIMARY KEY,
    total_margin_balance TEXT NOT NULL,
    unrealized_pnl TEXT,
    available_balance TEXT,
    position_notional TEXT
)
"""
_SCHEMA = """
CREATE TABLE symbol_states (
    symbol TEXT NOT NULL,
    tier INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 3),
    lifecycle TEXT NOT NULL,
    qty TEXT NOT NULL,
    avg_entry_price TEXT NOT NULL,
    tier_entry_qty TEXT NOT NULL,
    tier_anchor_price TEXT NOT NULL,
    completed_batches INTEGER NOT NULL CHECK (completed_batches BETWEEN 0 AND 2),
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (symbol)
);

CREATE TABLE order_intents (
    intent_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    target_cumulative_sold_qty TEXT,
    status TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    requested_qty TEXT
);

CREATE UNIQUE INDEX one_active_intent_per_symbol
ON order_intents (symbol)
WHERE status IN ('PREPARED', 'PENDING', 'UNKNOWN', 'PARTIALLY_FILLED');

CREATE TABLE trade_fills (
    symbol TEXT NOT NULL,
    exchange_trade_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    commission TEXT NOT NULL,
    commission_asset TEXT NOT NULL,
    traded_at_ms INTEGER NOT NULL,
    intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
    PRIMARY KEY (symbol, exchange_trade_id)
);

CREATE INDEX trade_fills_by_time ON trade_fills(traded_at_ms);

CREATE TABLE account_equity_snapshots (
    captured_at_ms INTEGER PRIMARY KEY,
    total_margin_balance TEXT NOT NULL,
    unrealized_pnl TEXT,
    available_balance TEXT,
    position_notional TEXT
);

"""

_ALLOWED_TRANSITIONS = {
    "PREPARED": {"CANCELED"},
    "PENDING": {"UNKNOWN", "PARTIALLY_FILLED"},
    "UNKNOWN": {"PENDING", "PARTIALLY_FILLED"},
    "PARTIALLY_FILLED": {"UNKNOWN"},
}

_TERMINAL_STATUSES = {"FILLED", "REJECTED", "CANCELED", "EXPIRED"}


class SQLiteStateStore:
    """Small durable interface for position state, review slots and order intent.

    The adapter requires a file-backed database so WAL and restart recovery have
    their intended meaning. Its connection stays on the creating thread; each
    public write is one short ``BEGIN IMMEDIATE`` transaction.
    """

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("SQLiteStateStore requires a file-backed database")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self._path,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self._initialize_schema()
            journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise RuntimeError(f"SQLite did not enable WAL mode: {journal_mode}")
            self._connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _initialize_schema(self) -> None:
        schema_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if schema_version == _SCHEMA_VERSION:
            return
        if schema_version == 19:
            with self._transaction() as connection:
                connection.execute(_ACCOUNT_EQUITY_SCHEMA)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return
        if schema_version == 20:
            with self._transaction() as connection:
                connection.execute("ALTER TABLE account_equity_snapshots ADD COLUMN unrealized_pnl TEXT")
                connection.execute("ALTER TABLE account_equity_snapshots ADD COLUMN available_balance TEXT")
                connection.execute("ALTER TABLE account_equity_snapshots ADD COLUMN position_notional TEXT")
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return
        if schema_version != 0:
            raise RuntimeError(f"unsupported database schema version: {schema_version}")
        has_tables = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if has_tables is not None:
            raise RuntimeError(
                "existing database schemas are not supported; use an empty data directory"
            )

        with self._transaction() as connection:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def load_symbol_state(self, symbol: str) -> SymbolState:
        row = self._connection.execute(
            "SELECT * FROM symbol_states WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None:
            raise LookupError(f"symbol state not found: {symbol}")
        return self._symbol_state_from_row(row)

    def list_symbol_states(self) -> tuple[SymbolState, ...]:
        rows = self._connection.execute(
            "SELECT * FROM symbol_states ORDER BY symbol"
        ).fetchall()
        return tuple(self._symbol_state_from_row(row) for row in rows)

    def record_account_equity_snapshot(
        self,
        *,
        captured_at_ms: int,
        snapshot: AccountSnapshot,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO account_equity_snapshots (
                       captured_at_ms, total_margin_balance, unrealized_pnl,
                       available_balance, position_notional
                   ) VALUES (?, ?, ?, ?, ?)""",
                (captured_at_ms, str(snapshot.equity), str(snapshot.unrealized_pnl),
                 str(snapshot.available_balance), str(snapshot.position_notional)),
            )

    def ensure_idle_symbol_state(
        self,
        symbol: str,
        *,
        created_at_ms: int,
    ) -> SymbolState:
        """Create an initial flat state once, without replacing an existing state."""

        with self._transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO symbol_states (
                    symbol, tier, lifecycle, qty,
                    avg_entry_price, tier_entry_qty, tier_anchor_price,
                    completed_batches, updated_at_ms
                ) VALUES (?, 0, 'IDLE', '0', '0', '0', '0', 0, ?)""",
                (symbol, created_at_ms),
            )
            row = connection.execute(
                "SELECT * FROM symbol_states WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            return self._symbol_state_from_row(row)

    def clear_settled_symbol_state(
        self,
        symbol: str,
        *,
        updated_at_ms: int,
    ) -> SymbolState:
        """Clear local strategy state after Binance settles a delisted contract."""

        with self._transaction() as connection:
            active_intent = connection.execute(
                """SELECT 1 FROM order_intents
                   WHERE symbol = ?
                     AND status IN ('PREPARED', 'PENDING', 'UNKNOWN', 'PARTIALLY_FILLED')
                   LIMIT 1""",
                (symbol,),
            ).fetchone()
            if active_intent is not None:
                raise RuntimeError(
                    f"cannot clear settled symbol {symbol} with an active order intent"
                )

            updated = connection.execute(
                """UPDATE symbol_states SET
                    tier = 0, lifecycle = 'IDLE', qty = '0',
                    avg_entry_price = '0', tier_entry_qty = '0',
                    tier_anchor_price = '0', completed_batches = 0,
                    updated_at_ms = ?
                WHERE symbol = ?""",
                (updated_at_ms, symbol),
            )
            if updated.rowcount != 1:
                raise LookupError(f"symbol state not found: {symbol}")
            row = connection.execute(
                "SELECT * FROM symbol_states WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            return self._symbol_state_from_row(row)

    @staticmethod
    def _symbol_state_from_row(row: sqlite3.Row) -> SymbolState:
        return SymbolState(
            symbol=row["symbol"],
            tier=Tier(row["tier"]),
            lifecycle=Lifecycle(row["lifecycle"]),
            qty=Decimal(row["qty"]),
            avg_entry_price=Decimal(row["avg_entry_price"]),
            tier_entry_qty=Decimal(row["tier_entry_qty"]),
            tier_anchor_price=Decimal(row["tier_anchor_price"]),
            completed_batches=row["completed_batches"],
        )

    def create_order_intent(
        self,
        intent: OrderIntent,
    ) -> None:
        """Reserve one symbol's execution slot; exact duplicate calls are harmless."""

        with self._transaction() as connection:
            by_id = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if by_id is not None:
                if not self._intent_matches(by_id, intent):
                    raise IntentConflict("intent_id was already used for different parameters")
                return

            state_row = connection.execute(
                "SELECT * FROM symbol_states WHERE symbol = ?",
                (intent.symbol,),
            ).fetchone()
            if state_row is None:
                raise IntentConflict("symbol state must be initialized before reserving an intent")
            try:
                validate_execution_intent(
                    self._symbol_state_from_row(state_row),
                    intent.action,
                    intent.target_cumulative_sold_qty,
                )
            except InvalidExecution as exc:
                raise IntentConflict(str(exc)) from exc

            try:
                connection.execute(
                    """INSERT INTO order_intents (
                        intent_id, symbol, action,
                        target_cumulative_sold_qty,
                        status, created_at_ms
                    ) VALUES (?, ?, ?, ?, 'PREPARED', ?)""",
                    (
                        intent.intent_id,
                        intent.symbol,
                        intent.action.value,
                        None if intent.target_cumulative_sold_qty is None else str(intent.target_cumulative_sold_qty),
                        intent.created_at_ms,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ActiveIntentConflict(
                    f"symbol {intent.symbol} already has an active intent"
                ) from exc

    @staticmethod
    def _intent_matches(row: sqlite3.Row, intent: OrderIntent) -> bool:
        return (
            row["symbol"] == intent.symbol
            and row["action"] == intent.action.value
            and row["target_cumulative_sold_qty"]
            == (None if intent.target_cumulative_sold_qty is None else str(intent.target_cumulative_sold_qty))
            and row["created_at_ms"] == intent.created_at_ms
        )

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> OrderIntent:
        return OrderIntent(
            intent_id=row["intent_id"],
            symbol=row["symbol"],
            action=Action(row["action"]),
            created_at_ms=row["created_at_ms"],
            target_cumulative_sold_qty=(
                None
                if row["target_cumulative_sold_qty"] is None
                else Decimal(row["target_cumulative_sold_qty"])
            ),
        )

    def list_active_order_intents(
        self,
        symbol: str | None = None,
    ) -> tuple[PersistedOrderIntent, ...]:
        """Load active intents and their attempts for startup recovery."""

        sql = """SELECT * FROM order_intents
                 WHERE status IN ('PREPARED', 'PENDING', 'UNKNOWN', 'PARTIALLY_FILLED')"""
        parameters: tuple[str, ...] = ()
        if symbol is not None:
            sql += " AND symbol = ?"
            parameters = (symbol,)
        sql += " ORDER BY created_at_ms, symbol, intent_id"
        rows = self._connection.execute(sql, parameters).fetchall()

        result: list[PersistedOrderIntent] = []
        for row in rows:
            result.append(
                PersistedOrderIntent(
                    intent=self._intent_from_row(row),
                    status=IntentStatus(row["status"]),
                    requested_qty=(
                        None if row["requested_qty"] is None else Decimal(row["requested_qty"])
                    ),
                )
            )
        return tuple(result)

    def list_full_closes_missing_fills(self) -> tuple[PersistedOrderIntent, ...]:
        actions = tuple(action.value for action in FULL_CLOSE_ACTIONS)
        placeholders = ", ".join("?" for _ in actions)
        rows = self._connection.execute(
            f"""SELECT * FROM order_intents AS i
                WHERE status = 'FILLED' AND action IN ({placeholders})
                  AND i.intent_id NOT IN (SELECT intent_id FROM trade_fills)
                ORDER BY created_at_ms, intent_id""",
            actions,
        ).fetchall()
        return tuple(
            PersistedOrderIntent(
                intent=self._intent_from_row(row),
                status=IntentStatus.FILLED,
                requested_qty=Decimal(row["requested_qty"]),
            )
            for row in rows
        )

    def confirm_full_close(
        self, intent_id: str, *, expected: IntentStatus, filled_qty: Decimal, updated_at_ms: int
    ) -> None:
        """Record a confirmed flat position before the trade ledger is available."""

        with self._transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None or intent["status"] != expected.value:
                raise IntentConflict("intent is missing or has changed status")
            parsed = self._intent_from_row(intent)
            if parsed.action not in FULL_CLOSE_ACTIONS or intent["requested_qty"] is None:
                raise IntentConflict("confirmed close requires a full-close order attempt")
            state_row = connection.execute(
                "SELECT * FROM symbol_states WHERE symbol = ?", (parsed.symbol,)
            ).fetchone()
            if state_row is None:
                raise IntentConflict("symbol state is missing for the execution")
            state = self._symbol_state_from_row(state_row)
            if filled_qty != Decimal(intent["requested_qty"]) or filled_qty != state.qty:
                raise IntentConflict("confirmed close does not match the entire position")
            try:
                validate_execution_intent(state, parsed.action, parsed.target_cumulative_sold_qty)
            except InvalidExecution as exc:
                raise IntentConflict(str(exc)) from exc
            connection.execute(
                """UPDATE symbol_states SET tier = 0, lifecycle = 'REOPEN_PENDING',
                       qty = '0', avg_entry_price = '0', tier_entry_qty = '0',
                       tier_anchor_price = '0', completed_batches = 0, updated_at_ms = ?
                   WHERE symbol = ?""",
                (updated_at_ms, parsed.symbol),
            )
            connection.execute(
                "UPDATE order_intents SET status = 'FILLED' WHERE intent_id = ?",
                (intent_id,),
            )

    def record_full_close_fills(
        self, intent_id: str, fills: tuple[TradeFillRecord, ...]
    ) -> None:
        """Add the actual fills for an already-confirmed full close."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None or row["status"] != "FILLED" or Action(row["action"]) not in FULL_CLOSE_ACTIONS:
                raise IntentConflict("fill record requires a confirmed full close")
            if not fills or any(fill.symbol != row["symbol"] or fill.side != "SELL" for fill in fills):
                raise IntentConflict("full-close fills do not match the order")
            if sum((fill.qty for fill in fills), ZERO) != Decimal(row["requested_qty"]):
                raise IntentConflict("full-close fills do not match the requested quantity")
            if connection.execute(
                "SELECT 1 FROM trade_fills WHERE intent_id = ? LIMIT 1", (intent_id,)
            ).fetchone() is not None:
                raise IntentConflict("full-close fills have already been recorded")
            for fill in fills:
                connection.execute(
                    """INSERT INTO trade_fills (
                           symbol, exchange_trade_id, side, price, qty, realized_pnl,
                           commission, commission_asset, traded_at_ms, intent_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (fill.symbol, fill.exchange_trade_id, fill.side, str(fill.price),
                     str(fill.qty), str(fill.realized_pnl), str(fill.commission),
                     fill.commission_asset, fill.traded_at_ms, intent_id),
                )

    def create_order_attempt(self, intent_id: str, *, requested_qty: Decimal) -> None:
        """Persist the attempt before submitting the order."""

        with self._transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None or intent["status"] != IntentStatus.PREPARED.value:
                raise RuntimeError("an order attempt requires a PREPARED intent")
            state_row = connection.execute(
                "SELECT qty, tier_entry_qty FROM symbol_states "
                "WHERE symbol = ?",
                (intent["symbol"],),
            ).fetchone()
            if state_row is None:
                raise IntentConflict("symbol state is missing for the order attempt")
            target = (
                None
                if intent["target_cumulative_sold_qty"] is None
                else Decimal(intent["target_cumulative_sold_qty"])
            )
            current_sold_qty = Decimal(state_row["tier_entry_qty"]) - Decimal(state_row["qty"])
            if target is not None and requested_qty != target - current_sold_qty:
                raise IntentConflict("close order quantity must match its remaining cumulative target")
            connection.execute(
                """UPDATE order_intents SET status = 'PENDING', requested_qty = ?
                   WHERE intent_id = ?""",
                (str(requested_qty), intent_id),
            )

    def resolve_no_fill(
        self,
        intent_id: str,
        *,
        expected: IntentStatus,
        terminal_status: IntentStatus,
    ) -> None:
        """Persist a terminal exchange result after reconciliation proves no fill."""

        if terminal_status.value not in _TERMINAL_STATUSES - {IntentStatus.FILLED.value}:
            raise RuntimeError("no-fill resolution requires a terminal no-fill status")
        if expected not in {
            IntentStatus.PENDING,
            IntentStatus.UNKNOWN,
        }:
            raise RuntimeError("no-fill resolution requires an unresolved order status")
        with self._transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None or intent["status"] != expected.value:
                raise RuntimeError("intent is missing or has changed status")
            if intent["requested_qty"] is None:
                raise RuntimeError("no-fill resolution requires the matching order attempt")
            connection.execute(
                "UPDATE order_intents SET status = ? WHERE intent_id = ?",
                (terminal_status.value, intent_id),
            )

    def commit_confirmed_execution(
        self,
        *,
        intent_id: str,
        receipt: ExecutionReceipt,
        fills: tuple[TradeFillRecord, ...],
        expected_intent_status: IntentStatus,
        terminal_status: IntentStatus,
        updated_at_ms: int,
    ) -> None:
        """Atomically apply one terminal order's fills, position and intent.

        The exchange aggregate and fills must cover the complete terminal execution.
        Active order intent and position state are checked in the same immediate
        transaction before any row is changed.
        """

        if not intent_id:
            raise ValueError("intent_id must not be empty")
        if terminal_status.value not in _TERMINAL_STATUSES - {IntentStatus.REJECTED.value}:
            raise ValueError("filled execution must end as FILLED, CANCELED or EXPIRED")
        if not fills:
            raise ValueError("positive execution requires trade fill records")
        expected_side = "BUY" if receipt.action in BUY_ACTIONS else "SELL"
        if any(fill.side != expected_side for fill in fills):
            raise IntentConflict(f"{receipt.action.value} execution requires {expected_side} fills")
        sum_qty = sum((fill.qty for fill in fills), ZERO)
        sum_quote = sum((fill.price * fill.qty for fill in fills), ZERO)
        if sum_qty != receipt.filled_qty or sum_quote != receipt.quote_qty:
            raise IntentConflict("trade records do not match the aggregate execution receipt")

        with self._transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise RuntimeError("intent is missing or has changed status")
            if intent["requested_qty"] is None:
                raise RuntimeError("execution requires the matching active order attempt")
            if intent["status"] != expected_intent_status.value:
                raise RuntimeError("intent is missing or has changed status")
            if intent["action"] != receipt.action.value:
                raise IntentConflict("execution action does not match its intent")
            intent_target = (
                None
                if intent["target_cumulative_sold_qty"] is None
                else Decimal(intent["target_cumulative_sold_qty"])
            )
            if intent_target != receipt.target_cumulative_sold_qty:
                raise IntentConflict("execution target does not match its intent")

            requested_qty = Decimal(intent["requested_qty"])
            if any(fill.symbol != intent["symbol"] for fill in fills):
                raise IntentConflict("trade fills do not belong to the intent's symbol")
            if receipt.filled_qty > requested_qty:
                raise IntentConflict("confirmed fills exceed the submitted order quantity")
            if terminal_status == IntentStatus.FILLED and receipt.filled_qty != requested_qty:
                raise IntentConflict("FILLED status requires the full submitted quantity")
            state_row = connection.execute(
                "SELECT * FROM symbol_states WHERE symbol = ?",
                (intent["symbol"],),
            ).fetchone()
            if state_row is None:
                raise IntentConflict("symbol state is missing for the execution")
            previous_state = self._symbol_state_from_row(state_row)
            try:
                next_state = apply_confirmed_execution(previous_state, receipt)
            except InvalidExecution as exc:
                raise IntentConflict(str(exc)) from exc

            try:
                for fill in fills:
                    connection.execute(
                        """INSERT INTO trade_fills (
                               symbol, exchange_trade_id, side, price, qty, realized_pnl,
                               commission, commission_asset, traded_at_ms,
                               intent_id
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            fill.symbol,
                            fill.exchange_trade_id,
                            fill.side,
                            str(fill.price),
                            str(fill.qty),
                            str(fill.realized_pnl),
                            str(fill.commission),
                            fill.commission_asset,
                            fill.traded_at_ms,
                            intent_id,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise IntentConflict(
                    "trade fill violates a ledger constraint"
                ) from exc

            state_values = (
                int(next_state.tier),
                next_state.lifecycle.value,
                str(next_state.qty),
                str(next_state.avg_entry_price),
                str(next_state.tier_entry_qty),
                str(next_state.tier_anchor_price),
                next_state.completed_batches,
                updated_at_ms,
                intent["symbol"],
            )
            connection.execute(
                """UPDATE symbol_states SET
                    tier = ?, lifecycle = ?, qty = ?,
                    avg_entry_price = ?, tier_entry_qty = ?, tier_anchor_price = ?,
                    completed_batches = ?, updated_at_ms = ?
                WHERE symbol = ?""",
                state_values,
            )

            connection.execute(
                "UPDATE order_intents SET status = ? WHERE intent_id = ?",
                (terminal_status.value, intent_id),
            )

    def transition_intent(
        self,
        intent_id: str,
        *,
        expected: IntentStatus,
        target: IntentStatus,
    ) -> None:
        if target is not expected and target.value not in _ALLOWED_TRANSITIONS.get(expected.value, set()):
            raise RuntimeError(f"transition {expected.value} -> {target.value} is not allowed")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"unknown intent {intent_id}")
            if row["status"] != expected.value:
                raise RuntimeError(
                    f"intent {intent_id} is {row['status']}, expected {expected.value}"
                )
            if target is not expected:
                connection.execute(
                    "UPDATE order_intents SET status = ? WHERE intent_id = ?",
                    (target.value, intent_id),
                )

    def get_intent_status(self, intent_id: str) -> IntentStatus | None:
        row = self._connection.execute(
            "SELECT status FROM order_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return None if row is None else IntentStatus(row["status"])
