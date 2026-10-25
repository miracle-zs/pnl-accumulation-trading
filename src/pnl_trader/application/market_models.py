"""Exchange-neutral market data and trading-rule values."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from ..domain.models import ZERO


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
    base_asset: str
    quote_asset: str
    margin_asset: str
    contract_type: str
    status: str
    rules: InstrumentRules | None


@dataclass(frozen=True, slots=True)
class FifteenMinuteCloseSnapshot:
    symbol: str
    close_price: Decimal
