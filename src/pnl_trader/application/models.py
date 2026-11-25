"""Application-level values shared across workflows and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from enum import Enum, IntEnum
from typing import Literal

from ..domain.models import BUY_ACTIONS, CLOSE_ACTIONS, Action, ZERO

OrderSide = Literal["BUY", "SELL"]


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
        if not self.intent_id:
            raise ValueError("intent_id must not be empty")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
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
    side: OrderSide
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
    """Exchange-reported aggregate order state."""

    status: ExchangeOrderStatus
    executed_qty: Decimal
    cumulative_quote: Decimal
    exchange_order_id: str

    def __post_init__(self) -> None:
        if self.executed_qty < ZERO:
            raise ValueError("executed_qty must be non-negative")
        if self.cumulative_quote < ZERO:
            raise ValueError("cumulative_quote must be non-negative")
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


@dataclass(frozen=True, slots=True)
class MarketQuantityRules:
    """The effective quantity grid for a MARKET order."""

    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal

    def __post_init__(self) -> None:
        if self.min_qty <= ZERO or self.max_qty < self.min_qty:
            raise ValueError("quantity limits must be positive and ordered")
        if self.step_size <= ZERO:
            raise ValueError("step_size must be positive")

    def align_down(self, requested_qty: Decimal) -> Decimal:
        """Return the greatest allowed quantity <= requested_qty, or zero if none fits."""

        if requested_qty < self.min_qty:
            return ZERO
        steps = ((requested_qty - self.min_qty) / self.step_size).to_integral_value(
            rounding=ROUND_FLOOR
        )
        return self.min_qty + steps * self.step_size


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    """Normalized contract eligibility and market-order constraints."""

    symbol: str
    contract_type: str
    status: str
    quote_asset: str
    margin_asset: str
    market_quantity: MarketQuantityRules
    min_market_notional: Decimal

    def __post_init__(self) -> None:
        if self.min_market_notional <= ZERO:
            raise ValueError("min_market_notional must be positive")


@dataclass(frozen=True, slots=True)
class ExchangeInstrument:
    """ExchangeInfo metadata for one contract, with optional usable order rules."""

    symbol: str
    quote_asset: str
    margin_asset: str
    contract_type: str
    status: str
    underlying_type: str
    underlying_sub_types: tuple[str, ...]
    rules: InstrumentRules | None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity: Decimal
    unrealized_pnl: Decimal
    available_balance: Decimal
    position_notional: Decimal


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    """One position-risk row; amount keeps the exchange's signed direction."""

    symbol: str
    position_side: str
    position_amt: Decimal


@dataclass(frozen=True, slots=True)
class ReviewSignal:
    symbol: str
    admitted: bool
    delisted: bool = False


class ReviewPriority(IntEnum):
    POSITION = 0
    ENTRY = 1


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """One symbol's entry signal and batch-level review priority."""

    symbol: str
    admitted: bool
    priority: ReviewPriority


@dataclass(frozen=True, slots=True)
class ReviewBatch:
    """Candidate set for one 15-minute review slot."""

    slot_start_ms: int
    candidates: tuple[ReviewCandidate, ...]
