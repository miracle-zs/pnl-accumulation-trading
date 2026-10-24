"""Exchange open-order snapshots used to detect unsupported account activity."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ExchangeOpenOrder:
    symbol: str
    exchange_order_id: str
    client_order_id: str
    side: str
    position_side: str
    order_type: str
    status: str
    reduce_only: bool
    updated_at_ms: int
    original_qty: Decimal | None = None
    executed_qty: Decimal | None = None
    algorithmic: bool = False

    def __post_init__(self) -> None:
        for name in (
            "symbol",
            "exchange_order_id",
            "client_order_id",
            "position_side",
            "order_type",
            "status",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if not isinstance(self.reduce_only, bool) or not isinstance(self.algorithmic, bool):
            raise TypeError("reduce_only and algorithmic must be bool")
        if self.updated_at_ms < 0:
            raise ValueError("updated_at_ms must be non-negative")
        for name in ("original_qty", "executed_qty"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite()
            ):
                raise TypeError(f"{name} must be a finite Decimal or None")

        if self.algorithmic:
            return
        if self.status not in {"NEW", "PARTIALLY_FILLED"}:
            raise ValueError("a standard open order must have a live status")
        if self.original_qty is None or self.original_qty <= 0:
            raise ValueError("a standard open order requires positive original_qty")
        if self.executed_qty is None or self.executed_qty < 0:
            raise ValueError("a standard open order requires non-negative executed_qty")
        if self.executed_qty >= self.original_qty:
            raise ValueError("a live standard order cannot be fully executed")
        if self.status == "NEW" and self.executed_qty != 0:
            raise ValueError("a NEW order cannot have executed quantity")
        if self.status == "PARTIALLY_FILLED" and self.executed_qty <= 0:
            raise ValueError("PARTIALLY_FILLED requires positive executed quantity")


@dataclass(frozen=True, slots=True)
class ExchangeOpenOrdersSnapshot:
    account_id: str
    sampled_at_ms: int
    orders: tuple[ExchangeOpenOrder, ...]

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must not be empty")
        if self.sampled_at_ms < 0:
            raise ValueError("sampled_at_ms must be non-negative")
        if not isinstance(self.orders, tuple) or any(
            not isinstance(order, ExchangeOpenOrder) for order in self.orders
        ):
            raise TypeError("orders must be a tuple of ExchangeOpenOrder")
        order_ids = {(order.algorithmic, order.exchange_order_id) for order in self.orders}
        client_ids = {(order.algorithmic, order.client_order_id) for order in self.orders}
        if len(order_ids) != len(self.orders) or len(client_ids) != len(self.orders):
            raise ValueError("an open-order snapshot cannot contain duplicate order identities")
