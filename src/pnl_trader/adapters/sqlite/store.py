"""Transactional SQLite adapter for durable trading state."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterator

from ...application.execution_models import (
    IntentStatus,
    OrderAttempt,
    OrderIntent,
    PersistedOrderAttempt,
    PersistedOrderIntent,
)
from ...application.review_run_models import ReviewPriority
from ...application.store_errors import (
    ActiveIntentConflict,
    IntentConflict,
    IntentTransitionError,
    StateVersionConflict,
)
from ...domain.models import (
    Action,
    ExecutionReceipt,
    Lifecycle,
    SymbolState,
    Tier,
    TradeFillRecord,
    ZERO,
)
from ...domain.transitions import apply_confirmed_execution
from .backup import create_online_backup


_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE symbol_states (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cycle_id INTEGER NOT NULL,
    tier INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 3),
    lifecycle TEXT NOT NULL,
    qty TEXT NOT NULL,
    avg_entry_price TEXT NOT NULL,
    tier_entry_qty TEXT NOT NULL,
    tier_anchor_price TEXT NOT NULL,
    tier_sold_qty TEXT NOT NULL,
    completed_batches INTEGER NOT NULL CHECK (completed_batches BETWEEN 0 AND 2),
    version INTEGER NOT NULL CHECK (version >= 0),
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE order_intents (
    intent_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cycle_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    business_key TEXT NOT NULL,
    expected_state_version INTEGER NOT NULL,
    target_cumulative_sold_qty TEXT,
    buy_notional TEXT,
    status TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (account_id, business_key)
);

CREATE UNIQUE INDEX one_active_intent_per_symbol
ON order_intents (account_id, symbol)
WHERE status IN ('PREPARED', 'SUBMITTING', 'UNKNOWN', 'ACKNOWLEDGED', 'PARTIALLY_FILLED');

CREATE TABLE order_attempts (
    attempt_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    client_order_id TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    requested_qty TEXT NOT NULL,
    status TEXT NOT NULL,
    applied_cum_qty TEXT NOT NULL DEFAULT '0',
    cumulative_quote TEXT NOT NULL DEFAULT '0',
    sent_at_ms INTEGER,
    UNIQUE (intent_id, attempt_no)
);

CREATE TABLE execution_receipts (
    receipt_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES order_attempts(attempt_id),
    intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
    action TEXT NOT NULL,
    filled_qty TEXT NOT NULL,
    quote_qty TEXT NOT NULL,
    final_status TEXT NOT NULL,
    applied_at_ms INTEGER NOT NULL
);

CREATE TABLE trade_fills (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange_trade_id TEXT NOT NULL,
    client_order_id TEXT,
    exchange_order_id TEXT,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    commission TEXT NOT NULL,
    commission_asset TEXT NOT NULL,
    traded_at_ms INTEGER NOT NULL,
    position_side TEXT,
    margin_asset TEXT,
    receipt_id TEXT REFERENCES execution_receipts(receipt_id),
    PRIMARY KEY (account_id, symbol, exchange_trade_id)
);

CREATE INDEX trade_fills_by_receipt ON trade_fills(receipt_id);
CREATE INDEX trade_fills_by_account_time
ON trade_fills(account_id, traded_at_ms, symbol);
CREATE INDEX trade_fills_by_order
ON trade_fills(account_id, symbol, exchange_order_id);

CREATE TABLE review_rotation_cursors (
    account_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority IN (0, 1, 2)),
    symbol TEXT NOT NULL,
    PRIMARY KEY (account_id, strategy_version, priority)
);
"""

_SCHEMA_TABLES = {
    "symbol_states",
    "order_intents",
    "order_attempts",
    "execution_receipts",
    "trade_fills",
    "review_rotation_cursors",
}

_ALLOWED_TRANSITIONS = {
    "PREPARED": {"CANCELED"},
    "SUBMITTING": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN"},
    "UNKNOWN": {"ACKNOWLEDGED", "PARTIALLY_FILLED"},
    "ACKNOWLEDGED": {"PARTIALLY_FILLED", "UNKNOWN"},
    "PARTIALLY_FILLED": {"UNKNOWN"},
}

_ACTIVE_INTENT_STATUSES = (
    "PREPARED",
    "SUBMITTING",
    "UNKNOWN",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
)
_TERMINAL_STATUSES = {"FILLED", "REJECTED", "CANCELED", "EXPIRED"}


class SQLiteStateStore:
    """Small durable interface for position state, review slots and order intent.

    The adapter requires a file-backed database so WAL and restart recovery have
    their intended meaning. Each public write is one short ``BEGIN IMMEDIATE``
    transaction; callers must not perform network I/O while using a transaction.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if str(path) == ":memory:":
            raise ValueError("SQLiteStateStore requires a file-backed database")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self._path,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
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
        with self._lock:
            self._connection.close()

    def backup_to(
        self,
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Create an online, integrity-checked snapshot of the current state."""

        # Use a dedicated read-only connection so a large backup does not hold
        # the state-store lock and delay order or position writes.
        return create_online_backup(
            self._path,
            destination,
            overwrite=overwrite,
        )

    def __enter__(self) -> SQLiteStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _initialize_schema(self) -> None:
        with self._lock:
            schema_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }

        if schema_version == _SCHEMA_VERSION:
            if tables != _SCHEMA_TABLES:
                raise RuntimeError(
                    "database schema does not match this version; use an empty data directory"
                )
            return
        if schema_version != 0:
            raise RuntimeError(f"unsupported database schema version: {schema_version}")
        if tables:
            raise RuntimeError(
                "existing database schemas are not supported; use an empty data directory"
            )

        with self._transaction() as connection:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def load_symbol_state(self, account_id: str, symbol: str) -> SymbolState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM symbol_states WHERE account_id = ? AND symbol = ?",
                (account_id, symbol),
            ).fetchone()
        if row is None:
            return None
        return self._symbol_state_from_row(row)

    def list_symbol_states(self, account_id: str) -> tuple[SymbolState, ...]:
        if not account_id:
            raise ValueError("account_id must not be empty")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM symbol_states WHERE account_id = ? ORDER BY symbol",
                (account_id,),
            ).fetchall()
        return tuple(self._symbol_state_from_row(row) for row in rows)

    def ensure_idle_symbol_state(
        self,
        account_id: str,
        symbol: str,
        *,
        created_at_ms: int,
    ) -> SymbolState:
        """Create an initial flat state once, without replacing an existing state."""

        if not account_id or not symbol:
            raise ValueError("account_id and symbol must not be empty")
        if created_at_ms < 0:
            raise ValueError("created_at_ms must be non-negative")
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO symbol_states (
                    account_id, symbol, cycle_id, tier, lifecycle, qty,
                    avg_entry_price, tier_entry_qty, tier_anchor_price,
                    tier_sold_qty, completed_batches, version, updated_at_ms
                ) VALUES (?, ?, 0, 0, 'IDLE', '0', '0', '0', '0', '0', 0, 0, ?)""",
                (account_id, symbol, created_at_ms),
            )
            row = connection.execute(
                "SELECT * FROM symbol_states WHERE account_id = ? AND symbol = ?",
                (account_id, symbol),
            ).fetchone()
            if row is None:
                raise RuntimeError("symbol state was not created or found")
            return self._symbol_state_from_row(row)

    @staticmethod
    def _symbol_state_from_row(row: sqlite3.Row) -> SymbolState:
        return SymbolState(
            symbol=row["symbol"],
            cycle_id=row["cycle_id"],
            tier=Tier(row["tier"]),
            lifecycle=Lifecycle(row["lifecycle"]),
            qty=Decimal(row["qty"]),
            avg_entry_price=Decimal(row["avg_entry_price"]),
            tier_entry_qty=Decimal(row["tier_entry_qty"]),
            tier_anchor_price=Decimal(row["tier_anchor_price"]),
            tier_sold_qty=Decimal(row["tier_sold_qty"]),
            completed_batches=row["completed_batches"],
            version=row["version"],
        )

    def save_symbol_state(
        self,
        account_id: str,
        state: SymbolState,
        *,
        expected_version: int | None,
        updated_at_ms: int,
    ) -> None:
        if not account_id:
            raise ValueError("account_id must not be empty")
        if updated_at_ms < 0:
            raise ValueError("updated_at_ms must be non-negative")

        values = (
            state.cycle_id,
            int(state.tier),
            state.lifecycle.value,
            str(state.qty),
            str(state.avg_entry_price),
            str(state.tier_entry_qty),
            str(state.tier_anchor_price),
            str(state.tier_sold_qty),
            state.completed_batches,
            state.version,
            updated_at_ms,
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT version FROM symbol_states WHERE account_id = ? AND symbol = ?",
                (account_id, state.symbol),
            ).fetchone()
            active = connection.execute(
                """SELECT intent_id FROM order_intents
                   WHERE account_id = ? AND symbol = ?
                     AND status IN ('PREPARED', 'SUBMITTING', 'UNKNOWN', 'ACKNOWLEDGED', 'PARTIALLY_FILLED')
                   LIMIT 1""",
                (account_id, state.symbol),
            ).fetchone()
            if active is not None:
                raise ActiveIntentConflict(
                    f"cannot replace {state.symbol} state while intent {active['intent_id']} is active"
                )
            if expected_version is None:
                if row is not None:
                    raise StateVersionConflict("symbol state already exists")
                if state.version != 0:
                    raise StateVersionConflict("an initial symbol state must have version 0")
                connection.execute(
                    """INSERT INTO symbol_states (
                        account_id, symbol, cycle_id, tier, lifecycle, qty,
                        avg_entry_price, tier_entry_qty, tier_anchor_price,
                        tier_sold_qty, completed_batches, version, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (account_id, state.symbol, *values),
                )
                return

            if expected_version < 0 or state.version != expected_version + 1:
                raise StateVersionConflict("updated state version must be expected_version + 1")
            if row is None or row["version"] != expected_version:
                raise StateVersionConflict("stored symbol state has changed")
            cursor = connection.execute(
                """UPDATE symbol_states SET
                    cycle_id = ?, tier = ?, lifecycle = ?, qty = ?,
                    avg_entry_price = ?, tier_entry_qty = ?, tier_anchor_price = ?,
                    tier_sold_qty = ?, completed_batches = ?, version = ?, updated_at_ms = ?
                WHERE account_id = ? AND symbol = ? AND version = ?""",
                (*values, account_id, state.symbol, expected_version),
            )
            if cursor.rowcount != 1:
                raise StateVersionConflict("stored symbol state has changed")

    def load_review_rotation_cursor(
        self,
        account_id: str,
        strategy_version: str,
        priority: ReviewPriority,
    ) -> str | None:
        if not account_id or not strategy_version or not isinstance(priority, ReviewPriority):
            raise ValueError("account, strategy and review priority are required")
        with self._lock:
            row = self._connection.execute(
                """SELECT symbol FROM review_rotation_cursors
                   WHERE account_id = ? AND strategy_version = ? AND priority = ?""",
                (account_id, strategy_version, int(priority)),
            ).fetchone()
        return None if row is None else row["symbol"]

    def save_review_rotation_cursor(
        self,
        account_id: str,
        strategy_version: str,
        priority: ReviewPriority,
        symbol: str,
    ) -> None:
        if not account_id or not strategy_version or not isinstance(priority, ReviewPriority):
            raise ValueError("account, strategy and review priority are required")
        if not symbol:
            raise ValueError("symbol must not be empty")
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO review_rotation_cursors (
                       account_id, strategy_version, priority, symbol
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_id, strategy_version, priority)
                   DO UPDATE SET symbol = excluded.symbol""",
                (account_id, strategy_version, int(priority), symbol),
            )

    def create_order_intent(
        self,
        intent: OrderIntent,
    ) -> bool:
        """Reserve one symbol's execution slot; exact duplicate calls are harmless."""

        with self._transaction() as connection:
            by_id = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if by_id is not None:
                if not self._intent_matches(by_id, intent):
                    raise IntentConflict("intent_id was already used for different parameters")
                return False

            state_row = connection.execute(
                "SELECT * FROM symbol_states WHERE account_id = ? AND symbol = ?",
                (intent.account_id, intent.symbol),
            ).fetchone()
            if state_row is None:
                raise StateVersionConflict("symbol state must be initialized before reserving an intent")
            if (
                state_row["version"] != intent.expected_state_version
                or state_row["cycle_id"] != intent.cycle_id
            ):
                raise StateVersionConflict("intent was based on a stale symbol state")
            self._validate_action_for_state(
                self._symbol_state_from_row(state_row),
                intent.action,
                intent.target_cumulative_sold_qty,
            )

            by_business_key = connection.execute(
                "SELECT intent_id FROM order_intents WHERE account_id = ? AND business_key = ?",
                (intent.account_id, intent.business_key),
            ).fetchone()
            if by_business_key is not None:
                raise IntentConflict("business_key was already used by another intent")

            active = connection.execute(
                """SELECT intent_id FROM order_intents
                WHERE account_id = ? AND symbol = ?
                  AND status IN ('PREPARED', 'SUBMITTING', 'UNKNOWN', 'ACKNOWLEDGED', 'PARTIALLY_FILLED')
                LIMIT 1""",
                (intent.account_id, intent.symbol),
            ).fetchone()
            if active is not None:
                raise ActiveIntentConflict(
                    f"symbol {intent.symbol} already has active intent {active['intent_id']}"
                )

            connection.execute(
                """INSERT INTO order_intents (
                    intent_id, account_id, symbol, cycle_id, action, business_key,
                    expected_state_version, target_cumulative_sold_qty, buy_notional,
                    status, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?)""",
                (
                    intent.intent_id,
                    intent.account_id,
                    intent.symbol,
                    intent.cycle_id,
                    intent.action.value,
                    intent.business_key,
                    intent.expected_state_version,
                    None if intent.target_cumulative_sold_qty is None else str(intent.target_cumulative_sold_qty),
                    None if intent.buy_notional is None else str(intent.buy_notional),
                    intent.created_at_ms,
                ),
            )
            return True

    @staticmethod
    def _intent_matches(row: sqlite3.Row, intent: OrderIntent) -> bool:
        return (
            row["account_id"] == intent.account_id
            and row["symbol"] == intent.symbol
            and row["cycle_id"] == intent.cycle_id
            and row["action"] == intent.action.value
            and row["business_key"] == intent.business_key
            and row["expected_state_version"] == intent.expected_state_version
            and row["target_cumulative_sold_qty"]
            == (None if intent.target_cumulative_sold_qty is None else str(intent.target_cumulative_sold_qty))
            and row["buy_notional"] == (None if intent.buy_notional is None else str(intent.buy_notional))
            and row["created_at_ms"] == intent.created_at_ms
        )

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> OrderIntent:
        return OrderIntent(
            intent_id=row["intent_id"],
            account_id=row["account_id"],
            symbol=row["symbol"],
            cycle_id=row["cycle_id"],
            action=Action(row["action"]),
            business_key=row["business_key"],
            expected_state_version=row["expected_state_version"],
            created_at_ms=row["created_at_ms"],
            target_cumulative_sold_qty=(
                None
                if row["target_cumulative_sold_qty"] is None
                else Decimal(row["target_cumulative_sold_qty"])
            ),
            buy_notional=None if row["buy_notional"] is None else Decimal(row["buy_notional"]),
        )

    def list_active_order_intents(
        self,
        account_id: str,
        symbol: str | None = None,
    ) -> tuple[PersistedOrderIntent, ...]:
        """Load active intents and their attempts for startup recovery."""

        if not account_id:
            raise ValueError("account_id must not be empty")
        if symbol is not None and not symbol:
            raise ValueError("symbol must not be empty")
        sql = """SELECT i.*,
                    a.attempt_id AS a_attempt_id,
                    a.attempt_no AS a_attempt_no,
                    a.client_order_id AS a_client_order_id,
                    a.exchange_order_id AS a_exchange_order_id,
                    a.requested_qty AS a_requested_qty,
                    a.status AS a_status,
                    a.applied_cum_qty AS a_applied_cum_qty,
                    a.cumulative_quote AS a_cumulative_quote,
                    a.sent_at_ms AS a_sent_at_ms
                 FROM order_intents AS i
                 LEFT JOIN order_attempts AS a ON a.intent_id = i.intent_id
                 WHERE i.status IN ('PREPARED', 'SUBMITTING', 'UNKNOWN', 'ACKNOWLEDGED', 'PARTIALLY_FILLED')"""
        sql += " AND i.account_id = ?"
        parameters: tuple[str, ...] = (account_id,)
        if symbol is not None:
            sql += " AND i.symbol = ?"
            parameters += (symbol,)
        sql += " ORDER BY i.created_at_ms, i.symbol, i.intent_id"
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()

        result: list[PersistedOrderIntent] = []
        for row in rows:
            attempt = None
            if row["a_attempt_id"] is not None:
                attempt = PersistedOrderAttempt(
                    attempt_id=row["a_attempt_id"],
                    attempt_no=row["a_attempt_no"],
                    client_order_id=row["a_client_order_id"],
                    exchange_order_id=row["a_exchange_order_id"],
                    requested_qty=Decimal(row["a_requested_qty"]),
                    status=IntentStatus(row["a_status"]),
                    applied_cum_qty=Decimal(row["a_applied_cum_qty"]),
                    cumulative_quote=Decimal(row["a_cumulative_quote"]),
                    sent_at_ms=row["a_sent_at_ms"],
                )
            result.append(
                PersistedOrderIntent(
                    intent=self._intent_from_row(row),
                    status=IntentStatus(row["status"]),
                    attempt=attempt,
                )
            )
        return tuple(result)

    @staticmethod
    def _validate_action_for_state(
        state: SymbolState,
        action: Action,
        target_cumulative_sold_qty: Decimal | None,
    ) -> None:
        buy_prerequisites = {
            Action.OPEN_T1: (Tier.NONE, {Lifecycle.IDLE, Lifecycle.REOPEN_PENDING}),
            Action.ADD_T2: (Tier.T1, {Lifecycle.OPEN}),
            Action.ADD_T3: (Tier.T2, {Lifecycle.OPEN}),
        }
        close_tiers = {
            Action.CLOSE_T1: Tier.T1,
            Action.CLOSE_T2_BATCH1: Tier.T2,
            Action.CLOSE_T2_FULL: Tier.T2,
            Action.CLOSE_T3_TO_ONE_THIRD: Tier.T3,
            Action.CLOSE_T3_TO_TWO_THIRDS: Tier.T3,
            Action.CLOSE_T3_FULL: Tier.T3,
        }
        if action in buy_prerequisites:
            expected_tier, lifecycles = buy_prerequisites[action]
            if state.tier is not expected_tier or state.lifecycle not in lifecycles:
                raise IntentConflict(f"{action.value} is not valid for the current symbol state")
            return

        expected_tier = close_tiers.get(action)
        if expected_tier is None:
            raise IntentConflict(f"{action.value} cannot create an order intent")
        if state.tier is not expected_tier or state.lifecycle is not Lifecycle.OPEN:
            raise IntentConflict(f"{action.value} is not valid for the current symbol state")
        if target_cumulative_sold_qty is None:
            raise IntentConflict("a close intent requires its cumulative sell target")
        if not state.tier_sold_qty < target_cumulative_sold_qty <= state.tier_entry_qty:
            raise IntentConflict("close target is outside the unsold Tier quantity")
        if action in {Action.CLOSE_T1, Action.CLOSE_T2_FULL, Action.CLOSE_T3_FULL}:
            if target_cumulative_sold_qty != state.tier_entry_qty:
                raise IntentConflict("full close target must equal the Tier entry quantity")

    def create_order_attempt(self, attempt: OrderAttempt) -> bool:
        """Persist an attempt and move the intent to SUBMITTING atomically.

        The caller must send the network request only after this method commits.
        Repeating the same attempt record is idempotent and never sends an order.
        """

        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM order_attempts WHERE attempt_id = ?", (attempt.attempt_id,)
            ).fetchone()
            if existing is not None:
                if self._attempt_matches(existing, attempt):
                    return False
                raise IntentConflict("attempt_id was already used for different parameters")

            intent = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (attempt.intent_id,)
            ).fetchone()
            if intent is None or intent["status"] != IntentStatus.PREPARED.value:
                raise IntentTransitionError("an order attempt requires a PREPARED intent")
            state_row = connection.execute(
                "SELECT * FROM symbol_states WHERE account_id = ? AND symbol = ?",
                (intent["account_id"], intent["symbol"]),
            ).fetchone()
            if (
                state_row is None
                or state_row["version"] != intent["expected_state_version"]
                or state_row["cycle_id"] != intent["cycle_id"]
            ):
                raise StateVersionConflict("symbol state changed before order submission")
            state = self._symbol_state_from_row(state_row)
            action = Action(intent["action"])
            target = (
                None
                if intent["target_cumulative_sold_qty"] is None
                else Decimal(intent["target_cumulative_sold_qty"])
            )
            self._validate_action_for_state(state, action, target)
            if target is not None and attempt.requested_qty != target - state.tier_sold_qty:
                raise IntentConflict("close order quantity must match its remaining cumulative target")
            prior = connection.execute(
                "SELECT 1 FROM order_attempts WHERE intent_id = ? LIMIT 1", (attempt.intent_id,)
            ).fetchone()
            if prior is not None:
                raise IntentConflict("this intent already has a submission attempt")

            try:
                connection.execute(
                    """INSERT INTO order_attempts (
                        attempt_id, intent_id, attempt_no, client_order_id,
                        requested_qty, status, sent_at_ms
                    ) VALUES (?, ?, ?, ?, ?, 'SUBMITTING', ?)""",
                    (
                        attempt.attempt_id,
                        attempt.intent_id,
                        attempt.attempt_no,
                        attempt.client_order_id,
                        str(attempt.requested_qty),
                        attempt.sent_at_ms,
                    ),
                )
                cursor = connection.execute(
                    "UPDATE order_intents SET status = 'SUBMITTING' WHERE intent_id = ? AND status = 'PREPARED'",
                    (attempt.intent_id,),
                )
            except sqlite3.IntegrityError as exc:
                raise IntentConflict("order attempt identifiers must be unique") from exc
            if cursor.rowcount != 1:
                raise IntentTransitionError("intent changed while recording its submission attempt")
            return True

    @staticmethod
    def _attempt_matches(row: sqlite3.Row, attempt: OrderAttempt) -> bool:
        return (
            row["intent_id"] == attempt.intent_id
            and row["attempt_no"] == attempt.attempt_no
            and row["client_order_id"] == attempt.client_order_id
            and row["requested_qty"] == str(attempt.requested_qty)
            and row["sent_at_ms"] == attempt.sent_at_ms
        )


    def resolve_no_fill(
        self,
        intent_id: str,
        *,
        receipt_id: str,
        expected: IntentStatus,
        terminal_status: IntentStatus,
        resolved_at_ms: int,
        confirmed_filled_qty: Decimal,
        confirmed_quote_qty: Decimal,
        exchange_order_id: str | None = None,
    ) -> bool:
        """Persist a terminal exchange result after reconciliation proves no fill."""

        if terminal_status.value not in _TERMINAL_STATUSES - {IntentStatus.FILLED.value}:
            raise IntentTransitionError("no-fill resolution requires a terminal no-fill status")
        if expected not in {
            IntentStatus.SUBMITTING,
            IntentStatus.ACKNOWLEDGED,
            IntentStatus.UNKNOWN,
        }:
            raise IntentTransitionError("no-fill resolution requires an unresolved order status")
        if not receipt_id:
            raise ValueError("receipt_id must not be empty")
        if resolved_at_ms < 0:
            raise ValueError("resolved_at_ms must be non-negative")
        for name, value in (
            ("confirmed_filled_qty", confirmed_filled_qty),
            ("confirmed_quote_qty", confirmed_quote_qty),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
            if value != ZERO:
                raise IntentConflict("no-fill resolution requires an exchange-confirmed zero execution")

        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM execution_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if duplicate is not None:
                if (
                    duplicate["intent_id"] == intent_id
                    and duplicate["filled_qty"] == "0"
                    and duplicate["quote_qty"] == "0"
                    and duplicate["final_status"] == terminal_status.value
                ):
                    return False
                raise IntentConflict("receipt_id was already used for different order data")

            intent = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None or intent["status"] != expected.value:
                raise IntentTransitionError("intent is missing or has changed status")
            attempt = connection.execute(
                "SELECT * FROM order_attempts WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if attempt is None or attempt["status"] != expected.value:
                raise IntentTransitionError("no-fill resolution requires the matching order attempt")
            has_fill = connection.execute(
                """SELECT 1 FROM trade_fills
                WHERE client_order_id = ? LIMIT 1""",
                (attempt["client_order_id"],),
            ).fetchone()
            if has_fill is not None or Decimal(attempt["applied_cum_qty"]) != ZERO:
                raise IntentConflict("an order with fills cannot use no-fill resolution")
            if connection.execute(
                "SELECT 1 FROM execution_receipts WHERE attempt_id = ? LIMIT 1",
                (attempt["attempt_id"],),
            ).fetchone() is not None:
                raise IntentConflict("this order attempt already has an execution receipt")

            state = connection.execute(
                "SELECT version, cycle_id FROM symbol_states WHERE account_id = ? AND symbol = ?",
                (intent["account_id"], intent["symbol"]),
            ).fetchone()
            if (
                state is None
                or state["version"] != intent["expected_state_version"]
                or state["cycle_id"] != intent["cycle_id"]
            ):
                raise StateVersionConflict("symbol state changed before the no-fill result was applied")

            connection.execute(
                """INSERT INTO execution_receipts (
                    receipt_id, attempt_id, intent_id, action, filled_qty,
                    quote_qty, final_status, applied_at_ms
                ) VALUES (?, ?, ?, ?, '0', '0', ?, ?)""",
                (
                    receipt_id,
                    attempt["attempt_id"],
                    intent_id,
                    intent["action"],
                    terminal_status.value,
                    resolved_at_ms,
                ),
            )

            cursor = connection.execute(
                """UPDATE order_attempts SET status = ?, exchange_order_id = COALESCE(?, exchange_order_id),
                    applied_cum_qty = '0', cumulative_quote = '0'
                   WHERE attempt_id = ? AND status = ?""",
                (
                    terminal_status.value,
                    exchange_order_id,
                    attempt["attempt_id"],
                    expected.value,
                ),
            )
            if cursor.rowcount != 1:
                raise IntentTransitionError("order attempt changed while resolving no-fill result")
            cursor = connection.execute(
                "UPDATE order_intents SET status = ? WHERE intent_id = ? AND status = ?",
                (terminal_status.value, intent_id, expected.value),
            )
            if cursor.rowcount != 1:
                raise IntentTransitionError("intent changed while resolving no-fill result")
            return True

    def commit_confirmed_execution(
        self,
        *,
        receipt_id: str,
        intent_id: str,
        receipt: ExecutionReceipt,
        fills: tuple[TradeFillRecord, ...],
        expected_intent_status: IntentStatus,
        terminal_status: IntentStatus,
        applied_at_ms: int,
        exchange_order_id: str | None = None,
    ) -> bool:
        """Atomically apply one terminal order's fills, position and intent.

        The receipt and fills must cover the order's complete terminal execution.
        Reapplying the same receipt and fill IDs returns False without changing
        state. The symbol version and active order intent are checked in the same
        immediate transaction before any row is changed.
        """

        if not receipt_id or not intent_id:
            raise ValueError("receipt_id and intent_id must not be empty")
        if applied_at_ms < 0:
            raise ValueError("applied_at_ms must be non-negative")
        if not receipt.order_terminal or receipt.filled_qty <= ZERO or receipt.quote_qty <= ZERO:
            raise ValueError("execution commit requires a terminal order with positive fills")
        if terminal_status.value not in _TERMINAL_STATUSES - {IntentStatus.REJECTED.value}:
            raise ValueError("filled execution must end as FILLED, CANCELED or EXPIRED")
        if not fills:
            raise ValueError("positive execution requires trade fill records")
        if any(fill.client_order_id is None for fill in fills):
            raise IntentConflict("managed execution fills require a client order ID")
        if len({fill.exchange_trade_id for fill in fills}) != len(fills):
            raise IntentConflict("receipt contains duplicate exchange trade IDs")
        if len({fill.client_order_id for fill in fills}) != 1:
            raise IntentConflict("all fills in one receipt must belong to one client order")
        if any(fill.symbol != fills[0].symbol for fill in fills):
            raise IntentConflict("all fills in one receipt must belong to one symbol")
        buy_actions = {Action.OPEN_T1, Action.ADD_T2, Action.ADD_T3}
        expected_side = "BUY" if receipt.action in buy_actions else "SELL"
        if any(fill.side != expected_side for fill in fills):
            raise IntentConflict(f"{receipt.action.value} execution requires {expected_side} fills")
        sum_qty = sum((fill.qty for fill in fills), ZERO)
        sum_quote = sum((fill.price * fill.qty for fill in fills), ZERO)
        if sum_qty != receipt.filled_qty or sum_quote != receipt.quote_qty:
            raise IntentConflict("trade records do not match the aggregate execution receipt")

        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM execution_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if duplicate is not None:
                recorded_fills = tuple(
                    tuple(row[name] for name in (
                        "symbol",
                        "exchange_trade_id",
                        "client_order_id",
                        "side",
                        "price",
                        "qty",
                        "realized_pnl",
                        "commission",
                        "commission_asset",
                        "traded_at_ms",
                    ))
                    for row in connection.execute(
                        """SELECT symbol, exchange_trade_id, client_order_id, side,
                                  price, qty, realized_pnl, commission,
                                  commission_asset, traded_at_ms
                           FROM trade_fills WHERE receipt_id = ?
                           ORDER BY exchange_trade_id""",
                        (receipt_id,),
                    )
                )
                incoming_fills = tuple(
                    (
                        fill.symbol,
                        fill.exchange_trade_id,
                        fill.client_order_id,
                        fill.side,
                        str(fill.price),
                        str(fill.qty),
                        str(fill.realized_pnl),
                        str(fill.commission),
                        fill.commission_asset,
                        fill.traded_at_ms,
                    )
                    for fill in sorted(fills, key=lambda item: item.exchange_trade_id)
                )
                if (
                    duplicate["intent_id"] == intent_id
                    and duplicate["action"] == receipt.action.value
                    and duplicate["filled_qty"] == str(receipt.filled_qty)
                    and duplicate["quote_qty"] == str(receipt.quote_qty)
                    and duplicate["final_status"] == terminal_status.value
                    and recorded_fills == incoming_fills
                ):
                    return False
                raise IntentConflict("receipt_id was already used for different execution data")

            intent = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None or intent["status"] != expected_intent_status.value:
                raise IntentTransitionError("intent is missing or has changed status")
            if intent["action"] != receipt.action.value:
                raise IntentConflict("execution action does not match its intent")
            if intent["target_cumulative_sold_qty"] is not None:
                if receipt.target_cumulative_sold_qty is None or Decimal(
                    intent["target_cumulative_sold_qty"]
                ) != receipt.target_cumulative_sold_qty:
                    raise IntentConflict("execution close target does not match its intent")
            elif receipt.target_cumulative_sold_qty is not None:
                raise IntentConflict("buy execution cannot carry a close target")

            attempt = connection.execute(
                "SELECT * FROM order_attempts WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if attempt is None or attempt["status"] != expected_intent_status.value:
                raise IntentTransitionError("execution requires the matching active order attempt")
            client_order_id = fills[0].client_order_id
            if attempt["client_order_id"] != client_order_id:
                raise IntentConflict("trade fills do not belong to the intent's client order")
            if any(fill.symbol != intent["symbol"] for fill in fills):
                raise IntentConflict("trade fills do not belong to the intent's symbol")
            if receipt.filled_qty > Decimal(attempt["requested_qty"]):
                raise IntentConflict("confirmed fills exceed the submitted order quantity")
            if terminal_status == IntentStatus.FILLED and receipt.filled_qty != Decimal(
                attempt["requested_qty"]
            ):
                raise IntentConflict("FILLED status requires the full submitted quantity")
            other_receipt = connection.execute(
                "SELECT receipt_id FROM execution_receipts WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            ).fetchone()
            if other_receipt is not None:
                raise IntentConflict("this order attempt already has an execution receipt")

            state_row = connection.execute(
                "SELECT * FROM symbol_states WHERE account_id = ? AND symbol = ?",
                (intent["account_id"], intent["symbol"]),
            ).fetchone()
            if (
                state_row is None
                or state_row["version"] != intent["expected_state_version"]
                or state_row["cycle_id"] != intent["cycle_id"]
            ):
                raise StateVersionConflict("symbol state changed after the intent was reserved")
            previous_state = self._symbol_state_from_row(state_row)
            if receipt.action not in buy_actions:
                target = receipt.target_cumulative_sold_qty
                if target is None:
                    raise IntentConflict("close execution is missing its cumulative sell target")
                full_close_actions = {
                    Action.CLOSE_T1,
                    Action.CLOSE_T2_FULL,
                    Action.CLOSE_T3_FULL,
                }
                if receipt.action in full_close_actions and target != previous_state.tier_entry_qty:
                    raise IntentConflict("full close target must equal the current Tier entry quantity")
                if target <= previous_state.tier_sold_qty or target > previous_state.tier_entry_qty:
                    raise IntentConflict("close target is outside the unsold Tier quantity")
                if previous_state.tier_sold_qty + receipt.filled_qty > target:
                    raise IntentConflict("confirmed sell exceeds its persisted cumulative target")
            next_state = apply_confirmed_execution(previous_state, receipt)

            connection.execute(
                """INSERT INTO execution_receipts (
                    receipt_id, attempt_id, intent_id, action, filled_qty,
                    quote_qty, final_status, applied_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt_id,
                    attempt["attempt_id"],
                    intent_id,
                    receipt.action.value,
                    str(receipt.filled_qty),
                    str(receipt.quote_qty),
                    terminal_status.value,
                    applied_at_ms,
                ),
            )
            for fill in fills:
                existing_fill = connection.execute(
                    """SELECT * FROM trade_fills
                       WHERE account_id = ? AND symbol = ? AND exchange_trade_id = ?""",
                    (intent["account_id"], fill.symbol, fill.exchange_trade_id),
                ).fetchone()
                if existing_fill is not None:
                    if existing_fill["receipt_id"] not in (None, receipt_id):
                        raise IntentConflict(
                            "exchange trade ID is already linked to another execution receipt"
                        )
                    for field, expected in (
                        ("side", fill.side),
                        ("commission_asset", fill.commission_asset),
                    ):
                        if existing_fill[field] != expected:
                            raise IntentConflict(
                                f"recorded trade fill differs in {field}"
                            )
                    for field, expected in (
                        ("price", fill.price),
                        ("qty", fill.qty),
                        ("realized_pnl", fill.realized_pnl),
                        ("commission", fill.commission),
                    ):
                        if Decimal(existing_fill[field]) != expected:
                            raise IntentConflict(
                                f"recorded trade fill differs in {field}"
                            )
                    if existing_fill["traded_at_ms"] != fill.traded_at_ms:
                        raise IntentConflict("recorded trade fill differs in traded_at_ms")
                    for field, received in (
                        ("client_order_id", fill.client_order_id),
                        ("exchange_order_id", fill.exchange_order_id),
                        ("position_side", fill.position_side),
                        ("margin_asset", fill.margin_asset),
                    ):
                        saved = existing_fill[field]
                        if saved is not None and received is not None and saved != received:
                            raise IntentConflict(f"recorded trade fill differs in {field}")
                    connection.execute(
                        """UPDATE trade_fills SET
                               client_order_id = COALESCE(client_order_id, ?),
                               exchange_order_id = COALESCE(exchange_order_id, ?),
                               position_side = COALESCE(position_side, ?),
                               margin_asset = COALESCE(margin_asset, ?),
                               receipt_id = ?
                           WHERE account_id = ? AND symbol = ? AND exchange_trade_id = ?""",
                        (
                            fill.client_order_id,
                            fill.exchange_order_id,
                            fill.position_side,
                            fill.margin_asset,
                            receipt_id,
                            intent["account_id"],
                            fill.symbol,
                            fill.exchange_trade_id,
                        ),
                    )
                    continue
                connection.execute(
                    """INSERT INTO trade_fills (
                           account_id, symbol, exchange_trade_id, client_order_id,
                           exchange_order_id, side, price, qty, realized_pnl,
                           commission, commission_asset, traded_at_ms,
                           position_side, margin_asset, receipt_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        intent["account_id"],
                        fill.symbol,
                        fill.exchange_trade_id,
                        fill.client_order_id,
                        fill.exchange_order_id,
                        fill.side,
                        str(fill.price),
                        str(fill.qty),
                        str(fill.realized_pnl),
                        str(fill.commission),
                        fill.commission_asset,
                        fill.traded_at_ms,
                        fill.position_side,
                        fill.margin_asset,
                        receipt_id,
                    ),
                )

            state_values = (
                next_state.cycle_id,
                int(next_state.tier),
                next_state.lifecycle.value,
                str(next_state.qty),
                str(next_state.avg_entry_price),
                str(next_state.tier_entry_qty),
                str(next_state.tier_anchor_price),
                str(next_state.tier_sold_qty),
                next_state.completed_batches,
                next_state.version,
                applied_at_ms,
                intent["account_id"],
                intent["symbol"],
                intent["expected_state_version"],
            )
            cursor = connection.execute(
                """UPDATE symbol_states SET
                    cycle_id = ?, tier = ?, lifecycle = ?, qty = ?,
                    avg_entry_price = ?, tier_entry_qty = ?, tier_anchor_price = ?,
                    tier_sold_qty = ?, completed_batches = ?, version = ?, updated_at_ms = ?
                WHERE account_id = ? AND symbol = ? AND version = ?""",
                state_values,
            )
            if cursor.rowcount != 1:
                raise StateVersionConflict("symbol state changed while applying execution")

            cursor = connection.execute(
                """UPDATE order_attempts SET status = ?, exchange_order_id = COALESCE(?, exchange_order_id),
                    applied_cum_qty = ?, cumulative_quote = ? WHERE attempt_id = ? AND status = ?""",
                (
                    terminal_status.value,
                    exchange_order_id,
                    str(receipt.filled_qty),
                    str(receipt.quote_qty),
                    attempt["attempt_id"],
                    expected_intent_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise IntentTransitionError("order attempt changed while applying execution")
            cursor = connection.execute(
                "UPDATE order_intents SET status = ? WHERE intent_id = ? AND status = ?",
                (terminal_status.value, intent_id, expected_intent_status.value),
            )
            if cursor.rowcount != 1:
                raise IntentTransitionError("intent changed while applying execution")

            return True

    def transition_intent(
        self,
        intent_id: str,
        *,
        expected: IntentStatus,
        target: IntentStatus,
        exchange_order_id: str | None = None,
    ) -> None:
        if target is not expected and target.value not in _ALLOWED_TRANSITIONS.get(expected.value, set()):
            raise IntentTransitionError(f"transition {expected.value} -> {target.value} is not allowed")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise IntentTransitionError(f"unknown intent {intent_id}")
            if row["status"] != expected.value:
                raise IntentTransitionError(
                    f"intent {intent_id} is {row['status']}, expected {expected.value}"
                )
            attempt = None
            if expected is not IntentStatus.PREPARED:
                attempt = connection.execute(
                    "SELECT attempt_id, status FROM order_attempts WHERE intent_id = ?",
                    (intent_id,),
                ).fetchone()
                if attempt is None or attempt["status"] != expected.value:
                    raise IntentTransitionError(
                        "in-flight intent transition requires a matching order attempt"
                    )

            if attempt is not None:
                cursor = connection.execute(
                    """UPDATE order_attempts
                       SET status = ?, exchange_order_id = COALESCE(?, exchange_order_id)
                       WHERE attempt_id = ? AND status = ?""",
                    (target.value, exchange_order_id, attempt["attempt_id"], expected.value),
                )
                if cursor.rowcount != 1:
                    raise IntentTransitionError("order attempt changed concurrently")
            cursor = connection.execute(
                "UPDATE order_intents SET status = ? WHERE intent_id = ? AND status = ?",
                (target.value, intent_id, expected.value),
            )
            if cursor.rowcount != 1:
                raise IntentTransitionError(f"intent {intent_id} changed concurrently")

    def get_intent_status(self, intent_id: str) -> IntentStatus | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return None if row is None else IntentStatus(row["status"])
