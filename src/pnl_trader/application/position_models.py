"""Exchange position snapshots in the application's stable vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    """One position-risk row; amount keeps the exchange's signed direction."""

    symbol: str
    position_side: str
    position_amt: Decimal
    entry_price: Decimal
    update_time_ms: int

    def __post_init__(self) -> None:
        if not self.symbol or not self.position_side:
            raise ValueError("symbol and position_side must not be empty")
        for name in ("position_amt", "entry_price"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
        if self.entry_price < 0:
            raise ValueError("entry_price must be non-negative")
        if self.update_time_ms < 0:
            raise ValueError("update_time_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class ExchangePositionSnapshot:
    """A successfully fetched, fully parsed response for one account."""

    account_id: str
    sampled_at_ms: int
    positions: tuple[ExchangePosition, ...]

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must not be empty")
        if self.sampled_at_ms < 0:
            raise ValueError("sampled_at_ms must be non-negative")
        if not isinstance(self.positions, tuple) or any(
            not isinstance(position, ExchangePosition) for position in self.positions
        ):
            raise TypeError("positions must be a tuple of ExchangePosition")
        keys = {(position.symbol, position.position_side) for position in self.positions}
        if len(keys) != len(self.positions):
            raise ValueError("a position snapshot cannot contain duplicate symbol sides")
