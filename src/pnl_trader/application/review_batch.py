"""Build a review batch from universe signals and reconciled account state."""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from ..domain.models import BUY_ACTIONS, Action, Lifecycle, SymbolState, ZERO
from .execution_models import PersistedOrderIntent
from .position_models import ExchangePosition
from .review_run_models import ReviewBatch, ReviewCandidate, ReviewPriority
from .ports import (
    PositionSnapshotPort,
    ReviewSignalSourcePort,
    StateStorePort,
)


_MANAGED_LIFECYCLES = {
    Lifecycle.OPEN,
    Lifecycle.REOPEN_PENDING,
}


class ReconciledReviewBatchSource:
    """Compose input snapshots and fail closed on unexplained exchange state.

    The signal source owns universe eligibility and returns a signal for every
    managed position and pending order, even when that symbol is no longer
    eligible for a new entry.
    """

    def __init__(
        self,
        *,
        store: StateStorePort,
        positions: PositionSnapshotPort,
        signals: ReviewSignalSourcePort,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._positions = positions
        self._signals = signals
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def load_review_batch(
        self,
        slot_start_ms: int,
    ) -> ReviewBatch:
        states = {state.symbol: state for state in self._store.list_symbol_states()}
        active_intents = self._store.list_active_order_intents()
        intents_by_symbol = {persisted.intent.symbol: persisted for persisted in active_intents}
        managed_symbols = {
            state.symbol for state in states.values()
            if state.lifecycle in _MANAGED_LIFECYCLES
        }
        managed_symbols.update(intents_by_symbol)

        position_snapshot, signals = await asyncio.gather(
            self._positions.get_position_snapshot(),
            self._signals.load_review_signals(
                managed_symbols=tuple(managed_symbols),
            ),
        )
        now_ms = self._clock_ms()

        positions_by_symbol = self._index_positions(position_snapshot)
        self._reconcile_positions(states, positions_by_symbol, intents_by_symbol)

        signals_by_symbol = {signal.symbol: signal.admitted for signal in signals}
        candidate_symbols = set(signals_by_symbol)
        candidate_symbols.update(
            symbol for symbol, position in positions_by_symbol.items()
            if position.position_amt != ZERO
        )

        candidates: list[ReviewCandidate] = []
        for symbol in candidate_symbols:
            if symbol not in states:
                if symbol in positions_by_symbol or symbol in intents_by_symbol:
                    raise RuntimeError(
                        f"exchange activity for {symbol} has no initialized local state"
                    )
                states[symbol] = self._store.ensure_idle_symbol_state(
                    symbol,
                    created_at_ms=now_ms,
                )

            admitted = signals_by_symbol[symbol]
            priority = (
                ReviewPriority.POSITION
                if states[symbol].lifecycle is Lifecycle.OPEN
                else ReviewPriority.ENTRY
            )
            candidates.append(
                ReviewCandidate(symbol=symbol, admitted=admitted, priority=priority)
            )

        return ReviewBatch(
            slot_start_ms=slot_start_ms,
            candidates=tuple(candidates),
        )

    @staticmethod
    def _index_positions(
        positions: tuple[ExchangePosition, ...],
    ) -> dict[str, ExchangePosition]:
        result: dict[str, ExchangePosition] = {}
        for position in positions:
            if position.position_side != "BOTH":
                raise RuntimeError(
                    f"{position.symbol} uses unsupported position side {position.position_side}"
                )
            if position.position_amt < ZERO:
                raise RuntimeError(f"short position detected for {position.symbol}")
            result[position.symbol] = position
        return result

    @staticmethod
    def _reconcile_positions(
        states: dict[str, SymbolState],
        positions: dict[str, ExchangePosition],
        intents: dict[str, PersistedOrderIntent],
    ) -> None:
        for symbol, state in states.items():
            actual_qty = positions[symbol].position_amt if symbol in positions else ZERO
            intent = intents.get(symbol)
            if intent is None or intent.requested_qty is None:
                if actual_qty != state.qty:
                    raise RuntimeError(
                        f"position mismatch for {symbol}: local={state.qty}, "
                        f"exchange={actual_qty}"
                    )
                continue

            if intent.intent.action in BUY_ACTIONS:
                minimum_qty = state.qty
                maximum_qty = state.qty + intent.requested_qty
            else:
                minimum_qty = max(ZERO, state.qty - intent.requested_qty)
                maximum_qty = state.qty
            if not minimum_qty <= actual_qty <= maximum_qty:
                raise RuntimeError(
                    f"exchange position for {symbol} is outside the active order range: "
                    f"expected={minimum_qty}..{maximum_qty}, exchange={actual_qty}"
                )
