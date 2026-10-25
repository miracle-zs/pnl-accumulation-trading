"""Stable values for one complete 15-minute review batch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from ..domain.models import Observation


class ReviewPriority(IntEnum):
    REDUCE = 0
    ADD = 1
    OPEN = 2


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """One symbol's entry signals and batch-level execution priority.

    The batch producer assigns priority from its aligned position and market
    snapshot; the symbol executor re-evaluates the executable action using a
    fresh mark price before any order is authorized.
    """

    symbol: str
    signals: Observation
    priority: ReviewPriority

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("candidate symbol must not be empty")
        if not isinstance(self.signals, Observation):
            raise TypeError("candidate signals must be Observation")
        if not isinstance(self.priority, ReviewPriority):
            raise TypeError("candidate priority must be ReviewPriority")


@dataclass(frozen=True, slots=True)
class ReviewBatch:
    """Validated, versioned input snapshot handed to the run coordinator."""

    account_id: str
    strategy_version: str
    slot_start_ms: int
    sampled_at_ms: int
    candidates: tuple[ReviewCandidate, ...]

    def __post_init__(self) -> None:
        if not self.account_id or not self.strategy_version:
            raise ValueError("account and strategy identities must not be empty")
        if self.slot_start_ms < 0 or self.slot_start_ms % 900_000 != 0:
            raise ValueError("slot_start_ms must be a UTC 15-minute boundary")
        if self.sampled_at_ms < 0:
            raise ValueError("sampled_at_ms must be non-negative")
        if not self.candidates:
            raise ValueError("a review batch must contain at least one candidate")
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(candidate, ReviewCandidate) for candidate in self.candidates
        ):
            raise TypeError("candidates must be a tuple of ReviewCandidate values")
        if len({candidate.symbol for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("a review batch cannot contain a symbol more than once")
