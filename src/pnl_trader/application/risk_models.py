"""Account data and explicit policy for pre-trade risk authorization."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.models import ZERO


@dataclass(frozen=True, slots=True)
class MaintenanceMarginBracket:
    """One exchange-provided notional tier for maintenance-margin estimates."""

    bracket_number: int
    notional_floor: Decimal
    notional_cap: Decimal
    maintenance_margin_rate: Decimal
    maintenance_amount: Decimal

    def __post_init__(self) -> None:
        if self.bracket_number <= 0:
            raise ValueError("bracket_number must be positive")
        for name in (
            "notional_floor",
            "notional_cap",
            "maintenance_margin_rate",
            "maintenance_amount",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
        if self.notional_floor < ZERO or self.notional_cap <= self.notional_floor:
            raise ValueError("maintenance-margin notional bounds must be positive and ordered")
        if not ZERO < self.maintenance_margin_rate <= Decimal("1"):
            raise ValueError("maintenance_margin_rate must be in (0, 1]")
        if self.maintenance_amount < ZERO:
            raise ValueError("maintenance_amount must be non-negative")


@dataclass(frozen=True, slots=True)
class MaintenanceMarginBracketSnapshot:
    """User-specific maintenance-margin tiers for one symbol."""

    symbol: str
    brackets: tuple[MaintenanceMarginBracket, ...]
    notional_coefficient: Decimal
    fetched_at_ms: int

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if not isinstance(self.brackets, tuple) or not self.brackets:
            raise ValueError("brackets must be a non-empty tuple")
        if any(not isinstance(item, MaintenanceMarginBracket) for item in self.brackets):
            raise TypeError("brackets must contain MaintenanceMarginBracket values")
        if tuple(sorted(self.brackets, key=lambda item: item.notional_floor)) != self.brackets:
            raise ValueError("maintenance-margin brackets must be ordered by notional floor")
        if len({item.bracket_number for item in self.brackets}) != len(self.brackets):
            raise ValueError("maintenance-margin bracket numbers must be unique")
        for previous, current in zip(self.brackets, self.brackets[1:]):
            if current.notional_floor < previous.notional_cap:
                raise ValueError("maintenance-margin brackets cannot overlap")
        if (
            not isinstance(self.notional_coefficient, Decimal)
            or not self.notional_coefficient.is_finite()
            or self.notional_coefficient <= ZERO
        ):
            raise ValueError("notional_coefficient must be a positive finite Decimal")
        if self.fetched_at_ms < 0:
            raise ValueError("fetched_at_ms must be non-negative")

    def maintenance_margin_for(self, notional: Decimal) -> Decimal | None:
        """Estimate maintenance margin, returning None outside known bracket bounds."""

        if not isinstance(notional, Decimal) or not notional.is_finite():
            raise TypeError("notional must be a finite Decimal")
        if notional < ZERO:
            raise ValueError("notional must be non-negative")
        if notional == ZERO:
            return ZERO
        matching = [
            item
            for item in self.brackets
            if item.notional_floor <= notional <= item.notional_cap
        ]
        if not matching:
            return None
        # At a shared boundary, select the higher tier. The exchange's cumulative
        # amount keeps the estimate continuous across adjacent tiers.
        bracket = max(matching, key=lambda item: item.notional_floor)
        return max(
            ZERO,
            notional * bracket.maintenance_margin_rate - bracket.maintenance_amount,
        )


@dataclass(frozen=True, slots=True)
class AccountRiskSnapshot:
    """Fresh account-wide balances plus leverage and margin mode for one symbol."""

    account_id: str
    symbol: str
    wallet_balance: Decimal
    unrealized_pnl: Decimal
    available_balance: Decimal
    total_initial_margin: Decimal
    total_maintenance_margin: Decimal
    effective_leverage: Decimal
    margin_type: str
    multi_assets_mode: bool
    dual_side_position: bool
    fetched_at_ms: int
    symbol_position_qty: Decimal | None = None
    symbol_maintenance_margin: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.account_id or not self.symbol:
            raise ValueError("account_id and symbol must not be empty")
        if not self.margin_type:
            raise ValueError("margin_type must not be empty")
        if not isinstance(self.multi_assets_mode, bool) or not isinstance(
            self.dual_side_position, bool
        ):
            raise TypeError("account mode flags must be bool")
        for name in (
            "wallet_balance",
            "unrealized_pnl",
            "available_balance",
            "total_initial_margin",
            "total_maintenance_margin",
            "effective_leverage",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
        if self.wallet_balance < ZERO or self.available_balance < ZERO:
            raise ValueError("wallet_balance and available_balance must be non-negative")
        if self.total_initial_margin < ZERO or self.total_maintenance_margin < ZERO:
            raise ValueError("account margin values must be non-negative")
        for name in ("symbol_position_qty", "symbol_maintenance_margin"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite()
            ):
                raise TypeError(f"{name} must be a finite Decimal or None")
        if self.symbol_maintenance_margin is not None and self.symbol_maintenance_margin < ZERO:
            raise ValueError("symbol_maintenance_margin must be non-negative")
        if (self.symbol_position_qty is None) != (self.symbol_maintenance_margin is None):
            raise ValueError("symbol position quantity and maintenance margin must be supplied together")
        if self.effective_leverage <= ZERO:
            raise ValueError("effective_leverage must be positive")
        if self.fetched_at_ms < 0:
            raise ValueError("fetched_at_ms must be non-negative")

    @property
    def equity(self) -> Decimal:
        """Use the documented single-account equity basis for risk ratios."""

        return self.wallet_balance + self.unrealized_pnl


@dataclass(frozen=True, slots=True)
class AccountRiskPolicy:
    """Required operational limits for sizing account-level risk permits."""

    max_maintenance_margin_ratio: Decimal
    reserve_buffer: Decimal
    taker_fee_rate: Decimal
    slippage_buffer_rate: Decimal
    max_initial_margin_utilization: Decimal = Decimal("0.70")
    max_snapshot_age_ms: int = 30_000
    max_bracket_age_ms: int = 86_400_000
    future_clock_skew_ms: int = 1_000
    authorization_ttl_ms: int = 5_000

    def __post_init__(self) -> None:
        for name in (
            "max_maintenance_margin_ratio",
            "reserve_buffer",
            "taker_fee_rate",
            "slippage_buffer_rate",
            "max_initial_margin_utilization",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
        if not ZERO < self.max_initial_margin_utilization <= Decimal("1"):
            raise ValueError("max_initial_margin_utilization must be in (0, 1]")
        if self.max_maintenance_margin_ratio <= ZERO:
            raise ValueError("max_maintenance_margin_ratio must be positive")
        for name in ("reserve_buffer", "taker_fee_rate", "slippage_buffer_rate"):
            if getattr(self, name) < ZERO:
                raise ValueError(f"{name} must be non-negative")
        for name in ("max_snapshot_age_ms", "max_bracket_age_ms", "authorization_ttl_ms"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.future_clock_skew_ms < 0:
            raise ValueError("future_clock_skew_ms must be non-negative")
