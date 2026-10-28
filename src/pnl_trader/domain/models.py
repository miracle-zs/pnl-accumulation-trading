"""Domain values shared by the periodic-review policy and execution layer."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, IntEnum


ZERO = Decimal("0")


class Lifecycle(str, Enum):
    IDLE = "IDLE"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    REOPEN_PENDING = "REOPEN_PENDING"


class Tier(IntEnum):
    NONE = 0
    T1 = 1
    T2 = 2
    T3 = 3


class Action(str, Enum):
    HOLD = "HOLD"
    OPEN_T1 = "OPEN_T1"
    ADD_T2 = "ADD_T2"
    ADD_T3 = "ADD_T3"
    CLOSE_T1 = "CLOSE_T1"
    CLOSE_T2_BATCH1 = "CLOSE_T2_BATCH1"
    CLOSE_T2_FULL = "CLOSE_T2_FULL"
    CLOSE_T3_TO_ONE_THIRD = "CLOSE_T3_TO_ONE_THIRD"
    CLOSE_T3_TO_TWO_THIRDS = "CLOSE_T3_TO_TWO_THIRDS"
    CLOSE_T3_FULL = "CLOSE_T3_FULL"


BUY_ACTION_TIERS = {
    Action.OPEN_T1: Tier.T1,
    Action.ADD_T2: Tier.T2,
    Action.ADD_T3: Tier.T3,
}
CLOSE_ACTION_TIERS = {
    Action.CLOSE_T1: Tier.T1,
    Action.CLOSE_T2_BATCH1: Tier.T2,
    Action.CLOSE_T2_FULL: Tier.T2,
    Action.CLOSE_T3_TO_ONE_THIRD: Tier.T3,
    Action.CLOSE_T3_TO_TWO_THIRDS: Tier.T3,
    Action.CLOSE_T3_FULL: Tier.T3,
}
BUY_ACTIONS = frozenset(BUY_ACTION_TIERS)
CLOSE_ACTIONS = frozenset(CLOSE_ACTION_TIERS)
BATCH_CLOSE_ACTIONS = frozenset({
    Action.CLOSE_T2_BATCH1,
    Action.CLOSE_T3_TO_ONE_THIRD,
    Action.CLOSE_T3_TO_TWO_THIRDS,
})
FULL_CLOSE_BY_TIER = {
    Tier.T1: Action.CLOSE_T1,
    Tier.T2: Action.CLOSE_T2_FULL,
    Tier.T3: Action.CLOSE_T3_FULL,
}
FULL_CLOSE_ACTIONS = frozenset(FULL_CLOSE_BY_TIER.values())


@dataclass(frozen=True, slots=True)
class SymbolState:
    """Persistable strategy state for one symbol and its current position cycle.

    ``tier_anchor_price`` is the actual volume-weighted fill price of the most
    recent Tier buy. ``tier_entry_qty`` is the position quantity when that Tier
    was entered. Confirmed sells reduce ``qty``; cumulative sold quantity is
    derived from the difference and the average entry price remains unchanged.
    """

    symbol: str
    tier: Tier = Tier.NONE
    lifecycle: Lifecycle = Lifecycle.IDLE
    qty: Decimal = ZERO
    avg_entry_price: Decimal = ZERO
    tier_entry_qty: Decimal = ZERO
    tier_anchor_price: Decimal = ZERO
    completed_batches: int = 0

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        for name in (
            "qty",
            "avg_entry_price",
            "tier_entry_qty",
            "tier_anchor_price",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
            if value < ZERO:
                raise ValueError(f"{name} must be non-negative")

        if self.lifecycle in {Lifecycle.OPEN, Lifecycle.CLOSING}:
            if self.tier is Tier.NONE:
                raise ValueError("an open lifecycle requires an active Tier")
            if self.qty <= ZERO or self.tier_entry_qty <= ZERO:
                raise ValueError("an active Tier must have positive quantities")
            if self.qty > self.tier_entry_qty:
                raise ValueError("position quantity cannot exceed Tier entry quantity")
            if self.avg_entry_price <= ZERO or self.tier_anchor_price <= ZERO:
                raise ValueError("an active Tier must have positive prices")
            max_batches = 0 if self.tier is Tier.T1 else 1 if self.tier is Tier.T2 else 2
            if not 0 <= self.completed_batches <= max_batches:
                raise ValueError("completed_batches is outside this Tier's batch count")
        elif self.tier is not Tier.NONE or any(
            value != ZERO
            for value in (
                self.qty,
                self.avg_entry_price,
                self.tier_entry_qty,
                self.tier_anchor_price,
            )
        ) or self.completed_batches != 0:
            raise ValueError("a position-free lifecycle must have empty Tier state")

    @property
    def tier_sold_qty(self) -> Decimal:
        return self.tier_entry_qty - self.qty


@dataclass(frozen=True, slots=True)
class Observation:
    """One positive market price and the symbol's universe admission decision.

    Admission authorizes opening, DCA increases, and reopening after a full
    close. ``price`` is the strategy's only market-price reference.
    """

    admitted: bool
    price: Decimal

    def __post_init__(self) -> None:
        if self.price <= ZERO:
            raise ValueError("price must be positive")


@dataclass(frozen=True, slots=True)
class Decision:
    """A pure policy result; it does not submit orders or mutate state.

    For a close action, ``close_qty`` is the incremental quantity to request.
    The execution layer aligns it to exchange rules and records the effective
    cumulative target in the order intent.
    """

    action: Action
    close_qty: Decimal | None = None
    buy_notional: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Exchange-reported aggregate for one terminal order, matched against fills.

    ``quote_qty`` is the sum of fill price × fill quantity, before fees.
    For closing actions, ``target_cumulative_sold_qty`` is the exchange-aligned
    cumulative Tier sell target persisted with the intent. This aggregate is
    applied only after the order is terminal, so a multi-fill order advances a
    Tier once.
    """

    action: Action
    filled_qty: Decimal
    quote_qty: Decimal
    target_cumulative_sold_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TradeFillRecord:
    """One exchange fill reconciled against its order before ledger insertion."""

    symbol: str
    exchange_trade_id: str
    side: str
    price: Decimal
    qty: Decimal
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str
    traded_at_ms: int

    def __post_init__(self) -> None:
        if self.price <= ZERO or self.qty <= ZERO or self.commission < ZERO:
            raise ValueError("price and qty must be positive; commission must be non-negative")


class InvalidExecution(ValueError):
    """Raised when a confirmed exchange result violates the domain contract."""
