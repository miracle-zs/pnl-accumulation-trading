"""Stable values for one complete 15-minute review batch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ReviewPriority(IntEnum):
    POSITION = 0
    ENTRY = 1


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """One symbol's entry signal and batch-level review priority.

    Symbols with an open position are reviewed before new or reopened entries.
    The symbol executor determines the action from the batch's latest price.
    """

    symbol: str
    admitted: bool
    priority: ReviewPriority


@dataclass(frozen=True, slots=True)
class ReviewBatch:
    """Candidate set for one 15-minute review slot."""

    slot_start_ms: int
    candidates: tuple[ReviewCandidate, ...]
