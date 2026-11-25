"""Build a review batch from universe signals and reconciled account state."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from ..clock import current_epoch_ms
from ..domain.models import BUY_ACTIONS, Lifecycle, SymbolState, ZERO
from .models import (
    ExchangePosition,
    PersistedOrderIntent,
    ReviewBatch,
    ReviewCandidate,
    ReviewPriority,
)
from .ports import (
    PositionSnapshotPort,
    ReviewSignalSourcePort,
)

if TYPE_CHECKING:
    from ..adapters.sqlite.store import SQLiteStateStore


_MANAGED_LIFECYCLES = {
    Lifecycle.OPEN,
    Lifecycle.CLOSING,
    Lifecycle.REOPEN_PENDING,
}
logger = logging.getLogger(__name__)


class ReconciledReviewBatchSource:
    """Compose input snapshots and fail closed on unexplained exchange state.

    The signal source owns universe eligibility and returns a signal for every
    locally stored symbol, including idle symbols, and every eligible entry
    candidate. This lets the batch clear issues for symbols that leave the
    active universe without reviewing them as trade candidates.
    """

    def __init__(
        self,
        *,
        store: SQLiteStateStore,
        positions: PositionSnapshotPort,
        signals: ReviewSignalSourcePort,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._positions = positions
        self._signals = signals
        self._clock_ms = current_epoch_ms if clock_ms is None else clock_ms

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
        tracked_symbols = set(states)
        tracked_symbols.update(managed_symbols)

        position_snapshot, signals = await asyncio.gather(
            self._positions.get_position_snapshot(),
            self._signals.load_review_signals(
                managed_symbols=tuple(tracked_symbols),
            ),
        )
        now_ms = self._clock_ms()

        positions_by_symbol = self._index_positions(position_snapshot)
        signals_by_symbol = {signal.symbol: signal for signal in signals}
        for symbol, state in states.items():
            position = positions_by_symbol.get(symbol)
            actual_qty = position.position_amt if position is not None else ZERO
            if symbol in intents_by_symbol or actual_qty != ZERO:
                continue
            signal = signals_by_symbol.get(symbol)
            stale_reopen = state.lifecycle is Lifecycle.REOPEN_PENDING and (
                signal is None or signal.delisted or not signal.admitted
            )
            if state.qty > ZERO or stale_reopen:
                states[symbol] = self._store.clear_flat_symbol_state(
                    symbol,
                    updated_at_ms=now_ms,
                )
                logger.info("cleared local state for flat exchange position: symbol=%s", symbol)

        self._reconcile_positions(states, positions_by_symbol, intents_by_symbol)

        active_managed_symbols = {
            state.symbol for state in states.values()
            if state.lifecycle in _MANAGED_LIFECYCLES
        }
        active_managed_symbols.update(intents_by_symbol)

        candidate_symbols = {
            symbol
            for symbol, signal in signals_by_symbol.items()
            if not signal.delisted and (signal.admitted or symbol in active_managed_symbols)
        }
        candidate_symbols.update(
            symbol for symbol, position in positions_by_symbol.items()
            if position.position_amt != ZERO
        )
        candidate_symbols.update(intents_by_symbol)

        inactive_symbols = set(signals_by_symbol) - candidate_symbols
        self._store.clear_dashboard_issues(tuple(sorted(inactive_symbols)))

        candidates: list[ReviewCandidate] = []
        for symbol in candidate_symbols:
            if symbol not in states:
                states[symbol] = self._store.ensure_idle_symbol_state(
                    symbol,
                    created_at_ms=now_ms,
                )

            admitted = signals_by_symbol[symbol].admitted
            priority = (
                ReviewPriority.POSITION
                if states[symbol].qty > ZERO
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
            if position.symbol in result:
                raise RuntimeError(
                    f"position snapshot contains duplicate rows for {position.symbol}"
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
        for symbol, position in positions.items():
            if position.position_amt != ZERO and symbol not in states:
                raise RuntimeError(
                    f"exchange activity for {symbol} has no initialized local state"
                )

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
                if intent.requested_qty > state.qty:
                    raise RuntimeError(
                        f"active sell intent for {symbol} exceeds local position: "
                        f"requested={intent.requested_qty}, local={state.qty}"
                    )
                minimum_qty = state.qty - intent.requested_qty
                maximum_qty = state.qty
            if not minimum_qty <= actual_qty <= maximum_qty:
                raise RuntimeError(
                    f"exchange position for {symbol} is outside the active order range: "
                    f"expected={minimum_qty}..{maximum_qty}, exchange={actual_qty}"
                )
