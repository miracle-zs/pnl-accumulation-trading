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
    step_origin: Decimal

    def __post_init__(self) -> None:
        for name in ("min_qty", "max_qty", "step_size", "step_origin"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
        if self.min_qty <= ZERO or self.max_qty < self.min_qty:
            raise ValueError("quantity limits must be positive and ordered")
        if self.step_size <= ZERO or self.step_origin < ZERO:
            raise ValueError("step_size must be positive and step_origin non-negative")

    def align_down(self, requested_qty: Decimal) -> Decimal:
        """Return the greatest non-negative allowed grid value <= requested_qty."""

        if not isinstance(requested_qty, Decimal) or not requested_qty.is_finite():
            raise TypeError("requested_qty must be a finite Decimal")
        if requested_qty <= ZERO or requested_qty < self.step_origin:
            return ZERO
        steps = ((requested_qty - self.step_origin) / self.step_size).to_integral_value(
            rounding=ROUND_FLOOR
        )
        quantity = self.step_origin + steps * self.step_size
        return quantity if quantity >= self.min_qty else ZERO

    def contains(self, quantity: Decimal) -> bool:
        if not isinstance(quantity, Decimal) or not quantity.is_finite():
            return False
        if quantity < self.min_qty or quantity > self.max_qty:
            return False
        return (quantity - self.step_origin) % self.step_size == ZERO


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    """Normalized contract eligibility and market-order constraints."""

    symbol: str
    contract_type: str
    status: str
    quote_asset: str
    margin_asset: str
    market_quantity: MarketQuantityRules
    min_market_notional: Decimal | None = None
    max_market_notional: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("symbol", "contract_type", "status", "quote_asset", "margin_asset"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.market_quantity, MarketQuantityRules):
            raise TypeError("market_quantity must be MarketQuantityRules")
        for name in ("min_market_notional", "max_market_notional"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise TypeError(f"{name} must be a finite Decimal or None")
            if value is not None and value <= ZERO:
                raise ValueError(f"{name} must be positive when supplied")
        if (
            self.min_market_notional is not None
            and self.max_market_notional is not None
            and self.max_market_notional < self.min_market_notional
        ):
            raise ValueError("max_market_notional must be >= min_market_notional")


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

    def __post_init__(self) -> None:
        for name in (
            "symbol",
            "base_asset",
            "quote_asset",
            "margin_asset",
            "contract_type",
            "status",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.rules is not None and (
            self.rules.symbol != self.symbol
            or self.rules.quote_asset != self.quote_asset
            or self.rules.margin_asset != self.margin_asset
            or self.rules.contract_type != self.contract_type
            or self.rules.status != self.status
        ):
            raise ValueError("instrument rules must describe the same exchange instrument")


@dataclass(frozen=True, slots=True)
class ExchangeInfoSnapshot:
    """Complete normalized result of one successful exchangeInfo response."""

    snapshot_id: str
    sampled_at_ms: int
    instruments: tuple[ExchangeInstrument, ...]

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("snapshot_id must not be empty")
        if self.sampled_at_ms < 0:
            raise ValueError("sampled_at_ms must be non-negative")
        if not isinstance(self.instruments, tuple) or any(
            not isinstance(instrument, ExchangeInstrument) for instrument in self.instruments
        ):
            raise TypeError("instruments must be a tuple of ExchangeInstrument")
        if not self.instruments:
            raise ValueError("exchangeInfo snapshot must contain at least one instrument")
        if len({instrument.symbol for instrument in self.instruments}) != len(self.instruments):
            raise ValueError("an exchangeInfo snapshot cannot contain duplicate symbols")


@dataclass(frozen=True, slots=True)
class MarkPriceSnapshot:
    symbol: str
    mark_price: Decimal
    observed_at_ms: int

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if not isinstance(self.mark_price, Decimal) or not self.mark_price.is_finite():
            raise TypeError("mark_price must be a finite Decimal")
        if self.mark_price <= ZERO:
            raise ValueError("mark_price must be positive")
        if self.observed_at_ms < 0:
            raise ValueError("observed_at_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class FifteenMinuteCloseSnapshot:
    symbol: str
    slot_start_ms: int
    close_price: Decimal
    close_time_ms: int

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if (
            isinstance(self.slot_start_ms, bool)
            or not isinstance(self.slot_start_ms, int)
            or self.slot_start_ms < 0
            or self.slot_start_ms % 900_000 != 0
        ):
            raise ValueError("slot_start_ms must be a UTC 15-minute boundary")
        if not isinstance(self.close_price, Decimal) or not self.close_price.is_finite():
            raise TypeError("close_price must be a finite Decimal")
        if self.close_price <= ZERO:
            raise ValueError("close_price must be positive")
        if (
            isinstance(self.close_time_ms, bool)
            or not isinstance(self.close_time_ms, int)
            or self.close_time_ms != self.slot_start_ms + 899_999
        ):
            raise ValueError("close_time_ms must match the requested 15-minute slot")


@dataclass(frozen=True, slots=True)
class MarkPriceUniverseSnapshot:
    """One bulk mark-price response used to order a review candidate batch."""

    snapshot_id: str
    sampled_at_ms: int
    prices: tuple[MarkPriceSnapshot, ...]

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("snapshot_id must not be empty")
        if self.sampled_at_ms < 0:
            raise ValueError("sampled_at_ms must be non-negative")
        if not isinstance(self.prices, tuple) or any(
            not isinstance(price, MarkPriceSnapshot) for price in self.prices
        ):
            raise TypeError("prices must be a tuple of MarkPriceSnapshot")
        if len({price.symbol for price in self.prices}) != len(self.prices):
            raise ValueError("a mark-price snapshot cannot contain duplicate symbols")
