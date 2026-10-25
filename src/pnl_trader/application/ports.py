"""Interfaces consumed by application workflows."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from ..domain.models import Decision, ExecutionReceipt, SymbolState, TradeFillRecord
from .execution_models import RiskAuthorization
from .market_models import (
    ExchangeInfoSnapshot,
    FifteenMinuteCloseSnapshot,
    InstrumentRules,
    MarkPriceSnapshot,
    MarkPriceUniverseSnapshot,
)
from .order_planning import MarketOrderPlan
from .position_models import ExchangePositionSnapshot
from .order_reconciliation_models import ExchangeOpenOrdersSnapshot
from .risk_models import AccountRiskSnapshot, MaintenanceMarginBracketSnapshot
from .review_batch_models import ReviewSignalSnapshot
from .universe_models import UniverseSnapshot
from .review_run_models import ReviewBatch, ReviewPriority
from .execution_models import (
    ExchangeOrderSnapshot,
    IntentStatus,
    MarketOrderRequest,
    OrderAttempt,
    OrderIntent,
    PersistedOrderIntent,
    SubmissionResult,
)


class StateStorePort(Protocol):
    def load_symbol_state(self, account_id: str, symbol: str) -> SymbolState | None: ...

    def list_symbol_states(self, account_id: str) -> tuple[SymbolState, ...]: ...

    def ensure_idle_symbol_state(
        self, account_id: str, symbol: str, *, created_at_ms: int
    ) -> SymbolState: ...

    def save_universe_snapshot(self, snapshot: UniverseSnapshot) -> None: ...

    def load_latest_universe_snapshot(self) -> UniverseSnapshot | None: ...

    def load_review_rotation_cursor(
        self,
        account_id: str,
        strategy_version: str,
        priority: ReviewPriority,
    ) -> str | None: ...

    def save_review_rotation_cursor(
        self,
        account_id: str,
        strategy_version: str,
        priority: ReviewPriority,
        symbol: str,
        updated_at_ms: int,
    ) -> None: ...

    def create_order_intent(
        self,
        intent: OrderIntent,
        *,
        max_active_reserved_margin: Decimal | None = None,
    ) -> bool: ...

    def create_order_attempt(self, attempt: OrderAttempt) -> bool: ...

    def list_active_order_intents(
        self,
        account_id: str,
        symbol: str | None = None,
    ) -> tuple[PersistedOrderIntent, ...]: ...

    def get_intent_status(self, intent_id: str) -> IntentStatus | None: ...

    def transition_intent(
        self,
        intent_id: str,
        *,
        expected: IntentStatus,
        target: IntentStatus,
        exchange_order_id: str | None = None,
    ) -> None: ...

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
    ) -> bool: ...

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
    ) -> bool: ...


class ExchangeGatewayPort(Protocol):
    async def get_instrument_rules(self, symbol: str) -> InstrumentRules: ...

    async def get_mark_price(self, symbol: str) -> MarkPriceSnapshot: ...

    async def get_fifteen_minute_close(
        self,
        symbol: str,
        slot_start_ms: int,
    ) -> FifteenMinuteCloseSnapshot: ...

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult: ...

    async def lookup_order(
        self,
        symbol: str,
        client_order_id: str,
    ) -> ExchangeOrderSnapshot | None: ...

    async def list_order_fills(
        self,
        symbol: str,
        client_order_id: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]: ...



class RiskGatePort(Protocol):
    async def authorize_order(
        self,
        *,
        account_id: str,
        state: SymbolState,
        decision: Decision,
        plan: MarketOrderPlan,
        price: MarkPriceSnapshot,
        rules: InstrumentRules,
    ) -> RiskAuthorization: ...


class AccountRiskSnapshotPort(Protocol):
    async def get_account_risk_snapshot(
        self,
        account_id: str,
        symbol: str,
    ) -> AccountRiskSnapshot: ...


class MaintenanceMarginBracketPort(Protocol):
    async def get_maintenance_margin_bracket_snapshot(
        self,
        account_id: str,
        symbol: str,
    ) -> MaintenanceMarginBracketSnapshot: ...


class PositionSnapshotPort(Protocol):
    async def get_position_snapshot(self, account_id: str) -> ExchangePositionSnapshot: ...


class OpenOrdersSnapshotPort(Protocol):
    async def get_open_orders_snapshot(self, account_id: str) -> ExchangeOpenOrdersSnapshot: ...


class ExchangeInstrumentCatalogPort(Protocol):
    async def get_exchange_info_snapshot(self) -> ExchangeInfoSnapshot: ...


class BulkMarkPricePort(Protocol):
    async def get_all_mark_prices(self) -> MarkPriceUniverseSnapshot: ...


class ReviewSignalSourcePort(Protocol):
    async def load_review_signals(
        self,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
        *,
        managed_symbols: tuple[str, ...],
    ) -> ReviewSignalSnapshot: ...


class ProcessLockPort(Protocol):
    @property
    def is_held(self) -> bool: ...


class ReviewBatchSourcePort(Protocol):
    async def load_review_batch(
        self,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
    ) -> ReviewBatch: ...
