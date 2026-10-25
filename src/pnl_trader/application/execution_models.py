"""Stable application values shared by execution and persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import re

from ..domain.models import Action, ZERO


class IntentStatus(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: str
    account_id: str
    symbol: str
    cycle_id: int
    action: Action
    business_key: str
    expected_state_version: int
    created_at_ms: int
    target_cumulative_sold_qty: Decimal | None = None
    buy_notional: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("intent_id", "account_id", "symbol", "business_key"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.cycle_id < 0 or self.expected_state_version < 0 or self.created_at_ms < 0:
            raise ValueError("cycle, version and timestamp values must be non-negative")
        for name in ("target_cumulative_sold_qty", "buy_notional"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal or None")
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if (self.target_cumulative_sold_qty is None) == (self.buy_notional is None):
            raise ValueError("an intent must define exactly one close target or buy notional")
        buy_actions = {Action.OPEN_T1, Action.ADD_T2, Action.ADD_T3}
        close_actions = {
            Action.CLOSE_T1,
            Action.CLOSE_T2_BATCH1,
            Action.CLOSE_T2_FULL,
            Action.CLOSE_T3_TO_ONE_THIRD,
            Action.CLOSE_T3_TO_TWO_THIRDS,
            Action.CLOSE_T3_FULL,
        }
        if self.action in buy_actions and self.buy_notional is None:
            raise ValueError("a buy action requires buy_notional")
        if self.action in close_actions and self.target_cumulative_sold_qty is None:
            raise ValueError("a close action requires target_cumulative_sold_qty")
        if self.action not in buy_actions | close_actions:
            raise ValueError("HOLD cannot be stored as an order intent")
        if self.target_cumulative_sold_qty is not None and self.target_cumulative_sold_qty <= ZERO:
            raise ValueError("target_cumulative_sold_qty must be positive")
        if self.buy_notional is not None and self.buy_notional <= ZERO:
            raise ValueError("buy_notional must be positive")


@dataclass(frozen=True, slots=True)
class OrderAttempt:
    attempt_id: str
    intent_id: str
    attempt_no: int
    client_order_id: str
    requested_qty: Decimal
    sent_at_ms: int

    def __post_init__(self) -> None:
        for name in ("attempt_id", "intent_id", "client_order_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not re.fullmatch(r"[.A-Za-z0-9_:/-]{1,36}", self.client_order_id):
            raise ValueError("client_order_id does not match Binance's allowed format")
        if self.attempt_no != 1:
            raise ValueError("an intent may have one submit attempt; re-evaluate before creating a new intent")
        if self.sent_at_ms < 0:
            raise ValueError("sent_at_ms must be non-negative")
        if not isinstance(self.requested_qty, Decimal) or not self.requested_qty.is_finite():
            raise TypeError("requested_qty must be a finite Decimal")
        if self.requested_qty <= ZERO:
            raise ValueError("requested_qty must be positive")


@dataclass(frozen=True, slots=True)
class PersistedOrderAttempt:
    attempt_id: str
    attempt_no: int
    client_order_id: str
    exchange_order_id: str | None
    requested_qty: Decimal
    status: IntentStatus
    applied_cum_qty: Decimal
    cumulative_quote: Decimal
    sent_at_ms: int | None


@dataclass(frozen=True, slots=True)
class PersistedOrderIntent:
    intent: OrderIntent
    status: IntentStatus
    attempt: PersistedOrderAttempt | None


@dataclass(frozen=True, slots=True)
class MarketOrderRequest:
    symbol: str
    side: str
    quantity: Decimal
    client_order_id: str
    reduce_only: bool

    def __post_init__(self) -> None:
        if not self.symbol or not self.client_order_id:
            raise ValueError("symbol and client_order_id must not be empty")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite():
            raise TypeError("quantity must be a finite Decimal")
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")
        if not re.fullmatch(r"[.A-Za-z0-9_:/-]{1,36}", self.client_order_id):
            raise ValueError("client_order_id does not match Binance's allowed format")
        if self.reduce_only != (self.side == "SELL"):
            raise ValueError("long-only market sells must be reduce-only")


class ExchangeOrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.REJECTED,
            ExchangeOrderStatus.CANCELED,
            ExchangeOrderStatus.EXPIRED,
            ExchangeOrderStatus.EXPIRED_IN_MATCH,
        }


@dataclass(frozen=True, slots=True)
class ExchangeOrderSnapshot:
    status: ExchangeOrderStatus
    executed_qty: Decimal
    cumulative_quote: Decimal
    exchange_order_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExchangeOrderStatus):
            raise TypeError("status must be an ExchangeOrderStatus")
        for name in ("executed_qty", "cumulative_quote"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
            if value < ZERO:
                raise ValueError(f"{name} must be non-negative")
        if (self.executed_qty == ZERO) != (self.cumulative_quote == ZERO):
            raise ValueError("quantity and quote must both be zero or both be positive")
        if self.status in {ExchangeOrderStatus.NEW, ExchangeOrderStatus.REJECTED}:
            if self.executed_qty != ZERO:
                raise ValueError(f"{self.status.value} order cannot have executed quantity")
        if self.status is ExchangeOrderStatus.PARTIALLY_FILLED and self.executed_qty <= ZERO:
            raise ValueError("PARTIALLY_FILLED requires positive executed quantity")
        if self.status is ExchangeOrderStatus.FILLED and self.executed_qty <= ZERO:
            raise ValueError("FILLED requires positive executed quantity")


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
        if not isinstance(self.certainty, SubmissionCertainty):
            raise TypeError("certainty must be a SubmissionCertainty")
        if self.order is not None and not isinstance(self.order, ExchangeOrderSnapshot):
            raise TypeError("order must be an ExchangeOrderSnapshot or None")
        if self.certainty is SubmissionCertainty.ACCEPTED and self.order is None:
            raise ValueError("an accepted submission must include an order snapshot")
        if (
            self.certainty is SubmissionCertainty.ACCEPTED
            and self.order is not None
            and self.order.status is ExchangeOrderStatus.REJECTED
        ):
            raise ValueError("a rejected order must use definitive REJECTED certainty")
        if self.certainty is SubmissionCertainty.REJECTED and self.order is not None:
            raise ValueError("a definitive rejection cannot include an accepted order")
        if self.certainty is SubmissionCertainty.UNKNOWN and self.order is not None:
            raise ValueError("an unknown submission cannot include a confirmed order")
