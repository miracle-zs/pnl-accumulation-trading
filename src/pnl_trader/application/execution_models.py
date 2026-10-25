"""Stable application values shared by execution and persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ..domain.models import BUY_ACTIONS, CLOSE_ACTIONS, Action, ZERO


class IntentStatus(str, Enum):
    PREPARED = "PREPARED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: str
    symbol: str
    action: Action
    created_at_ms: int
    target_cumulative_sold_qty: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("intent_id", "symbol"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        target = self.target_cumulative_sold_qty
        if target is not None and not isinstance(target, Decimal):
            raise TypeError("target_cumulative_sold_qty must be Decimal or None")
        if target is not None and not target.is_finite():
            raise ValueError("target_cumulative_sold_qty must be finite")
        if self.action in BUY_ACTIONS:
            if self.target_cumulative_sold_qty is not None:
                raise ValueError("a buy action cannot have a cumulative sell target")
        elif self.action in CLOSE_ACTIONS:
            if self.target_cumulative_sold_qty is None:
                raise ValueError("a close action requires a cumulative sell target")
        else:
            raise ValueError("HOLD cannot be stored as an order intent")
        if target is not None and target <= ZERO:
            raise ValueError("target_cumulative_sold_qty must be positive")


@dataclass(frozen=True, slots=True)
class PersistedOrderIntent:
    intent: OrderIntent
    status: IntentStatus
    requested_qty: Decimal | None


@dataclass(frozen=True, slots=True)
class MarketOrderRequest:
    symbol: str
    side: str
    quantity: Decimal
    client_order_id: str


class ExchangeOrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"


@dataclass(frozen=True, slots=True)
class ExchangeOrderSnapshot:
    status: ExchangeOrderStatus
    executed_qty: Decimal
    cumulative_quote: Decimal
    exchange_order_id: str

    def __post_init__(self) -> None:
        for name in ("executed_qty", "cumulative_quote"):
            value = getattr(self, name)
            if value < ZERO:
                raise ValueError(f"{name} must be non-negative")
        if (self.executed_qty == ZERO) != (self.cumulative_quote == ZERO):
            raise ValueError("quantity and quote must both be zero or both be positive")
        if (
            self.status in {ExchangeOrderStatus.NEW, ExchangeOrderStatus.REJECTED}
            and self.executed_qty != ZERO
        ):
            raise ValueError(f"{self.status.value} order cannot have executed quantity")
        if self.status in {
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
        } and self.executed_qty <= ZERO:
            raise ValueError(f"{self.status.value} requires positive executed quantity")


class SubmissionCertainty(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    certainty: SubmissionCertainty
    order: ExchangeOrderSnapshot | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if (self.certainty is SubmissionCertainty.ACCEPTED) != (self.order is not None):
            raise ValueError("an accepted submission must include its order snapshot")
        if (
            self.certainty is SubmissionCertainty.ACCEPTED
            and self.order is not None
            and self.order.status is ExchangeOrderStatus.REJECTED
        ):
            raise ValueError("a rejected order must use definitive REJECTED certainty")
