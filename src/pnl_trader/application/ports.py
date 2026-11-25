"""External seam protocols consumed by application workflows."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Protocol

from ..domain.models import TradeFillRecord
from .models import (
    AccountSnapshot,
    ExchangeInstrument,
    ExchangeOrderSnapshot,
    ExchangePosition,
    InstrumentRules,
    MarketOrderRequest,
    OrderSide,
    ReviewBatch,
    ReviewSignal,
    SubmissionResult,
)


class AlertNotifierPort(Protocol):
    async def send(self, *, title: str, body: str) -> None: ...


class ExchangeGatewayPort(Protocol):
    async def set_leverage(self, symbol: str, leverage: int) -> int: ...

    async def reserve_order_submission(self) -> None: ...

    async def get_instrument_rules(self, symbol: str) -> InstrumentRules: ...

    async def get_exchange_info_snapshot(self) -> tuple[ExchangeInstrument, ...]: ...

    async def get_latest_prices(self) -> Mapping[str, Decimal]: ...

    async def get_position_snapshot(self) -> tuple[ExchangePosition, ...]: ...

    async def get_account_snapshot(self) -> AccountSnapshot: ...

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
        order_side: OrderSide,
    ) -> tuple[TradeFillRecord, ...]: ...


class PositionSnapshotPort(Protocol):
    async def get_position_snapshot(self) -> tuple[ExchangePosition, ...]: ...


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
