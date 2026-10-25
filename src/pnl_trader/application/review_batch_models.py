"""Inputs supplied by universe and market-signal adapters for one review slot."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewSignal:
    symbol: str
    admitted: bool
