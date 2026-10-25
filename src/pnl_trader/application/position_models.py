"""Exchange positions in the application's stable vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    """One position-risk row; amount keeps the exchange's signed direction."""

    symbol: str
    position_side: str
    position_amt: Decimal
