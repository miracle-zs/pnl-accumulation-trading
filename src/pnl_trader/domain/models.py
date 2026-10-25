"""Domain values shared by the periodic-review policy and execution layer."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, IntEnum


ZERO = Decimal("0")


class Lifecycle(str, Enum):
    IDLE = "IDLE"
    OPEN = "OPEN"
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


@dataclass(frozen=True, slots=True)
class SymbolState:
    """Persistable strategy state for one symbol and its current position cycle.

    ``tier_anchor_price`` is the actual volume-weighted fill price of the most
    recent Tier buy. ``tier_entry_qty`` is the position quantity when that Tier
    was entered. Confirmed sells reduce ``qty`` and increase
    ``tier_sold_qty``; the average entry price remains unchanged by a reduction.
    """

    symbol: str
    cycle_id: int = 0
    tier: Tier = Tier.NONE
    lifecycle: Lifecycle = Lifecycle.IDLE
    qty: Decimal = ZERO
    avg_entry_price: Decimal = ZERO
    tier_entry_qty: Decimal = ZERO
    tier_anchor_price: Decimal = ZERO
    tier_sold_qty: Decimal = ZERO
    completed_batches: int = 0
    version: int = 0

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if self.cycle_id < 0 or self.version < 0:
            raise ValueError("cycle_id and version must be non-negative")
        if self.completed_batches < 0:
            raise ValueError("completed_batches must be non-negative")
        for name in (
            "qty",
            "avg_entry_price",
            "tier_entry_qty",
            "tier_anchor_price",
            "tier_sold_qty",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
            if value < ZERO:
                raise ValueError(f"{name} must be non-negative")

        has_position = self.lifecycle is Lifecycle.OPEN and self.tier is not Tier.NONE

        if has_position:
            if self.qty <= ZERO or self.tier_entry_qty <= ZERO:
                raise ValueError("an active Tier must have positive quantities")
            if self.avg_entry_price <= ZERO or self.tier_anchor_price <= ZERO:
                raise ValueError("an active Tier must have positive prices")
            if self.qty + self.tier_sold_qty != self.tier_entry_qty:
                raise ValueError("qty + tier_sold_qty must equal tier_entry_qty")
            max_batches = 0 if self.tier is Tier.T1 else 1 if self.tier is Tier.T2 else 2
            if self.completed_batches > max_batches:
                raise ValueError("completed_batches exceeds this Tier's batch count")
        elif self.tier is not Tier.NONE or any(
            value != ZERO
            for value in (
                self.qty,
                self.avg_entry_price,
                self.tier_entry_qty,
                self.tier_anchor_price,
                self.tier_sold_qty,
            )
        ):
            raise ValueError("a position-free lifecycle must have empty Tier state")
        elif self.completed_batches != 0:
            raise ValueError("a position-free lifecycle cannot have completed batches")


@dataclass(frozen=True, slots=True)
class Observation:
    """One validated market observation and its explicit entry authorizations.

    ``admitted`` allows an idle symbol to open, ``scale_in_authorized`` allows
    DCA increases, and ``reopen_authorized`` approves returning to Tier 1 after
    a confirmed full close. Account risk and exchange rules remain mandatory.
    """

    mark_price: Decimal | None
    admitted: bool = False
    reopen_authorized: bool = False
    valid: bool = True
    scale_in_authorized: bool = True
    close_price: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("admitted", "reopen_authorized", "valid", "scale_in_authorized"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if self.mark_price is not None:
            if not isinstance(self.mark_price, Decimal):
                raise TypeError("mark_price must be Decimal or None")
            if not self.mark_price.is_finite():
                raise ValueError("mark_price must be finite")
        if self.close_price is not None:
            if not isinstance(self.close_price, Decimal):
                raise TypeError("close_price must be Decimal or None")
            if not self.close_price.is_finite():
                raise ValueError("close_price must be finite")


@dataclass(frozen=True, slots=True)
class Decision:
    """A pure policy result; it does not submit orders or mutate state.

    For a close action, ``close_qty`` is the incremental quantity to request.
    The execution layer aligns it to exchange rules and records the effective
    cumulative target in the order intent.
    """

    action: Action
    reason: str
    close_qty: Decimal | None = None
    buy_notional: Decimal | None = None

    def __post_init__(self) -> None:
        if self.close_qty is not None and self.buy_notional is not None:
            raise ValueError("a decision cannot close and buy in the same action")
        for name in ("close_qty", "buy_notional"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal or None")
            if not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be positive and finite")


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Aggregate fills for one terminal order, already reconciled with Binance.

    ``quote_qty`` is the sum of fill price × fill quantity, before fees.
    For closing actions, ``target_cumulative_sold_qty`` is the exchange-aligned
    cumulative Tier sell target persisted with the intent. Receipts are applied
    only after the order is terminal, so a multi-fill order advances a Tier once.
    """

    action: Action
    filled_qty: Decimal
    quote_qty: Decimal
    order_terminal: bool = True
    target_cumulative_sold_qty: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("filled_qty", "quote_qty", "target_cumulative_sold_qty"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal or None")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class TradeFillRecord:
    """One reconciled exchange fill recorded in the execution ledger."""

    symbol: str
    exchange_trade_id: str
    client_order_id: str | None
    side: str
    price: Decimal
    qty: Decimal
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str
    traded_at_ms: int
    exchange_order_id: str | None = None
    position_side: str | None = None
    margin_asset: str | None = None

    def __post_init__(self) -> None:
        for name in ("symbol", "exchange_trade_id", "commission_asset"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        for name in (
            "client_order_id",
            "exchange_order_id",
            "position_side",
            "margin_asset",
        ):
            value = getattr(self, name)
            if value is not None and not value:
                raise ValueError(f"{name} must not be empty when supplied")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        for name in ("price", "qty", "realized_pnl", "commission"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
        if self.price <= ZERO or self.qty <= ZERO or self.commission < ZERO:
            raise ValueError("price and qty must be positive; commission must be non-negative")
        if self.traded_at_ms < 0:
            raise ValueError("traded_at_ms must be non-negative")


class InvalidExecution(ValueError):
    """Raised when a confirmed exchange result violates the domain contract."""
