"""Inputs supplied by universe and market-signal adapters for one review slot."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import Observation


@dataclass(frozen=True, slots=True)
class ReviewSignal:
    symbol: str
    observation: Observation

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if not isinstance(self.observation, Observation):
            raise TypeError("observation must be Observation")


@dataclass(frozen=True, slots=True)
class ReviewSignalSnapshot:
    """Versioned signal inputs for the complete managed/candidate symbol set."""

    account_id: str
    strategy_version: str
    slot_start_ms: int
    snapshot_id: str
    sampled_at_ms: int
    signals: tuple[ReviewSignal, ...]

    def __post_init__(self) -> None:
        if not self.account_id or not self.strategy_version or not self.snapshot_id:
            raise ValueError("account, strategy and snapshot identities must not be empty")
        if self.slot_start_ms < 0 or self.slot_start_ms % 900_000 != 0:
            raise ValueError("slot_start_ms must be a UTC 15-minute boundary")
        if self.sampled_at_ms < 0:
            raise ValueError("sampled_at_ms must be non-negative")
        if not isinstance(self.signals, tuple) or any(
            not isinstance(signal, ReviewSignal) for signal in self.signals
        ):
            raise TypeError("signals must be a tuple of ReviewSignal")
        if len({signal.symbol for signal in self.signals}) != len(self.signals):
            raise ValueError("a signal snapshot cannot contain a symbol more than once")
