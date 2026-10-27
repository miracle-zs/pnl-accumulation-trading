"""Interfaces consumed by application workflows."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Protocol

from ..domain.models import ExecutionReceipt, SymbolState, TradeFillRecord
from .account_models import AccountSnapshot
from .market_models import (
    ExchangeInstrument,
    InstrumentRules,
)
from .position_models import ExchangePosition
from .review_batch_models import ReviewSignal
from .review_run_models import ReviewBatch
from .execution_models import (
    ExchangeOrderSnapshot,
    IntentStatus,
    MarketOrderRequest,
    OrderIntent,
    PersistedOrderIntent,
    SubmissionResult,
)


class StateStorePort(Protocol):
    def load_symbol_state(self, symbol: str) -> SymbolState: ...

    def list_symbol_states(self) -> tuple[SymbolState, ...]: ...

    def ensure_idle_symbol_state(
        self, symbol: str, *, created_at_ms: int
    ) -> SymbolState: ...

    def clear_settled_symbol_state(
        self, symbol: str, *, updated_at_ms: int
    ) -> SymbolState: ...

    def record_account_equity_snapshot(
        self,
        *,
        captured_at_ms: int,
        snapshot: AccountSnapshot,
    ) -> None: ...

    def create_order_intent(
        self,
        intent: OrderIntent,
    ) -> None: ...

    def create_order_attempt(self, intent_id: str, *, requested_qty: Decimal) -> None: ...

    def list_active_order_intents(
        self,
        symbol: str | None = None,
    ) -> tuple[PersistedOrderIntent, ...]: ...

    def list_full_closes_missing_fills(self) -> tuple[PersistedOrderIntent, ...]: ...

    def confirm_full_close(
        self, intent_id: str, *, expected: IntentStatus, filled_qty: Decimal, updated_at_ms: int
    ) -> None: ...

    def record_full_close_fills(
        self, intent_id: str, fills: tuple[TradeFillRecord, ...]
    ) -> None: ...

    def get_intent_status(self, intent_id: str) -> IntentStatus | None: ...

    def transition_intent(
        self,
        intent_id: str,
        *,
        expected: IntentStatus,
        target: IntentStatus,
    ) -> None: ...

    def commit_confirmed_execution(
        self,
        *,
        intent_id: str,
        receipt: ExecutionReceipt,
        fills: tuple[TradeFillRecord, ...],
        expected_intent_status: IntentStatus,
        terminal_status: IntentStatus,
        updated_at_ms: int,
    ) -> None: ...

    def resolve_no_fill(
        self,
        intent_id: str,
        *,
        expected: IntentStatus,
        terminal_status: IntentStatus,
    ) -> None: ...


class AlertNotifierPort(Protocol):
    async def send(self, *, title: str, body: str) -> None: ...


class ExchangeGatewayPort(Protocol):
    async def set_leverage(self, symbol: str, leverage: int) -> int: ...

    async def get_instrument_rules(self, symbol: str) -> InstrumentRules: ...

    async def submit_market_order(self, request: MarketOrderRequest) -> SubmissionResult: ...

    async def lookup_order(
        self,
        symbol: str,
        client_order_id: str,
    ) -> ExchangeOrderSnapshot | None: ...

    async def list_order_fills(
        self,
        symbol: str,
        order_snapshot: ExchangeOrderSnapshot,
        order_side: str,
    ) -> tuple[TradeFillRecord, ...]: ...


class PositionSnapshotPort(Protocol):
    async def get_position_snapshot(self) -> tuple[ExchangePosition, ...]: ...


class AccountEquitySnapshotPort(Protocol):
    async def get_account_snapshot(self) -> AccountSnapshot: ...


class MarketPriceSnapshotPort(Protocol):
    async def get_latest_prices(self) -> Mapping[str, Decimal]: ...


class ExchangeInstrumentCatalogPort(Protocol):
    async def get_exchange_info_snapshot(self) -> tuple[ExchangeInstrument, ...]: ...


class ReviewSignalSourcePort(Protocol):
    async def load_review_signals(
        self,
        *,
        managed_symbols: tuple[str, ...],
    ) -> tuple[ReviewSignal, ...]: ...


class ReviewBatchSourcePort(Protocol):
    async def load_review_batch(
        self,
        slot_start_ms: int,
    ) -> ReviewBatch: ...
