"""Account values sampled for the dashboard."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity: Decimal
    unrealized_pnl: Decimal
    available_balance: Decimal
    position_notional: Decimal
