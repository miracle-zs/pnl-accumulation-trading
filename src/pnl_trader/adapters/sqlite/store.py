"""Transactional SQLite adapter for durable trading state."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import RLock
import time
from typing import Iterator, Mapping

from ...application.execution_models import (
    IntentStatus,
    OrderAttempt,
    OrderIntent,
    PersistedOrderAttempt,
    PersistedOrderIntent,
)
from ...application.income_models import IncomeLedgerRecord, IncomeSyncCursor
from ...application.daily_report_models import (
    DailyReportInputs,
    ReportAccountSnapshot,
    TradeHistoryCoverage,
)
from ...application.notification_models import OutboxNotification
from ...application.trade_history_models import TradeHistoryCursor
from ...application.risk_models import AccountRiskSnapshot
from ...application.position_models import ExchangePositionSnapshot
from ...application.market_models import (
    ExchangeInstrument,
    InstrumentRules,
    MarketQuantityRules,
)
from ...application.universe_models import (
    AssetClassification,
    ContractDirectiveKind,
    UniverseEligibility,
    UniverseInstrument,
    UniverseSnapshot,
)
from ...application.review_run_models import (
    PersistedReviewRun,
    ReviewPriority,
    ReviewRunStatus,
)
from ...application.store_errors import (
    ActiveIntentConflict,
    IncomeLedgerConflict,
    IntentConflict,
    IntentTransitionError,
    ReservationLimitExceeded,
    ReviewRunConflict,
    StateVersionConflict,
    TradeLedgerConflict,
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


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS review_runs (
    run_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    slot_start_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    snapshot_id TEXT,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    UNIQUE (account_id, strategy_version, slot_start_ms)
);

CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    classification TEXT NOT NULL,
    eligibility TEXT NOT NULL,
    exchange_status TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    source TEXT NOT NULL,
    refreshed_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol_states (
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

CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cycle_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    business_key TEXT NOT NULL,
    expected_state_version INTEGER NOT NULL,
    target_cumulative_sold_qty TEXT,
    buy_notional TEXT,
    reserved_margin TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (account_id, business_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_intent_per_symbol
ON order_intents (account_id, symbol)
WHERE status IN ('PREPARED', 'SUBMITTING', 'UNKNOWN', 'ACKNOWLEDGED', 'PARTIALLY_FILLED');

CREATE TABLE IF NOT EXISTS order_attempts (
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

CREATE TABLE IF NOT EXISTS trade_fills (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange_trade_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    commission TEXT NOT NULL,
    commission_asset TEXT NOT NULL,
    traded_at_ms INTEGER NOT NULL,
    PRIMARY KEY (account_id, symbol, exchange_trade_id)
);

CREATE TABLE IF NOT EXISTS income_ledger (
    account_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    income_type TEXT NOT NULL,
    asset TEXT NOT NULL,
    amount TEXT NOT NULL,
    occurred_at_ms INTEGER NOT NULL,
    PRIMARY KEY (account_id, transaction_id, income_type, asset)
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    equity TEXT NOT NULL,
    wallet_balance TEXT NOT NULL,
    available_balance TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    initial_margin TEXT NOT NULL,
    maintenance_margin TEXT NOT NULL,
    fetched_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state_events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    before_version INTEGER,
    after_version INTEGER,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    event_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    next_attempt_at_ms INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel, dedupe_key)
);

CREATE TABLE IF NOT EXISTS sync_cursors (
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    cursor TEXT NOT NULL,
    reconciled_until_ms INTEGER NOT NULL,
    PRIMARY KEY (source, scope)
);
"""

_SCHEMA_V2 = """
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
ALTER TABLE trade_fills ADD COLUMN receipt_id TEXT REFERENCES execution_receipts(receipt_id);
CREATE INDEX trade_fills_by_receipt ON trade_fills(receipt_id);
"""

_SCHEMA_V3 = """
ALTER TABLE account_snapshots ADD COLUMN multi_assets_mode INTEGER NOT NULL DEFAULT 0;
ALTER TABLE account_snapshots ADD COLUMN dual_side_position INTEGER NOT NULL DEFAULT 0;
"""

_SCHEMA_V4 = """
ALTER TABLE review_runs ADD COLUMN result_counts_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE review_runs ADD COLUMN earliest_sampled_at_ms INTEGER;
ALTER TABLE review_runs ADD COLUMN latest_sampled_at_ms INTEGER;
"""

_SCHEMA_V5 = """
CREATE TABLE position_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    sampled_at_ms INTEGER NOT NULL
);
CREATE INDEX position_snapshots_by_account
ON position_snapshots(account_id, sampled_at_ms);
CREATE TABLE position_snapshot_rows (
    snapshot_id TEXT NOT NULL REFERENCES position_snapshots(snapshot_id),
    symbol TEXT NOT NULL,
    position_side TEXT NOT NULL,
    position_amt TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    update_time_ms INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, symbol, position_side)
);
"""

_SCHEMA_V6 = """
ALTER TABLE instruments ADD COLUMN base_asset TEXT NOT NULL DEFAULT '';
ALTER TABLE instruments ADD COLUMN quote_asset TEXT NOT NULL DEFAULT '';
ALTER TABLE instruments ADD COLUMN margin_asset TEXT NOT NULL DEFAULT '';
ALTER TABLE instruments ADD COLUMN contract_type TEXT NOT NULL DEFAULT '';
ALTER TABLE instruments ADD COLUMN eligibility_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE instruments ADD COLUMN classification_version TEXT NOT NULL DEFAULT '';
ALTER TABLE instruments ADD COLUMN classification_reviewed_at_ms INTEGER;
ALTER TABLE instruments ADD COLUMN directive TEXT;
ALTER TABLE instruments ADD COLUMN directive_source TEXT;
CREATE TABLE universe_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    directory_version TEXT NOT NULL,
    exchange_snapshot_id TEXT NOT NULL,
    sampled_at_ms INTEGER NOT NULL
);
CREATE INDEX universe_snapshots_by_sampled_at
ON universe_snapshots(sampled_at_ms);
CREATE TABLE universe_snapshot_instruments (
    snapshot_id TEXT NOT NULL REFERENCES universe_snapshots(snapshot_id),
    symbol TEXT NOT NULL,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    margin_asset TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    exchange_status TEXT NOT NULL,
    classification TEXT NOT NULL,
    classification_source TEXT NOT NULL,
    classification_reviewed_at_ms INTEGER,
    eligibility TEXT NOT NULL,
    eligibility_reason TEXT NOT NULL,
    directive TEXT,
    directive_source TEXT,
    filters_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, symbol)
);
CREATE INDEX universe_snapshot_instruments_by_symbol
ON universe_snapshot_instruments(symbol, snapshot_id);
"""

_SCHEMA_V7 = """
ALTER TABLE income_ledger ADD COLUMN symbol TEXT;
ALTER TABLE income_ledger ADD COLUMN info TEXT;
ALTER TABLE income_ledger ADD COLUMN trade_id TEXT;
ALTER TABLE sync_cursors ADD COLUMN coverage_from_ms INTEGER;
CREATE INDEX income_ledger_by_account_time
ON income_ledger(account_id, occurred_at_ms, income_type, asset);
"""

_SCHEMA_V8 = """
DROP INDEX trade_fills_by_receipt;
ALTER TABLE trade_fills RENAME TO trade_fills_v7;
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
INSERT INTO trade_fills (
    account_id, symbol, exchange_trade_id, client_order_id, exchange_order_id,
    side, price, qty, realized_pnl, commission, commission_asset,
    traded_at_ms, position_side, margin_asset, receipt_id
)
SELECT account_id, symbol, exchange_trade_id, client_order_id, NULL, side,
       price, qty, realized_pnl, commission, commission_asset, traded_at_ms,
       NULL, NULL, receipt_id
FROM trade_fills_v7;
DROP TABLE trade_fills_v7;
CREATE INDEX trade_fills_by_receipt ON trade_fills(receipt_id);
CREATE INDEX trade_fills_by_account_time
ON trade_fills(account_id, traded_at_ms, symbol);
CREATE INDEX trade_fills_by_order
ON trade_fills(account_id, symbol, exchange_order_id);
"""

_SCHEMA_V9 = """
CREATE TABLE daily_reports (
    account_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    period_start_ms INTEGER NOT NULL,
    period_end_ms INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    generated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (account_id, strategy_version, period_start_ms, revision)
);
CREATE INDEX daily_reports_latest
ON daily_reports(account_id, strategy_version, period_start_ms, revision DESC);
"""

_SCHEMA_V10 = """
ALTER TABLE notification_outbox ADD COLUMN event_type TEXT NOT NULL DEFAULT 'generic';
ALTER TABLE notification_outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_outbox ADD COLUMN created_at_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_outbox ADD COLUMN last_attempt_at_ms INTEGER;
ALTER TABLE notification_outbox ADD COLUMN sent_at_ms INTEGER;
ALTER TABLE notification_outbox ADD COLUMN last_error TEXT;
CREATE UNIQUE INDEX notification_outbox_event_id ON notification_outbox(event_id);
CREATE INDEX notification_outbox_due
ON notification_outbox(channel, status, next_attempt_at_ms, priority DESC, created_at_ms);
CREATE TABLE notification_alert_state (
    account_id TEXT NOT NULL,
    alert_key TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    episode INTEGER NOT NULL CHECK (episode >= 0),
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (account_id, alert_key)
);
"""

_MIGRATIONS = (
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
    (5, _SCHEMA_V5),
    (6, _SCHEMA_V6),
    (7, _SCHEMA_V7),
    (8, _SCHEMA_V8),
    (9, _SCHEMA_V9),
    (10, _SCHEMA_V10),
)

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


def _enqueue_notification(
    connection: sqlite3.Connection,
    *,
    channel: str,
    dedupe_key: str,
    event_type: str,
    payload: Mapping[str, object],
    priority: int,
    created_at_ms: int,
) -> None:
    if not channel or not dedupe_key or not event_type:
        raise ValueError("notification channel, dedupe key, and event type are required")
    event_id = "notification:" + sha256(f"{channel}\0{dedupe_key}".encode("utf-8")).hexdigest()
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """INSERT OR IGNORE INTO notification_outbox (
               event_id, channel, dedupe_key, payload_json, status,
               next_attempt_at_ms, attempts, event_type, priority, created_at_ms
           ) VALUES (?, ?, ?, ?, 'PENDING', ?, 0, ?, ?, ?)""",
        (
            event_id,
            channel,
            dedupe_key,
            payload_json,
            created_at_ms,
            event_type,
            priority,
            created_at_ms,
        ),
    )


def _enqueue_daily_report_notification(
    connection: sqlite3.Connection,
    *,
    channel: str,
    account_id: str,
    strategy_version: str,
    period_start_ms: int,
    revision: int,
    created_at_ms: int,
    payload: Mapping[str, object],
) -> None:
    event_payload = dict(payload)
    data = event_payload.get("data")
    data = dict(data) if isinstance(data, Mapping) else {}
    data["account_id"] = account_id
    data["strategy_version"] = strategy_version
    data["revision"] = revision
    event_payload["data"] = data
    _enqueue_notification(
        connection,
        channel=channel,
        dedupe_key=f"daily-report:{account_id}:{strategy_version}:{period_start_ms}",
        event_type="daily_report",
        payload=event_payload,
        priority=int(event_payload.get("priority", 0)),
        created_at_ms=created_at_ms,
    )


def _enqueue_execution_notification(
    connection: sqlite3.Connection,
    *,
    channel: str,
    account_id: str,
    receipt_id: str,
    action: Action,
    symbol: str,
    previous_state: SymbolState,
    next_state: SymbolState,
    receipt: ExecutionReceipt,
    fills: tuple[TradeFillRecord, ...],
    applied_at_ms: int,
) -> None:
    if action in {Action.ADD_T2, Action.ADD_T3}:
        tier = "T2" if action is Action.ADD_T2 else "T3"
        _enqueue_notification(
            connection,
            channel=channel,
            dedupe_key=f"execution:{receipt_id}",
            event_type="dca_warning",
            priority=10,
            created_at_ms=applied_at_ms,
            payload={
                "title": f"补仓提醒：{symbol} 进入 {tier}",
                "body": (
                    f"标的：{symbol}\n"
                    f"触发层级：{tier}\n"
                    f"本次成交数量：{receipt.filled_qty}\n"
                    f"最新持仓均价：{next_state.avg_entry_price}\n"
                    f"本层锚定成交价：{next_state.tier_anchor_price}"
                ),
                "priority": 10,
                "data": {
                    "symbol": symbol,
                    "tier": tier,
                    "filled_qty": str(receipt.filled_qty),
                    "avg_entry_price": str(next_state.avg_entry_price),
                    "tier_anchor_price": str(next_state.tier_anchor_price),
                    "cycle_id": next_state.cycle_id,
                },
            },
        )
        return

    if not action.value.startswith("CLOSE_"):
        return

    close_vwap = receipt.quote_qty / receipt.filled_qty
    return_pct = (
        close_vwap / previous_state.avg_entry_price - Decimal("1")
    ) * Decimal("100")
    realized_by_asset: Counter[str] = Counter()
    fees_by_asset: Counter[str] = Counter()
    for fill in fills:
        realized_by_asset[fill.margin_asset or "UNKNOWN"] += fill.realized_pnl
        fees_by_asset[fill.commission_asset] += fill.commission
    cash_assets = sorted(set(realized_by_asset) | set(fees_by_asset))
    cash_lines = [
        f"{asset}: 已实现盈亏 {realized_by_asset.get(asset, ZERO)} - 手续费 "
        f"{fees_by_asset.get(asset, ZERO)} = 净额 "
        f"{realized_by_asset.get(asset, ZERO) - fees_by_asset.get(asset, ZERO)}"
        for asset in cash_assets
    ]
    if not cash_lines:
        cash_lines = ["成交记录中没有可汇总的盈亏和手续费"]
    opened_at_ms = _current_cycle_opened_at_ms(
        connection,
        account_id=account_id,
        symbol=symbol,
    )
    if opened_at_ms is None:
        holding_duration = "不可用（缺少 T1 开仓审计事件）"
    else:
        duration_seconds = max(0, (applied_at_ms - opened_at_ms) // 1_000)
        days, remainder = divmod(duration_seconds, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, seconds = divmod(remainder, 60)
        holding_duration = (
            f"{days}天{hours:02d}小时{minutes:02d}分{seconds:02d}秒"
        )
    tier = f"T{int(previous_state.tier)}"
    sign = "+" if return_pct >= ZERO else ""
    body = "\n".join(
        (
            f"标的：{symbol}",
            f"触发层级：{tier}",
            f"止盈收益率：{sign}{return_pct:.2f}%（平仓成交均价相对持仓均价）",
            "落袋净现金（按资产分别核算）：",
            *cash_lines,
            f"本轮开仓历时：{holding_duration}",
        )
    )
    _enqueue_notification(
        connection,
        channel=channel,
        dedupe_key=f"execution:{receipt_id}",
        event_type="take_profit",
        priority=0,
        created_at_ms=applied_at_ms,
        payload={
            "title": f"止盈执行完成：{symbol} {tier}",
            "body": body,
            "priority": 0,
            "data": {
                "symbol": symbol,
                "tier": tier,
                "return_percent": f"{return_pct:.2f}",
                "close_vwap": str(close_vwap),
                "average_entry_price": str(previous_state.avg_entry_price),
                "net_cash_by_asset": {
                    asset: str(realized_by_asset.get(asset, ZERO) - fees_by_asset.get(asset, ZERO))
                    for asset in cash_assets
                },
                "holding_duration_seconds": (
                    None
                    if opened_at_ms is None
                    else max(0, (applied_at_ms - opened_at_ms) // 1_000)
                ),
                "cycle_id": previous_state.cycle_id,
            },
        },
    )


def _current_cycle_opened_at_ms(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
) -> int | None:
    rows = connection.execute(
        """SELECT payload_json, created_at_ms FROM state_events
           WHERE account_id = ? AND symbol = ? AND event_type = 'ORDER_EXECUTION_APPLIED'
           ORDER BY created_at_ms DESC, rowid DESC""",
        (account_id, symbol),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("action") == Action.OPEN_T1.value:
            return int(row["created_at_ms"])
    return None


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
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            self._connection.close()
            raise RuntimeError(f"SQLite did not enable WAL mode: {journal_mode}")
        self._initialize_schema()

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
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at_ms INTEGER NOT NULL
                )"""
            )
            latest = self._connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            if latest is not None and latest > _MIGRATIONS[-1][0]:
                raise RuntimeError(f"unsupported database schema version: {latest}")

        for version, schema in _MIGRATIONS:
            checksum = sha256(schema.encode("utf-8")).hexdigest()
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if existing is not None:
                    if existing["checksum"] != checksum:
                        raise RuntimeError(f"schema version {version} checksum does not match")
                    continue

                previous = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                expected_previous = version - 1 if version > 1 else None
                if previous != expected_previous:
                    raise RuntimeError(
                        f"cannot apply schema version {version} after version {previous}"
                    )
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at_ms) VALUES (?, ?, ?)",
                    (version, checksum, time.time_ns() // 1_000_000),
                )

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

    def save_position_snapshot(self, snapshot: ExchangePositionSnapshot) -> str:
        """Persist the complete normalized response used for reconciliation."""

        identity_parts = [snapshot.account_id, str(snapshot.sampled_at_ms)]
        ordered_positions = sorted(
            snapshot.positions,
            key=lambda position: (position.symbol, position.position_side),
        )
        for position in ordered_positions:
            identity_parts.extend(
                (
                    position.symbol,
                    position.position_side,
                    str(position.position_amt),
                    str(position.entry_price),
                    str(position.update_time_ms),
                )
            )
        snapshot_id = sha256("\0".join(identity_parts).encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO position_snapshots(snapshot_id, account_id, sampled_at_ms) VALUES (?, ?, ?)",
                (snapshot_id, snapshot.account_id, snapshot.sampled_at_ms),
            )
            for position in ordered_positions:
                connection.execute(
                    """INSERT OR IGNORE INTO position_snapshot_rows (
                        snapshot_id, symbol, position_side, position_amt,
                        entry_price, update_time_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        position.symbol,
                        position.position_side,
                        str(position.position_amt),
                        str(position.entry_price),
                        position.update_time_ms,
                    ),
                )
        return snapshot_id

    def save_universe_snapshot(self, snapshot: UniverseSnapshot) -> None:
        """Persist a versioned listing/classification snapshot and its current projection."""

        with self._transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO universe_snapshots (
                    snapshot_id, directory_version, exchange_snapshot_id, sampled_at_ms
                ) VALUES (?, ?, ?, ?)""",
                (
                    snapshot.snapshot_id,
                    snapshot.directory_version,
                    snapshot.exchange_snapshot_id,
                    snapshot.sampled_at_ms,
                ),
            )
            for entry in snapshot.instruments:
                instrument = entry.instrument
                rules = instrument.rules
                filters_json = json.dumps(
                    {}
                    if rules is None
                    else {
                        "min_qty": str(rules.market_quantity.min_qty),
                        "max_qty": str(rules.market_quantity.max_qty),
                        "step_size": str(rules.market_quantity.step_size),
                        "step_origin": str(rules.market_quantity.step_origin),
                        "min_market_notional": (
                            None
                            if rules.min_market_notional is None
                            else str(rules.min_market_notional)
                        ),
                        "max_market_notional": (
                            None
                            if rules.max_market_notional is None
                            else str(rules.max_market_notional)
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                directive = None if entry.directive is None else entry.directive.value
                connection.execute(
                    """INSERT OR IGNORE INTO universe_snapshot_instruments (
                        snapshot_id, symbol, base_asset, quote_asset, margin_asset,
                        contract_type, exchange_status, classification, classification_source,
                        classification_reviewed_at_ms, eligibility, eligibility_reason,
                        directive, directive_source, filters_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot.snapshot_id,
                        instrument.symbol,
                        instrument.base_asset,
                        instrument.quote_asset,
                        instrument.margin_asset,
                        instrument.contract_type,
                        instrument.status,
                        entry.classification.value,
                        entry.classification_source,
                        entry.classification_reviewed_at_ms,
                        entry.eligibility.value,
                        entry.eligibility_reason,
                        directive,
                        entry.directive_source,
                        filters_json,
                    ),
                )
                connection.execute(
                    """INSERT INTO instruments (
                        symbol, classification, eligibility, exchange_status, filters_json,
                        source, refreshed_at_ms, base_asset, quote_asset, margin_asset,
                        contract_type, eligibility_reason, classification_version,
                        classification_reviewed_at_ms, directive, directive_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        classification = excluded.classification,
                        eligibility = excluded.eligibility,
                        exchange_status = excluded.exchange_status,
                        filters_json = excluded.filters_json,
                        source = excluded.source,
                        refreshed_at_ms = excluded.refreshed_at_ms,
                        base_asset = excluded.base_asset,
                        quote_asset = excluded.quote_asset,
                        margin_asset = excluded.margin_asset,
                        contract_type = excluded.contract_type,
                        eligibility_reason = excluded.eligibility_reason,
                        classification_version = excluded.classification_version,
                        classification_reviewed_at_ms = excluded.classification_reviewed_at_ms,
                        directive = excluded.directive,
                        directive_source = excluded.directive_source""",
                    (
                        instrument.symbol,
                        entry.classification.value,
                        entry.eligibility.value,
                        instrument.status,
                        filters_json,
                        entry.classification_source,
                        snapshot.sampled_at_ms,
                        instrument.base_asset,
                        instrument.quote_asset,
                        instrument.margin_asset,
                        instrument.contract_type,
                        entry.eligibility_reason,
                        snapshot.directory_version,
                        entry.classification_reviewed_at_ms,
                        directive,
                        entry.directive_source,
                    ),
                )

            connection.execute(
                """UPDATE instruments
                   SET eligibility = 'INELIGIBLE',
                       eligibility_reason = 'not present in the latest complete exchangeInfo snapshot',
                       exchange_status = 'DELISTED',
                       classification_version = ?,
                       refreshed_at_ms = ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM universe_snapshot_instruments AS current_rows
                       WHERE current_rows.snapshot_id = ?
                         AND current_rows.symbol = instruments.symbol
                   )""",
                (snapshot.directory_version, snapshot.sampled_at_ms, snapshot.snapshot_id),
            )

    def load_latest_universe_snapshot(self) -> UniverseSnapshot | None:
        """Restore the last complete exchange/classification snapshot for outage fallback."""

        with self._lock:
            snapshot_row = self._connection.execute(
                """SELECT snapshot_id, directory_version, exchange_snapshot_id, sampled_at_ms
                   FROM universe_snapshots ORDER BY sampled_at_ms DESC LIMIT 1"""
            ).fetchone()
            if snapshot_row is None:
                return None
            rows = self._connection.execute(
                """SELECT * FROM universe_snapshot_instruments
                   WHERE snapshot_id = ? ORDER BY symbol""",
                (snapshot_row["snapshot_id"],),
            ).fetchall()

        instruments: list[UniverseInstrument] = []
        for row in rows:
            filters = json.loads(row["filters_json"])
            if not isinstance(filters, dict):
                raise RuntimeError("persisted instrument filters must be a JSON object")
            rules = None
            if filters:
                min_notional = filters.get("min_market_notional")
                max_notional = filters.get("max_market_notional")
                rules = InstrumentRules(
                    symbol=row["symbol"],
                    contract_type=row["contract_type"],
                    status=row["exchange_status"],
                    quote_asset=row["quote_asset"],
                    margin_asset=row["margin_asset"],
                    market_quantity=MarketQuantityRules(
                        min_qty=Decimal(filters["min_qty"]),
                        max_qty=Decimal(filters["max_qty"]),
                        step_size=Decimal(filters["step_size"]),
                        step_origin=Decimal(filters["step_origin"]),
                    ),
                    min_market_notional=(
                        None if min_notional is None else Decimal(min_notional)
                    ),
                    max_market_notional=(
                        None if max_notional is None else Decimal(max_notional)
                    ),
                )
            instruments.append(
                UniverseInstrument(
                    instrument=ExchangeInstrument(
                        symbol=row["symbol"],
                        base_asset=row["base_asset"],
                        quote_asset=row["quote_asset"],
                        margin_asset=row["margin_asset"],
                        contract_type=row["contract_type"],
                        status=row["exchange_status"],
                        rules=rules,
                    ),
                    classification=AssetClassification(row["classification"]),
                    eligibility=UniverseEligibility(row["eligibility"]),
                    eligibility_reason=row["eligibility_reason"],
                    classification_source=row["classification_source"],
                    classification_reviewed_at_ms=row["classification_reviewed_at_ms"],
                    directive=(
                        None
                        if row["directive"] is None
                        else ContractDirectiveKind(row["directive"])
                    ),
                    directive_source=row["directive_source"],
                )
            )

        return UniverseSnapshot(
            snapshot_id=snapshot_row["snapshot_id"],
            directory_version=snapshot_row["directory_version"],
            sampled_at_ms=snapshot_row["sampled_at_ms"],
            exchange_snapshot_id=snapshot_row["exchange_snapshot_id"],
            instruments=tuple(instruments),
        )

    def save_account_risk_snapshot(
        self,
        snapshot: AccountRiskSnapshot,
        *,
        notification_channel: str | None = None,
        max_initial_margin_utilization: Decimal = Decimal("0.70"),
    ) -> None:
        """Persist one account sample and atomically enqueue a new risk episode."""

        if (
            not isinstance(max_initial_margin_utilization, Decimal)
            or not max_initial_margin_utilization.is_finite()
            or not ZERO < max_initial_margin_utilization <= Decimal("1")
        ):
            raise ValueError("max_initial_margin_utilization must be in (0, 1]")

        identity = "\0".join(
            (
                snapshot.account_id,
                str(snapshot.fetched_at_ms),
                str(snapshot.wallet_balance),
                str(snapshot.unrealized_pnl),
                str(snapshot.available_balance),
                str(snapshot.total_initial_margin),
                str(snapshot.total_maintenance_margin),
                str(snapshot.multi_assets_mode),
                str(snapshot.dual_side_position),
            )
        )
        snapshot_id = sha256(identity.encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO account_snapshots (
                    snapshot_id, account_id, equity, wallet_balance, available_balance,
                    unrealized_pnl, initial_margin, maintenance_margin, fetched_at_ms,
                    multi_assets_mode, dual_side_position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    snapshot.account_id,
                    str(snapshot.equity),
                    str(snapshot.wallet_balance),
                    str(snapshot.available_balance),
                    str(snapshot.unrealized_pnl),
                    str(snapshot.total_initial_margin),
                    str(snapshot.total_maintenance_margin),
                    snapshot.fetched_at_ms,
                    int(snapshot.multi_assets_mode),
                    int(snapshot.dual_side_position),
                ),
            )
            if notification_channel is None:
                connection.execute(
                    """UPDATE notification_alert_state SET active = 0, updated_at_ms = ?
                       WHERE account_id = ?
                         AND alert_key GLOB 'initial_margin_utilization:*'""",
                    (snapshot.fetched_at_ms, snapshot.account_id),
                )
                return

            equity = snapshot.equity
            utilization = (
                snapshot.total_initial_margin / equity if equity > ZERO else None
            )
            breached = equity <= ZERO or utilization > max_initial_margin_utilization
            alert_key = f"initial_margin_utilization:{notification_channel}"
            connection.execute(
                """UPDATE notification_alert_state SET active = 0, updated_at_ms = ?
                   WHERE account_id = ? AND alert_key GLOB 'initial_margin_utilization:*'
                     AND alert_key != ?""",
                (snapshot.fetched_at_ms, snapshot.account_id, alert_key),
            )
            state = connection.execute(
                """SELECT active, episode FROM notification_alert_state
                   WHERE account_id = ? AND alert_key = ?""",
                (snapshot.account_id, alert_key),
            ).fetchone()
            was_active = state is not None and bool(state["active"])
            episode = 0 if state is None else int(state["episode"])
            if breached:
                if not was_active:
                    episode += 1
                    ratio_text = (
                        "不可计算（权益非正）"
                        if utilization is None
                        else f"{utilization * Decimal('100'):.2f}%"
                    )
                    limit_text = f"{max_initial_margin_utilization * Decimal('100'):.2f}%"
                    _enqueue_notification(
                        connection,
                        channel=notification_channel,
                        dedupe_key=f"risk-alert:{snapshot.account_id}:{alert_key}:{episode}",
                        event_type="risk_circuit_breaker",
                        priority=100,
                        created_at_ms=snapshot.fetched_at_ms,
                        payload={
                            "title": "紧急熔断告警：保证金占用超限",
                            "body": (
                                f"账户 {snapshot.account_id} 的初始保证金使用率为 {ratio_text}，"
                                f"阈值为 {limit_text}。系统已暂停新的开仓和补仓请求；"
                                "已有仓位的减仓操作仍可执行。\n"
                                f"权益：{snapshot.equity}；初始保证金：{snapshot.total_initial_margin}；"
                                f"可用余额：{snapshot.available_balance}。"
                            ),
                            "priority": 100,
                            "data": {
                                "account_id": snapshot.account_id,
                                "utilization": None if utilization is None else str(utilization),
                                "threshold": str(max_initial_margin_utilization),
                                "equity": str(snapshot.equity),
                                "initial_margin": str(snapshot.total_initial_margin),
                                "available_balance": str(snapshot.available_balance),
                                "episode": episode,
                            },
                        },
                    )
                connection.execute(
                    """INSERT INTO notification_alert_state (
                           account_id, alert_key, active, episode, updated_at_ms
                       ) VALUES (?, ?, 1, ?, ?)
                       ON CONFLICT(account_id, alert_key) DO UPDATE SET
                           active = 1, episode = excluded.episode,
                           updated_at_ms = excluded.updated_at_ms""",
                    (snapshot.account_id, alert_key, episode, snapshot.fetched_at_ms),
                )
            else:
                connection.execute(
                    """INSERT INTO notification_alert_state (
                           account_id, alert_key, active, episode, updated_at_ms
                       ) VALUES (?, ?, 0, ?, ?)
                       ON CONFLICT(account_id, alert_key) DO UPDATE SET
                           active = 0, updated_at_ms = excluded.updated_at_ms""",
                    (snapshot.account_id, alert_key, episode, snapshot.fetched_at_ms),
                )

    def load_due_notifications(
        self,
        channel: str,
        *,
        now_ms: int,
        limit: int = 25,
    ) -> tuple[OutboxNotification, ...]:
        if not channel or now_ms < 0 or limit <= 0:
            raise ValueError("channel, non-negative now_ms, and positive limit are required")
        with self._lock:
            rows = self._connection.execute(
                """SELECT event_id, channel, dedupe_key, event_type, payload_json,
                          priority, attempts, next_attempt_at_ms
                   FROM notification_outbox
                   WHERE channel = ? AND status = 'PENDING' AND next_attempt_at_ms <= ?
                   ORDER BY priority DESC, created_at_ms, event_id
                   LIMIT ?""",
                (channel, now_ms, limit),
            ).fetchall()
        return tuple(
            OutboxNotification(
                event_id=row["event_id"],
                channel=row["channel"],
                dedupe_key=row["dedupe_key"],
                event_type=row["event_type"],
                payload_json=row["payload_json"],
                priority=row["priority"],
                attempts=row["attempts"],
                next_attempt_at_ms=row["next_attempt_at_ms"],
            )
            for row in rows
        )

    def mark_notification_delivered(self, event_id: str, *, sent_at_ms: int) -> None:
        if not event_id or sent_at_ms < 0:
            raise ValueError("event_id and non-negative sent_at_ms are required")
        with self._transaction() as connection:
            connection.execute(
                """UPDATE notification_outbox SET status = 'SENT', sent_at_ms = ?,
                          last_error = NULL
                   WHERE event_id = ? AND status = 'PENDING'""",
                (sent_at_ms, event_id),
            )

    def reschedule_notification(
        self,
        event_id: str,
        *,
        attempted_at_ms: int,
        next_attempt_at_ms: int,
        error_code: str,
    ) -> None:
        if (
            not event_id
            or attempted_at_ms < 0
            or next_attempt_at_ms <= attempted_at_ms
            or not error_code
        ):
            raise ValueError("notification retry fields are invalid")
        with self._transaction() as connection:
            connection.execute(
                """UPDATE notification_outbox SET attempts = attempts + 1,
                          last_attempt_at_ms = ?, next_attempt_at_ms = ?, last_error = ?
                   WHERE event_id = ? AND status = 'PENDING'""",
                (attempted_at_ms, next_attempt_at_ms, error_code[:120], event_id),
            )

    def load_income_sync_cursor(self, account_id: str) -> IncomeSyncCursor | None:
        if not account_id:
            raise ValueError("account_id must not be empty")
        with self._lock:
            row = self._connection.execute(
                """SELECT cursor, reconciled_until_ms, coverage_from_ms
                   FROM sync_cursors WHERE source = 'binance_income' AND scope = ?""",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return IncomeSyncCursor(
            cursor=row["cursor"],
            reconciled_until_ms=row["reconciled_until_ms"],
            coverage_from_ms=row["coverage_from_ms"],
        )

    def record_income_page(
        self,
        account_id: str,
        records: tuple[IncomeLedgerRecord, ...],
        *,
        coverage_from_ms: int,
        reconciled_until_ms: int,
    ) -> int:
        """Atomically insert one complete income window and advance its cursor."""

        if not account_id:
            raise ValueError("account_id must not be empty")
        if (
            isinstance(coverage_from_ms, bool)
            or not isinstance(coverage_from_ms, int)
            or coverage_from_ms < 0
        ):
            raise ValueError("coverage_from_ms must be a non-negative integer")
        if (
            isinstance(reconciled_until_ms, bool)
            or not isinstance(reconciled_until_ms, int)
            or reconciled_until_ms < coverage_from_ms
        ):
            raise ValueError("reconciled_until_ms must be at or after coverage_from_ms")
        if not isinstance(records, tuple) or any(
            not isinstance(record, IncomeLedgerRecord) for record in records
        ):
            raise TypeError("records must be a tuple of IncomeLedgerRecord values")
        if any(
            record.occurred_at_ms < coverage_from_ms
            or record.occurred_at_ms > reconciled_until_ms
            for record in records
        ):
            raise ValueError("income records must fall inside the covered interval")

        inserted_count = 0
        with self._transaction() as connection:
            for record in records:
                existing = connection.execute(
                    """SELECT amount, occurred_at_ms, symbol, info, trade_id
                       FROM income_ledger
                       WHERE account_id = ? AND transaction_id = ?
                         AND income_type = ? AND asset = ?""",
                    (
                        account_id,
                        record.transaction_id,
                        record.income_type,
                        record.asset,
                    ),
                ).fetchone()
                if existing is not None:
                    if (
                        Decimal(existing["amount"]) != record.amount
                        or existing["occurred_at_ms"] != record.occurred_at_ms
                    ):
                        raise IncomeLedgerConflict(
                            "an income identity was returned with a different amount or time"
                        )
                    for field in ("symbol", "info", "trade_id"):
                        saved = existing[field]
                        received = getattr(record, field)
                        if saved is not None and received is not None and saved != received:
                            raise IncomeLedgerConflict(
                                f"an income identity was returned with different {field} metadata"
                            )
                    connection.execute(
                        """UPDATE income_ledger SET
                               symbol = COALESCE(symbol, ?),
                               info = COALESCE(info, ?),
                               trade_id = COALESCE(trade_id, ?)
                           WHERE account_id = ? AND transaction_id = ?
                             AND income_type = ? AND asset = ?""",
                        (
                            record.symbol,
                            record.info,
                            record.trade_id,
                            account_id,
                            record.transaction_id,
                            record.income_type,
                            record.asset,
                        ),
                    )
                    continue

                connection.execute(
                    """INSERT INTO income_ledger (
                           account_id, transaction_id, income_type, asset, amount,
                           occurred_at_ms, symbol, info, trade_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id,
                        record.transaction_id,
                        record.income_type,
                        record.asset,
                        str(record.amount),
                        record.occurred_at_ms,
                        record.symbol,
                        record.info,
                        record.trade_id,
                    ),
                )
                inserted_count += 1

            previous = connection.execute(
                """SELECT reconciled_until_ms, coverage_from_ms
                   FROM sync_cursors WHERE source = 'binance_income' AND scope = ?""",
                (account_id,),
            ).fetchone()
            if previous is None:
                saved_until_ms = reconciled_until_ms
                saved_coverage_from_ms = coverage_from_ms
            elif reconciled_until_ms >= previous["reconciled_until_ms"]:
                saved_until_ms = reconciled_until_ms
                saved_coverage_from_ms = coverage_from_ms
            else:
                saved_until_ms = previous["reconciled_until_ms"]
                saved_coverage_from_ms = (
                    previous["coverage_from_ms"]
                    if previous["coverage_from_ms"] is not None
                    else coverage_from_ms
                )
            connection.execute(
                """INSERT INTO sync_cursors (
                       source, scope, cursor, reconciled_until_ms, coverage_from_ms
                   ) VALUES ('binance_income', ?, ?, ?, ?)
                   ON CONFLICT(source, scope) DO UPDATE SET
                       cursor = excluded.cursor,
                       reconciled_until_ms = excluded.reconciled_until_ms,
                       coverage_from_ms = excluded.coverage_from_ms""",
                (
                    account_id,
                    str(saved_until_ms),
                    saved_until_ms,
                    saved_coverage_from_ms,
                ),
            )
        return inserted_count

    def list_income_records(
        self,
        account_id: str,
        start_time_ms: int,
        end_time_ms: int,
        *,
        income_type: str | None = None,
    ) -> tuple[IncomeLedgerRecord, ...]:
        if not account_id:
            raise ValueError("account_id must not be empty")
        if start_time_ms < 0 or end_time_ms < start_time_ms:
            raise ValueError("income query window is invalid")
        if income_type is not None and not income_type:
            raise ValueError("income_type must not be empty when supplied")
        query = """SELECT transaction_id, income_type, asset, amount,
                          occurred_at_ms, symbol, info, trade_id
                   FROM income_ledger
                   WHERE account_id = ? AND occurred_at_ms >= ? AND occurred_at_ms <= ?"""
        params: tuple[object, ...] = (account_id, start_time_ms, end_time_ms)
        if income_type is not None:
            query += " AND income_type = ?"
            params += (income_type,)
        query += " ORDER BY occurred_at_ms, income_type, transaction_id, asset"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return tuple(
            IncomeLedgerRecord(
                transaction_id=row["transaction_id"],
                income_type=row["income_type"],
                asset=row["asset"],
                amount=Decimal(row["amount"]),
                occurred_at_ms=row["occurred_at_ms"],
                symbol=row["symbol"],
                info=row["info"],
                trade_id=row["trade_id"],
            )
            for row in rows
        )

    def load_daily_report_inputs(
        self,
        account_id: str,
        strategy_version: str,
        period_start_ms: int,
        period_end_ms: int,
        generated_at_ms: int,
    ) -> DailyReportInputs:
        """Read one serialized local view of the ledgers needed by a report."""

        if not account_id or not strategy_version:
            raise ValueError("account_id and strategy_version must not be empty")
        if (
            period_start_ms < 0
            or period_end_ms <= period_start_ms
            or generated_at_ms < period_end_ms
        ):
            raise ValueError("daily report time window is invalid")
        with self._lock:
            cursor_row = self._connection.execute(
                """SELECT cursor, reconciled_until_ms, coverage_from_ms
                   FROM sync_cursors WHERE source = 'binance_income' AND scope = ?""",
                (account_id,),
            ).fetchone()
            income_cursor = (
                None
                if cursor_row is None
                else IncomeSyncCursor(
                    cursor=cursor_row["cursor"],
                    reconciled_until_ms=cursor_row["reconciled_until_ms"],
                    coverage_from_ms=cursor_row["coverage_from_ms"],
                )
            )
            input_from_ms = period_start_ms
            if income_cursor is not None and income_cursor.coverage_from_ms is not None:
                input_from_ms = min(input_from_ms, income_cursor.coverage_from_ms)
            snapshot_rows = self._connection.execute(
                """SELECT fetched_at_ms, equity, wallet_balance, available_balance,
                          unrealized_pnl, initial_margin, maintenance_margin,
                          multi_assets_mode
                   FROM account_snapshots
                   WHERE account_id = ? AND fetched_at_ms >= ? AND fetched_at_ms <= ?
                   ORDER BY fetched_at_ms""",
                (account_id, input_from_ms, generated_at_ms),
            ).fetchall()
            account_snapshots = tuple(
                ReportAccountSnapshot(
                    fetched_at_ms=row["fetched_at_ms"],
                    equity=Decimal(row["equity"]),
                    wallet_balance=Decimal(row["wallet_balance"]),
                    available_balance=Decimal(row["available_balance"]),
                    unrealized_pnl=Decimal(row["unrealized_pnl"]),
                    initial_margin=Decimal(row["initial_margin"]),
                    maintenance_margin=Decimal(row["maintenance_margin"]),
                    multi_assets_mode=bool(row["multi_assets_mode"]),
                )
                for row in snapshot_rows
            )
            income_rows = self._connection.execute(
                """SELECT transaction_id, income_type, asset, amount, occurred_at_ms,
                          symbol, info, trade_id
                   FROM income_ledger
                   WHERE account_id = ? AND occurred_at_ms >= ? AND occurred_at_ms <= ?
                   ORDER BY occurred_at_ms, income_type, transaction_id, asset""",
                (account_id, input_from_ms, generated_at_ms),
            ).fetchall()
            income_records = tuple(
                IncomeLedgerRecord(
                    transaction_id=row["transaction_id"],
                    income_type=row["income_type"],
                    asset=row["asset"],
                    amount=Decimal(row["amount"]),
                    occurred_at_ms=row["occurred_at_ms"],
                    symbol=row["symbol"],
                    info=row["info"],
                    trade_id=row["trade_id"],
                )
                for row in income_rows
            )
            fill_rows = self._connection.execute(
                """SELECT symbol, exchange_trade_id, client_order_id, side, price, qty,
                          realized_pnl, commission, commission_asset, traded_at_ms,
                          exchange_order_id, position_side, margin_asset
                   FROM trade_fills
                   WHERE account_id = ? AND traded_at_ms >= ? AND traded_at_ms < ?
                   ORDER BY traded_at_ms, symbol, exchange_trade_id""",
                (account_id, period_start_ms, period_end_ms),
            ).fetchall()
            trade_fills = tuple(
                TradeFillRecord(
                    symbol=row["symbol"],
                    exchange_trade_id=row["exchange_trade_id"],
                    client_order_id=row["client_order_id"],
                    side=row["side"],
                    price=Decimal(row["price"]),
                    qty=Decimal(row["qty"]),
                    realized_pnl=Decimal(row["realized_pnl"]),
                    commission=Decimal(row["commission"]),
                    commission_asset=row["commission_asset"],
                    traded_at_ms=row["traded_at_ms"],
                    exchange_order_id=row["exchange_order_id"],
                    position_side=row["position_side"],
                    margin_asset=row["margin_asset"],
                )
                for row in fill_rows
            )
            states = self._connection.execute(
                "SELECT * FROM symbol_states WHERE account_id = ? ORDER BY symbol",
                (account_id,),
            ).fetchall()
            symbol_states = tuple(self._symbol_state_from_row(row) for row in states)

            universe_row = self._connection.execute(
                """SELECT snapshot_id FROM universe_snapshots
                   WHERE sampled_at_ms <= ?
                   ORDER BY sampled_at_ms DESC LIMIT 1""",
                (min(period_end_ms + 120_000, generated_at_ms),),
            ).fetchone()
            trade_symbols: set[str] = set()
            if universe_row is not None:
                universe_symbols = self._connection.execute(
                    """SELECT symbol FROM universe_snapshot_instruments
                       WHERE snapshot_id = ? AND exchange_status = 'TRADING'""",
                    (universe_row["snapshot_id"],),
                ).fetchall()
                trade_symbols.update(row["symbol"] for row in universe_symbols)
            local_symbols = self._connection.execute(
                """SELECT symbol FROM symbol_states WHERE account_id = ?
                   UNION SELECT symbol FROM trade_fills WHERE account_id = ?
                   UNION SELECT symbol FROM income_ledger
                         WHERE account_id = ? AND symbol IS NOT NULL""",
                (account_id, account_id, account_id),
            ).fetchall()
            trade_symbols.update(row["symbol"] for row in local_symbols)
            ordered_trade_symbols = tuple(sorted(trade_symbols))
            cursor_rows = self._connection.execute(
                """SELECT scope, coverage_from_ms, reconciled_until_ms
                   FROM sync_cursors WHERE source = 'binance_trades'"""
            ).fetchall()
            coverage_by_scope = {row["scope"]: row for row in cursor_rows}
            coverage_values: list[TradeHistoryCoverage] = []
            for symbol in ordered_trade_symbols:
                coverage = coverage_by_scope.get(
                    self._trade_history_scope(account_id, symbol)
                )
                coverage_values.append(
                    TradeHistoryCoverage(
                        symbol=symbol,
                        coverage_from_ms=(
                            None if coverage is None else coverage["coverage_from_ms"]
                        ),
                        reconciled_until_ms=(
                            None if coverage is None else coverage["reconciled_until_ms"]
                        ),
                    )
                )
            trade_coverage = tuple(coverage_values)

            review_rows = self._connection.execute(
                """SELECT result_counts_json FROM review_runs
                   WHERE account_id = ? AND strategy_version = ?
                     AND slot_start_ms >= ? AND slot_start_ms < ?
                     AND finished_at_ms IS NOT NULL""",
                (account_id, strategy_version, period_start_ms, period_end_ms),
            ).fetchall()
            review_counts: Counter[str] = Counter()
            for row in review_rows:
                try:
                    counts = json.loads(row["result_counts_json"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("stored review run counts are invalid JSON") from exc
                if not isinstance(counts, dict):
                    raise RuntimeError("stored review run counts have an invalid shape")
                for name, count in counts.items():
                    if isinstance(name, str) and isinstance(count, int) and not isinstance(count, bool):
                        review_counts[name] += count
            active_rows = self._connection.execute(
                """SELECT status, COUNT(*) AS count FROM order_intents
                   WHERE account_id = ?
                     AND status IN ('PREPARED', 'SUBMITTING', 'UNKNOWN',
                                    'ACKNOWLEDGED', 'PARTIALLY_FILLED')
                   GROUP BY status""",
                (account_id,),
            ).fetchall()
            active_intent_counts = tuple(
                sorted((row["status"], row["count"]) for row in active_rows)
            )

        return DailyReportInputs(
            account_id=account_id,
            strategy_version=strategy_version,
            period_start_ms=period_start_ms,
            period_end_ms=period_end_ms,
            generated_at_ms=generated_at_ms,
            account_snapshots=account_snapshots,
            income_cursor=income_cursor,
            income_records=income_records,
            trade_fills=trade_fills,
            trade_symbols=ordered_trade_symbols,
            trade_history_coverage=trade_coverage,
            symbol_states=symbol_states,
            review_counts=tuple(sorted(review_counts.items())),
            active_intent_counts=active_intent_counts,
        )

    def persist_daily_report_revision(
        self,
        *,
        account_id: str,
        strategy_version: str,
        period_start_ms: int,
        period_end_ms: int,
        status: str,
        content_hash: str,
        payload_json: str,
        generated_at_ms: int,
        notification_channel: str | None = None,
        notify: bool = False,
        notification_payload_json: str | None = None,
    ) -> tuple[int, bool]:
        """Append a report revision only when its canonical content changes."""

        if not account_id or not strategy_version:
            raise ValueError("account_id and strategy_version must not be empty")
        if period_start_ms < 0 or period_end_ms <= period_start_ms or generated_at_ms < 0:
            raise ValueError("daily report revision time fields are invalid")
        if generated_at_ms < period_end_ms:
            raise ValueError("daily report cannot be generated before its period ends")
        if status not in {"COMPLETE_WITHIN_KNOWN_SCOPE", "PROVISIONAL"}:
            raise ValueError("daily report status is invalid")
        if len(content_hash) != 64 or not payload_json:
            raise ValueError("daily report hash and payload are required")
        if sha256(payload_json.encode("utf-8")).hexdigest() != content_hash:
            raise ValueError("daily report hash does not match its payload")
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("daily report payload must be valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") != status:
            raise ValueError("daily report payload status does not match its revision")
        notification_payload: dict[str, object] | None = None
        if notification_payload_json is not None:
            try:
                decoded_notification = json.loads(notification_payload_json)
            except (TypeError, ValueError) as exc:
                raise ValueError("daily report notification payload must be valid JSON") from exc
            if not isinstance(decoded_notification, dict):
                raise ValueError("daily report notification payload must be a JSON object")
            notification_payload = decoded_notification
        with self._transaction() as connection:
            previous = connection.execute(
                """SELECT revision, content_hash FROM daily_reports
                   WHERE account_id = ? AND strategy_version = ? AND period_start_ms = ?
                   ORDER BY revision DESC LIMIT 1""",
                (account_id, strategy_version, period_start_ms),
            ).fetchone()
            if previous is not None and previous["content_hash"] == content_hash:
                if notify and notification_channel and notification_payload is not None:
                    _enqueue_daily_report_notification(
                        connection,
                        channel=notification_channel,
                        account_id=account_id,
                        strategy_version=strategy_version,
                        period_start_ms=period_start_ms,
                        revision=int(previous["revision"]),
                        created_at_ms=generated_at_ms,
                        payload=notification_payload,
                    )
                return previous["revision"], False
            revision = 1 if previous is None else previous["revision"] + 1
            connection.execute(
                """INSERT INTO daily_reports (
                       account_id, strategy_version, period_start_ms, period_end_ms,
                       revision, status, content_hash, payload_json, generated_at_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_id,
                    strategy_version,
                    period_start_ms,
                    period_end_ms,
                    revision,
                    status,
                    content_hash,
                    payload_json,
                    generated_at_ms,
                ),
            )
            if notify and notification_channel and notification_payload is not None:
                _enqueue_daily_report_notification(
                    connection,
                    channel=notification_channel,
                    account_id=account_id,
                    strategy_version=strategy_version,
                    period_start_ms=period_start_ms,
                    revision=revision,
                    created_at_ms=generated_at_ms,
                    payload=notification_payload,
                )
            return revision, True

    def load_latest_daily_report(
        self,
        account_id: str,
        strategy_version: str,
        *,
        period_start_ms: int | None = None,
    ) -> dict[str, object] | None:
        """Return the latest stored report, optionally for one report period."""

        if not account_id or not strategy_version:
            raise ValueError("account_id and strategy_version must not be empty")
        if period_start_ms is not None and period_start_ms < 0:
            raise ValueError("period_start_ms must be non-negative")
        query = """SELECT period_start_ms, period_end_ms, revision, status,
                          content_hash, payload_json, generated_at_ms
                   FROM daily_reports
                   WHERE account_id = ? AND strategy_version = ?"""
        parameters: tuple[object, ...] = (account_id, strategy_version)
        if period_start_ms is not None:
            query += " AND period_start_ms = ?"
            parameters += (period_start_ms,)
        query += " ORDER BY period_start_ms DESC, revision DESC LIMIT 1"
        with self._lock:
            row = self._connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("stored daily report payload is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("stored daily report payload has an invalid shape")
        return {
            "account_id": account_id,
            "strategy_version": strategy_version,
            "period_start_ms": row["period_start_ms"],
            "period_end_ms": row["period_end_ms"],
            "revision": row["revision"],
            "status": row["status"],
            "content_hash": row["content_hash"],
            "generated_at_ms": row["generated_at_ms"],
            "payload": payload,
        }

    @staticmethod
    def _trade_history_scope(account_id: str, symbol: str) -> str:
        return f"{len(account_id)}:{account_id}:{symbol}"

    def load_trade_history_cursor(
        self,
        account_id: str,
        symbol: str,
    ) -> TradeHistoryCursor | None:
        if not account_id or not symbol:
            raise ValueError("account_id and symbol must not be empty")
        with self._lock:
            row = self._connection.execute(
                """SELECT cursor, reconciled_until_ms, coverage_from_ms
                   FROM sync_cursors WHERE source = 'binance_trades' AND scope = ?""",
                (self._trade_history_scope(account_id, symbol),),
            ).fetchone()
        if row is None:
            return None
        return TradeHistoryCursor(
            cursor=row["cursor"],
            reconciled_until_ms=row["reconciled_until_ms"],
            coverage_from_ms=row["coverage_from_ms"],
        )

    def load_trade_history_rotation_cursor(self, account_id: str) -> str | None:
        if not account_id:
            raise ValueError("account_id must not be empty")
        with self._lock:
            row = self._connection.execute(
                """SELECT cursor FROM sync_cursors
                   WHERE source = 'binance_trade_rotation' AND scope = ?""",
                (account_id,),
            ).fetchone()
        return None if row is None else row["cursor"]

    def list_trade_history_symbols(self, account_id: str) -> tuple[str, ...]:
        """Return symbols already observed in local strategy or account ledgers."""

        if not account_id:
            raise ValueError("account_id must not be empty")
        with self._lock:
            rows = self._connection.execute(
                """SELECT symbol FROM symbol_states WHERE account_id = ?
                   UNION SELECT symbol FROM trade_fills WHERE account_id = ?
                   UNION SELECT symbol FROM income_ledger
                         WHERE account_id = ? AND symbol IS NOT NULL
                   ORDER BY symbol""",
                (account_id, account_id, account_id),
            ).fetchall()
        return tuple(row["symbol"] for row in rows)

    def save_trade_history_rotation_cursor(
        self,
        account_id: str,
        next_symbol: str,
        *,
        updated_at_ms: int,
    ) -> None:
        if not account_id or not next_symbol:
            raise ValueError("account_id and next_symbol must not be empty")
        if (
            isinstance(updated_at_ms, bool)
            or not isinstance(updated_at_ms, int)
            or updated_at_ms < 0
        ):
            raise ValueError("updated_at_ms must be a non-negative integer")
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO sync_cursors (
                       source, scope, cursor, reconciled_until_ms, coverage_from_ms
                   ) VALUES ('binance_trade_rotation', ?, ?, ?, NULL)
                   ON CONFLICT(source, scope) DO UPDATE SET
                       cursor = excluded.cursor,
                       reconciled_until_ms = excluded.reconciled_until_ms""",
                (account_id, next_symbol, updated_at_ms),
            )

    def record_trade_history_page(
        self,
        account_id: str,
        symbol: str,
        records: tuple[TradeFillRecord, ...],
        *,
        coverage_from_ms: int,
        reconciled_until_ms: int,
        next_symbol: str,
        updated_at_ms: int,
    ) -> int:
        """Atomically upsert a complete symbol window and both sync cursors."""

        if not account_id or not symbol or not next_symbol:
            raise ValueError("account_id, symbol, and next_symbol must not be empty")
        for name, value in (
            ("coverage_from_ms", coverage_from_ms),
            ("reconciled_until_ms", reconciled_until_ms),
            ("updated_at_ms", updated_at_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if reconciled_until_ms < coverage_from_ms:
            raise ValueError("reconciled_until_ms must be at or after coverage_from_ms")
        if not isinstance(records, tuple) or any(
            not isinstance(record, TradeFillRecord) for record in records
        ):
            raise TypeError("records must be a tuple of TradeFillRecord values")
        if any(
            record.symbol != symbol
            or record.traded_at_ms < coverage_from_ms
            or record.traded_at_ms > reconciled_until_ms
            for record in records
        ):
            raise ValueError("trade fills must belong to the symbol and covered interval")
        if len({record.exchange_trade_id for record in records}) != len(records):
            raise TradeLedgerConflict("a complete history window contains duplicate trade IDs")

        inserted_count = 0
        with self._transaction() as connection:
            for fill in records:
                existing = connection.execute(
                    """SELECT * FROM trade_fills
                       WHERE account_id = ? AND symbol = ? AND exchange_trade_id = ?""",
                    (account_id, symbol, fill.exchange_trade_id),
                ).fetchone()
                managed_client_order_id = None
                if fill.exchange_order_id is not None:
                    managed = connection.execute(
                        """SELECT oa.client_order_id
                           FROM order_attempts oa
                           JOIN order_intents oi ON oi.intent_id = oa.intent_id
                           WHERE oi.account_id = ? AND oi.symbol = ?
                             AND oa.exchange_order_id = ?""",
                        (account_id, symbol, fill.exchange_order_id),
                    ).fetchone()
                    if managed is not None:
                        managed_client_order_id = managed["client_order_id"]

                if existing is not None:
                    for field, expected in (
                        ("side", fill.side),
                        ("commission_asset", fill.commission_asset),
                    ):
                        if existing[field] != expected:
                            raise TradeLedgerConflict(
                                f"an exchange trade identity changed its {field}"
                            )
                    for field, expected in (
                        ("price", fill.price),
                        ("qty", fill.qty),
                        ("realized_pnl", fill.realized_pnl),
                        ("commission", fill.commission),
                    ):
                        if Decimal(existing[field]) != expected:
                            raise TradeLedgerConflict(
                                f"an exchange trade identity changed its {field}"
                            )
                    if existing["traded_at_ms"] != fill.traded_at_ms:
                        raise TradeLedgerConflict("an exchange trade identity changed its time")
                    for field, received in (
                        ("client_order_id", fill.client_order_id or managed_client_order_id),
                        ("exchange_order_id", fill.exchange_order_id),
                        ("position_side", fill.position_side),
                        ("margin_asset", fill.margin_asset),
                    ):
                        saved = existing[field]
                        if saved is not None and received is not None and saved != received:
                            raise TradeLedgerConflict(
                                f"an exchange trade identity changed its {field}"
                            )
                    client_order_id = (
                        existing["client_order_id"]
                        or fill.client_order_id
                        or managed_client_order_id
                    )
                    if (
                        managed_client_order_id is not None
                        and client_order_id != managed_client_order_id
                    ):
                        raise TradeLedgerConflict(
                            "exchange order ID maps to a different managed client order"
                        )
                    connection.execute(
                        """UPDATE trade_fills SET
                               client_order_id = ?,
                               exchange_order_id = COALESCE(exchange_order_id, ?),
                               position_side = COALESCE(position_side, ?),
                               margin_asset = COALESCE(margin_asset, ?)
                           WHERE account_id = ? AND symbol = ? AND exchange_trade_id = ?""",
                        (
                            client_order_id,
                            fill.exchange_order_id,
                            fill.position_side,
                            fill.margin_asset,
                            account_id,
                            symbol,
                            fill.exchange_trade_id,
                        ),
                    )
                    continue

                client_order_id = fill.client_order_id or managed_client_order_id
                connection.execute(
                    """INSERT INTO trade_fills (
                           account_id, symbol, exchange_trade_id, client_order_id,
                           exchange_order_id, side, price, qty, realized_pnl,
                           commission, commission_asset, traded_at_ms, position_side,
                           margin_asset
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id,
                        fill.symbol,
                        fill.exchange_trade_id,
                        client_order_id,
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
                    ),
                )
                inserted_count += 1

            scope = self._trade_history_scope(account_id, symbol)
            previous = connection.execute(
                """SELECT reconciled_until_ms, coverage_from_ms FROM sync_cursors
                   WHERE source = 'binance_trades' AND scope = ?""",
                (scope,),
            ).fetchone()
            if previous is None or previous["reconciled_until_ms"] < coverage_from_ms:
                saved_until_ms = reconciled_until_ms
                saved_coverage_from_ms = coverage_from_ms
            else:
                saved_until_ms = max(previous["reconciled_until_ms"], reconciled_until_ms)
                saved_coverage_from_ms = (
                    previous["coverage_from_ms"]
                    if previous["coverage_from_ms"] is not None
                    else coverage_from_ms
                )
            connection.execute(
                """INSERT INTO sync_cursors (
                       source, scope, cursor, reconciled_until_ms, coverage_from_ms
                   ) VALUES ('binance_trades', ?, ?, ?, ?)
                   ON CONFLICT(source, scope) DO UPDATE SET
                       cursor = excluded.cursor,
                       reconciled_until_ms = excluded.reconciled_until_ms,
                       coverage_from_ms = excluded.coverage_from_ms""",
                (
                    scope,
                    str(saved_until_ms),
                    saved_until_ms,
                    saved_coverage_from_ms,
                ),
            )
            connection.execute(
                """INSERT INTO sync_cursors (
                       source, scope, cursor, reconciled_until_ms, coverage_from_ms
                   ) VALUES ('binance_trade_rotation', ?, ?, ?, NULL)
                   ON CONFLICT(source, scope) DO UPDATE SET
                       cursor = excluded.cursor,
                       reconciled_until_ms = excluded.reconciled_until_ms""",
                (account_id, next_symbol, updated_at_ms),
            )
        return inserted_count

    def trade_history_window_is_covered(
        self,
        account_id: str,
        symbols: tuple[str, ...],
        start_time_ms: int,
        end_time_ms: int,
    ) -> bool:
        if not account_id:
            raise ValueError("account_id must not be empty")
        if start_time_ms < 0 or end_time_ms < start_time_ms:
            raise ValueError("trade history coverage window is invalid")
        with self._lock:
            for symbol in symbols:
                row = self._connection.execute(
                    """SELECT reconciled_until_ms, coverage_from_ms FROM sync_cursors
                       WHERE source = 'binance_trades' AND scope = ?""",
                    (self._trade_history_scope(account_id, symbol),),
                ).fetchone()
                if (
                    row is None
                    or row["coverage_from_ms"] is None
                    or row["coverage_from_ms"] > start_time_ms
                    or row["reconciled_until_ms"] < end_time_ms
                ):
                    return False
        return True

    def trade_history_watermark(
        self,
        account_id: str,
        symbols: tuple[str, ...],
        *,
        default_ms: int,
    ) -> int:
        if not account_id:
            raise ValueError("account_id must not be empty")
        values: list[int] = []
        with self._lock:
            for symbol in symbols:
                row = self._connection.execute(
                    """SELECT reconciled_until_ms FROM sync_cursors
                       WHERE source = 'binance_trades' AND scope = ?""",
                    (self._trade_history_scope(account_id, symbol),),
                ).fetchone()
                values.append(default_ms if row is None else row["reconciled_until_ms"])
        return min(values, default=default_ms)

    def list_trade_fills(
        self,
        account_id: str,
        start_time_ms: int,
        end_time_ms: int,
        *,
        symbol: str | None = None,
    ) -> tuple[TradeFillRecord, ...]:
        if not account_id:
            raise ValueError("account_id must not be empty")
        if start_time_ms < 0 or end_time_ms < start_time_ms:
            raise ValueError("trade fill query window is invalid")
        query = """SELECT symbol, exchange_trade_id, client_order_id, side,
                          price, qty, realized_pnl, commission, commission_asset,
                          traded_at_ms, exchange_order_id, position_side, margin_asset
                   FROM trade_fills
                   WHERE account_id = ? AND traded_at_ms >= ? AND traded_at_ms <= ?"""
        params: tuple[object, ...] = (account_id, start_time_ms, end_time_ms)
        if symbol is not None:
            if not symbol:
                raise ValueError("symbol must not be empty when supplied")
            query += " AND symbol = ?"
            params += (symbol,)
        query += " ORDER BY traded_at_ms, symbol, exchange_trade_id"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return tuple(
            TradeFillRecord(
                symbol=row["symbol"],
                exchange_trade_id=row["exchange_trade_id"],
                client_order_id=row["client_order_id"],
                side=row["side"],
                price=Decimal(row["price"]),
                qty=Decimal(row["qty"]),
                realized_pnl=Decimal(row["realized_pnl"]),
                commission=Decimal(row["commission"]),
                commission_asset=row["commission_asset"],
                traded_at_ms=row["traded_at_ms"],
                exchange_order_id=row["exchange_order_id"],
                position_side=row["position_side"],
                margin_asset=row["margin_asset"],
            )
            for row in rows
        )

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

    def create_review_run(
        self,
        *,
        run_id: str,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
        started_at_ms: int,
        snapshot_id: str | None = None,
    ) -> bool:
        if not all((run_id, account_id, strategy_version)):
            raise ValueError("run_id, account_id and strategy_version must not be empty")
        if slot_start_ms < 0 or started_at_ms < 0:
            raise ValueError("review timestamps must be non-negative")
        if snapshot_id is not None and not snapshot_id:
            raise ValueError("snapshot_id must not be empty")
        with self._transaction() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO review_runs (
                    run_id, account_id, strategy_version, slot_start_ms, status,
                    snapshot_id, started_at_ms
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)""",
                (
                    run_id,
                    account_id,
                    strategy_version,
                    slot_start_ms,
                    snapshot_id,
                    started_at_ms,
                ),
            )
            return cursor.rowcount == 1

    def get_review_run(
        self,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
    ) -> PersistedReviewRun | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM review_runs
                   WHERE account_id = ? AND strategy_version = ? AND slot_start_ms = ?""",
                (account_id, strategy_version, slot_start_ms),
            ).fetchone()
        return None if row is None else self._review_run_from_row(row)

    def resume_review_run(
        self,
        run_id: str,
        *,
        snapshot_id: str,
    ) -> None:
        if not run_id or not snapshot_id:
            raise ValueError("run_id and snapshot_id must not be empty")
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE review_runs
                   SET status = 'RUNNING', snapshot_id = ?, finished_at_ms = NULL
                   WHERE run_id = ? AND status IN ('RUNNING', 'PARTIAL', 'FAILED')""",
                (snapshot_id, run_id),
            )
            if cursor.rowcount == 1:
                return
            row = connection.execute(
                "SELECT status FROM review_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ReviewRunConflict("review run does not exist")
            raise ReviewRunConflict(f"review run in {row['status']} status cannot be resumed")

    def finish_review_run(
        self,
        run_id: str,
        *,
        status: ReviewRunStatus,
        snapshot_id: str,
        finished_at_ms: int,
        result_counts: Mapping[str, int],
        earliest_sampled_at_ms: int | None,
        latest_sampled_at_ms: int | None,
    ) -> None:
        if status not in {
            ReviewRunStatus.COMPLETED,
            ReviewRunStatus.PARTIAL,
            ReviewRunStatus.FAILED,
        }:
            raise ValueError("review run must finish in a terminal status")
        if not run_id or not snapshot_id:
            raise ValueError("run_id and snapshot_id must not be empty")
        if finished_at_ms < 0:
            raise ValueError("finished_at_ms must be non-negative")
        counts = dict(result_counts)
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for key, value in counts.items()
        ):
            raise ValueError("result_counts must map non-empty names to non-negative integers")
        if (earliest_sampled_at_ms is None) != (latest_sampled_at_ms is None):
            raise ValueError("earliest and latest sample times must both be set or both be empty")
        if earliest_sampled_at_ms is not None and (
            earliest_sampled_at_ms < 0
            or latest_sampled_at_ms is None
            or latest_sampled_at_ms < earliest_sampled_at_ms
        ):
            raise ValueError("sample times must be non-negative and ordered")
        counts_json = json.dumps(counts, sort_keys=True, separators=(",", ":"))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM review_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ReviewRunConflict("review run does not exist")
            if row["status"] != ReviewRunStatus.RUNNING.value:
                if (
                    row["status"] == status.value
                    and row["snapshot_id"] == snapshot_id
                    and row["finished_at_ms"] == finished_at_ms
                    and row["result_counts_json"] == counts_json
                    and row["earliest_sampled_at_ms"] == earliest_sampled_at_ms
                    and row["latest_sampled_at_ms"] == latest_sampled_at_ms
                ):
                    return
                raise ReviewRunConflict("review run was already finished with different results")
            connection.execute(
                """UPDATE review_runs
                   SET status = ?, snapshot_id = ?, finished_at_ms = ?,
                       result_counts_json = ?, earliest_sampled_at_ms = ?,
                       latest_sampled_at_ms = ?
                   WHERE run_id = ? AND status = 'RUNNING'""",
                (
                    status.value,
                    snapshot_id,
                    finished_at_ms,
                    counts_json,
                    earliest_sampled_at_ms,
                    latest_sampled_at_ms,
                    run_id,
                ),
            )

    @staticmethod
    def _review_run_from_row(row: sqlite3.Row) -> PersistedReviewRun:
        try:
            raw_counts = json.loads(row["result_counts_json"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("stored review run counts are invalid JSON") from exc
        if not isinstance(raw_counts, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for key, value in raw_counts.items()
        ):
            raise RuntimeError("stored review run counts have an invalid shape")
        return PersistedReviewRun(
            run_id=row["run_id"],
            account_id=row["account_id"],
            strategy_version=row["strategy_version"],
            slot_start_ms=row["slot_start_ms"],
            status=ReviewRunStatus(row["status"]),
            snapshot_id=row["snapshot_id"],
            started_at_ms=row["started_at_ms"],
            finished_at_ms=row["finished_at_ms"],
            result_counts=tuple(sorted(raw_counts.items())),
            earliest_sampled_at_ms=row["earliest_sampled_at_ms"],
            latest_sampled_at_ms=row["latest_sampled_at_ms"],
        )

    @staticmethod
    def _review_rotation_scope(
        account_id: str,
        strategy_version: str,
        priority: ReviewPriority,
    ) -> str:
        if not account_id or not strategy_version or not isinstance(priority, ReviewPriority):
            raise ValueError("account, strategy and review priority are required")
        return f"{account_id}:{strategy_version}:{int(priority)}"

    def load_review_rotation_cursor(
        self,
        account_id: str,
        strategy_version: str,
        priority: ReviewPriority,
    ) -> str | None:
        scope = self._review_rotation_scope(account_id, strategy_version, priority)
        with self._lock:
            row = self._connection.execute(
                "SELECT cursor FROM sync_cursors WHERE source = 'review_rotation' AND scope = ?",
                (scope,),
            ).fetchone()
        return None if row is None else row["cursor"]

    def save_review_rotation_cursor(
        self,
        account_id: str,
        strategy_version: str,
        priority: ReviewPriority,
        symbol: str,
        updated_at_ms: int,
    ) -> None:
        scope = self._review_rotation_scope(account_id, strategy_version, priority)
        if not symbol or updated_at_ms < 0:
            raise ValueError("symbol must not be empty and timestamp must be non-negative")
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO sync_cursors (source, scope, cursor, reconciled_until_ms)
                   VALUES ('review_rotation', ?, ?, ?)
                   ON CONFLICT(source, scope) DO UPDATE SET
                       cursor = excluded.cursor,
                       reconciled_until_ms = excluded.reconciled_until_ms""",
                (scope, symbol, updated_at_ms),
            )

    def create_order_intent(
        self,
        intent: OrderIntent,
        *,
        max_active_reserved_margin: Decimal | None = None,
    ) -> bool:
        """Reserve one symbol's execution slot; exact duplicate calls are harmless."""

        if max_active_reserved_margin is not None and (
            not isinstance(max_active_reserved_margin, Decimal)
            or not max_active_reserved_margin.is_finite()
            or max_active_reserved_margin < ZERO
        ):
            raise ValueError("max_active_reserved_margin must be a finite non-negative Decimal")

        with self._transaction() as connection:
            by_id = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if by_id is not None:
                if not self._intent_matches(by_id, intent):
                    raise IntentConflict("intent_id was already used for different parameters")
                if (
                    max_active_reserved_margin is not None
                    and by_id["status"] in _ACTIVE_INTENT_STATUSES
                ):
                    self._check_reservation_limit(
                        connection,
                        intent,
                        max_active_reserved_margin,
                    )
                return False

            if max_active_reserved_margin is not None:
                self._check_reservation_limit(
                    connection,
                    intent,
                    max_active_reserved_margin,
                )

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
                    reserved_margin, status, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?)""",
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
                    str(intent.reserved_margin),
                    intent.created_at_ms,
                ),
            )
            return True

    @staticmethod
    def _check_reservation_limit(
        connection: sqlite3.Connection,
        intent: OrderIntent,
        max_active_reserved_margin: Decimal,
    ) -> None:
        rows = connection.execute(
            """SELECT reserved_margin FROM order_intents
               WHERE account_id = ?
                 AND intent_id != ?
                 AND status IN ('PREPARED', 'SUBMITTING', 'UNKNOWN', 'ACKNOWLEDGED', 'PARTIALLY_FILLED')""",
            (intent.account_id, intent.intent_id),
        ).fetchall()
        already_reserved = sum((Decimal(row["reserved_margin"]) for row in rows), ZERO)
        total = already_reserved + intent.reserved_margin
        if total > max_active_reserved_margin:
            raise ReservationLimitExceeded(
                f"active margin reservations {total} exceed authorization limit "
                f"{max_active_reserved_margin}"
            )

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
            and row["reserved_margin"] == str(intent.reserved_margin)
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
            reserved_margin=Decimal(row["reserved_margin"]),
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
        account_id: str | None = None,
        symbol: str | None = None,
    ) -> tuple[PersistedOrderIntent, ...]:
        """Load active intents and their attempts for startup recovery."""

        if account_id is not None and not account_id:
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
        parameters: tuple[str, ...] = ()
        if account_id is not None:
            sql += " AND i.account_id = ?"
            parameters = (account_id,)
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
            Action.ADD_T2: (Tier.T1, {Lifecycle.OPEN, Lifecycle.QUARANTINED}),
            Action.ADD_T3: (Tier.T2, {Lifecycle.OPEN, Lifecycle.QUARANTINED}),
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
        if state.tier is not expected_tier or state.lifecycle not in {
            Lifecycle.OPEN,
            Lifecycle.QUARANTINED,
        }:
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

    def get_attempt_status(self, attempt_id: str) -> IntentStatus | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM order_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return None if row is None else IntentStatus(row["status"])

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
            connection.execute(
                """INSERT INTO state_events (
                    event_id, account_id, symbol, before_version, after_version,
                    event_type, payload_json, correlation_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'ORDER_NO_FILL_APPLIED', ?, ?, ?)""",
                (
                    f"receipt:{receipt_id}",
                    intent["account_id"],
                    intent["symbol"],
                    intent["expected_state_version"],
                    intent["expected_state_version"],
                    json.dumps(
                        {
                            "intent_id": intent_id,
                            "attempt_id": attempt["attempt_id"],
                            "receipt_id": receipt_id,
                            "action": intent["action"],
                            "filled_qty": "0",
                            "quote_qty": "0",
                            "status": terminal_status.value,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    intent_id,
                    resolved_at_ms,
                ),
            )
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
        notification_channel: str | None = None,
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

            payload = json.dumps(
                {
                    "receipt_id": receipt_id,
                    "intent_id": intent_id,
                    "action": receipt.action.value,
                    "filled_qty": str(receipt.filled_qty),
                    "quote_qty": str(receipt.quote_qty),
                    "status": terminal_status.value,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """INSERT INTO state_events (
                    event_id, account_id, symbol, before_version, after_version,
                    event_type, payload_json, correlation_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'ORDER_EXECUTION_APPLIED', ?, ?, ?)""",
                (
                    f"receipt:{receipt_id}",
                    intent["account_id"],
                    intent["symbol"],
                    previous_state.version,
                    next_state.version,
                    payload,
                    receipt_id,
                    applied_at_ms,
                ),
            )
            if notification_channel is not None:
                _enqueue_execution_notification(
                    connection,
                    channel=notification_channel,
                    account_id=intent["account_id"],
                    receipt_id=receipt_id,
                    action=receipt.action,
                    symbol=intent["symbol"],
                    previous_state=previous_state,
                    next_state=next_state,
                    receipt=receipt,
                    fills=fills,
                    applied_at_ms=applied_at_ms,
                )
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
