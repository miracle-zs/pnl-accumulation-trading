"""Build a review batch from universe signals and reconciled account state."""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from ..domain.models import Action, Lifecycle, Observation, SymbolState, ZERO
from ..domain.policy import StrategyPolicy
from .execution_models import PersistedOrderIntent
from .order_reconciliation_models import ExchangeOpenOrdersSnapshot
from .position_models import ExchangePosition, ExchangePositionSnapshot
from .review_batch_models import ReviewSignalSnapshot
from .review_run_models import ReviewBatch, ReviewCandidate, ReviewPriority
from .ports import (
    OpenOrdersSnapshotPort,
    PositionSnapshotPort,
    ReviewSignalSourcePort,
    StateStorePort,
)


_CLOSE_ACTIONS = {
    Action.CLOSE_T1,
    Action.CLOSE_T2_BATCH1,
    Action.CLOSE_T2_FULL,
    Action.CLOSE_T3_TO_ONE_THIRD,
    Action.CLOSE_T3_TO_TWO_THIRDS,
    Action.CLOSE_T3_FULL,
}
_ADD_ACTIONS = {Action.ADD_T2, Action.ADD_T3}
_OPEN_ACTIONS = {Action.OPEN_T1}
_MANAGED_LIFECYCLES = {
    Lifecycle.OPEN,
    Lifecycle.REOPEN_PENDING,
}


class ReviewBatchSourceError(RuntimeError):
    """The batch inputs cannot safely be aligned to one account state."""


class ReconciledReviewBatchSource:
    """Compose input snapshots and fail closed on unexplained exchange state.

    The signal source owns universe eligibility and the market-derived entry
    authorizations. Managed positions and pending order symbols are always
    added to its candidate set; missing signals for those symbols become
    invalid observations so they cannot create an action accidentally.
    """

    def __init__(
        self,
        *,
        store: StateStorePort,
        positions: PositionSnapshotPort,
        open_orders: OpenOrdersSnapshotPort,
        signals: ReviewSignalSourcePort,
        policy: StrategyPolicy | None = None,
        clock_ms: Callable[[], int] | None = None,
        future_clock_skew_ms: int = 1_000,
    ) -> None:
        if future_clock_skew_ms < 0:
            raise ValueError("future_clock_skew_ms must be non-negative")
        self._store = store
        self._positions = positions
        self._open_orders = open_orders
        self._signals = signals
        self._policy = policy or StrategyPolicy()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._future_clock_skew_ms = future_clock_skew_ms

    async def load_review_batch(
        self,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
    ) -> ReviewBatch:
        if not account_id or not strategy_version:
            raise ValueError("account_id and strategy_version must not be empty")
        if slot_start_ms < 0 or slot_start_ms % 900_000 != 0:
            raise ValueError("slot_start_ms must be a UTC 15-minute boundary")

        states = {state.symbol: state for state in self._store.list_symbol_states(account_id)}
        active_intents = self._store.list_active_order_intents(account_id)
        intents_by_symbol = {persisted.intent.symbol: persisted for persisted in active_intents}
        if len(intents_by_symbol) != len(active_intents):
            raise ReviewBatchSourceError("multiple active order intents exist for one symbol")
        for symbol in intents_by_symbol:
            if symbol not in states:
                raise ReviewBatchSourceError(
                    f"active order intent for {symbol} has no local symbol state"
                )
        managed_symbols = {
            state.symbol for state in states.values()
            if state.lifecycle in _MANAGED_LIFECYCLES
        }
        managed_symbols.update(intents_by_symbol)

        position_snapshot, open_orders_snapshot, signal_snapshot = await asyncio.gather(
            self._positions.get_position_snapshot(account_id),
            self._open_orders.get_open_orders_snapshot(account_id),
            self._signals.load_review_signals(
                account_id,
                strategy_version,
                slot_start_ms,
                managed_symbols=tuple(sorted(managed_symbols)),
            ),
        )
        self._validate_snapshot_identity(
            account_id=account_id,
            strategy_version=strategy_version,
            slot_start_ms=slot_start_ms,
            positions=position_snapshot,
            open_orders=open_orders_snapshot,
            signals=signal_snapshot,
        )
        now_ms = self._clock_ms()
        for label, sampled_at_ms in (
            ("position", position_snapshot.sampled_at_ms),
            ("open-order", open_orders_snapshot.sampled_at_ms),
            ("signal", signal_snapshot.sampled_at_ms),
        ):
            if sampled_at_ms - now_ms > self._future_clock_skew_ms:
                raise ReviewBatchSourceError(f"{label} snapshot timestamp is in the future")

        positions_by_symbol = self._index_positions(position_snapshot)
        self._reconcile_open_orders(open_orders_snapshot, intents_by_symbol)
        self._reconcile_positions(states, positions_by_symbol, intents_by_symbol)

        signals_by_symbol = {signal.symbol: signal.observation for signal in signal_snapshot.signals}
        candidate_symbols = set(signals_by_symbol)
        candidate_symbols.update(intents_by_symbol)
        candidate_symbols.update(
            symbol for symbol, position in positions_by_symbol.items()
            if position.position_amt != ZERO
        )
        candidate_symbols.update(
            state.symbol for state in states.values()
            if state.lifecycle in _MANAGED_LIFECYCLES
        )

        for symbol in candidate_symbols:
            if symbol not in states:
                if symbol in positions_by_symbol or symbol in intents_by_symbol:
                    raise ReviewBatchSourceError(
                        f"exchange activity for {symbol} has no initialized local state"
                    )
                states[symbol] = self._store.ensure_idle_symbol_state(
                    account_id,
                    symbol,
                    created_at_ms=now_ms,
                )

        if not candidate_symbols:
            raise ReviewBatchSourceError("the signal snapshot produced no review candidates")

        candidates: list[ReviewCandidate] = []
        for symbol in sorted(candidate_symbols):
            observation = signals_by_symbol.get(
                symbol,
                Observation(
                    admitted=False,
                    reopen_authorized=False,
                    valid=False,
                    scale_in_authorized=False,
                ),
            )
            intent = intents_by_symbol.get(symbol)
            priority = (
                self._action_priority(intent.intent.action)
                if intent is not None
                else self._signal_priority(states[symbol], observation)
            )
            candidates.append(
                ReviewCandidate(symbol=symbol, signals=observation, priority=priority)
            )

        return ReviewBatch(
            account_id=account_id,
            strategy_version=strategy_version,
            slot_start_ms=slot_start_ms,
            sampled_at_ms=min(
                position_snapshot.sampled_at_ms,
                open_orders_snapshot.sampled_at_ms,
                signal_snapshot.sampled_at_ms,
            ),
            candidates=tuple(candidates),
        )

    @staticmethod
    def _validate_snapshot_identity(
        *,
        account_id: str,
        strategy_version: str,
        slot_start_ms: int,
        positions: ExchangePositionSnapshot,
        open_orders: ExchangeOpenOrdersSnapshot,
        signals: ReviewSignalSnapshot,
    ) -> None:
        if positions.account_id != account_id:
            raise ReviewBatchSourceError("position snapshot belongs to a different account")
        if open_orders.account_id != account_id:
            raise ReviewBatchSourceError("open-order snapshot belongs to a different account")
        if (
            signals.account_id != account_id
            or signals.strategy_version != strategy_version
            or signals.slot_start_ms != slot_start_ms
        ):
            raise ReviewBatchSourceError("signal snapshot does not match the requested review slot")

    @staticmethod
    def _reconcile_open_orders(
        snapshot: ExchangeOpenOrdersSnapshot,
        intents: dict[str, PersistedOrderIntent],
    ) -> None:
        active_attempts = {
            (intent.intent.symbol, intent.attempt.client_order_id): intent
            for intent in intents.values()
            if intent.attempt is not None
        }
        for order in snapshot.orders:
            if order.algorithmic:
                raise ReviewBatchSourceError(
                    f"unmanaged algorithmic order exists for {order.symbol}; "
                    "the strategy only supports market orders"
                )
            if order.position_side != "BOTH":
                raise ReviewBatchSourceError(
                    f"open order for {order.symbol} uses unsupported position side "
                    f"{order.position_side}"
                )
            intent = active_attempts.get((order.symbol, order.client_order_id))
            if intent is None:
                raise ReviewBatchSourceError(
                    f"unmanaged open order exists for {order.symbol} "
                    f"(order_id={order.exchange_order_id})"
                )
            if intent.intent.action in _OPEN_ACTIONS | _ADD_ACTIONS:
                expected_side = "BUY"
                expected_reduce_only = False
            elif intent.intent.action in _CLOSE_ACTIONS:
                expected_side = "SELL"
                expected_reduce_only = True
            else:
                raise ReviewBatchSourceError(
                    f"active order intent for {order.symbol} has an unsupported action"
                )
            attempt = intent.attempt
            if attempt is None:
                raise ReviewBatchSourceError(
                    f"active order intent for {order.symbol} has no submit attempt"
                )
            if (
                order.order_type != "MARKET"
                or order.side != expected_side
                or order.reduce_only is not expected_reduce_only
                or order.original_qty != attempt.requested_qty
            ):
                raise ReviewBatchSourceError(
                    f"exchange open order for {order.symbol} does not match its persisted intent"
                )

    @staticmethod
    def _index_positions(
        snapshot: ExchangePositionSnapshot,
    ) -> dict[str, ExchangePosition]:
        result: dict[str, ExchangePosition] = {}
        for position in snapshot.positions:
            if position.position_side != "BOTH":
                raise ReviewBatchSourceError(
                    f"{position.symbol} uses unsupported position side {position.position_side}"
                )
            if position.position_amt < ZERO:
                raise ReviewBatchSourceError(f"short position detected for {position.symbol}")
            if position.symbol in result:
                raise ReviewBatchSourceError(
                    f"multiple position rows exist for one-way symbol {position.symbol}"
                )
            result[position.symbol] = position
        return result

    @staticmethod
    def _reconcile_positions(
        states: dict[str, SymbolState],
        positions: dict[str, ExchangePosition],
        intents: dict[str, PersistedOrderIntent],
    ) -> None:
        for symbol, position in positions.items():
            if position.position_amt == ZERO and symbol not in intents:
                raise ReviewBatchSourceError(
                    f"{symbol} was returned with zero quantity, indicating unexplained exchange activity"
                )
            if position.position_amt > ZERO and symbol not in states and symbol not in intents:
                raise ReviewBatchSourceError(
                    f"exchange position for {symbol} has no local strategy state"
                )

        for symbol, state in states.items():
            actual_qty = positions[symbol].position_amt if symbol in positions else ZERO
            intent = intents.get(symbol)
            if intent is not None:
                attempt = intent.attempt
                if attempt is None:
                    minimum_qty = maximum_qty = state.qty
                elif intent.intent.action in _OPEN_ACTIONS | _ADD_ACTIONS:
                    minimum_qty = state.qty
                    maximum_qty = state.qty + attempt.requested_qty
                else:
                    minimum_qty = max(ZERO, state.qty - attempt.requested_qty)
                    maximum_qty = state.qty
                if not minimum_qty <= actual_qty <= maximum_qty:
                    raise ReviewBatchSourceError(
                        f"exchange position for {symbol} is outside the active order range: "
                        f"expected={minimum_qty}..{maximum_qty}, exchange={actual_qty}"
                    )
                continue

            expected_qty = state.qty if state.lifecycle is Lifecycle.OPEN else ZERO
            if actual_qty != expected_qty:
                raise ReviewBatchSourceError(
                    f"position mismatch for {symbol}: local={expected_qty}, exchange={actual_qty}"
                )

    def _signal_priority(
        self,
        state: SymbolState,
        observation: Observation,
    ) -> ReviewPriority:
        action = self._policy.decide(state, observation).action
        if action in _CLOSE_ACTIONS:
            return ReviewPriority.REDUCE
        if action in _ADD_ACTIONS:
            return ReviewPriority.ADD
        if action in _OPEN_ACTIONS:
            return ReviewPriority.OPEN
        return (
            ReviewPriority.REDUCE
            if state.lifecycle is Lifecycle.OPEN
            else ReviewPriority.OPEN
        )

    @staticmethod
    def _action_priority(action: Action) -> ReviewPriority:
        if action in _CLOSE_ACTIONS:
            return ReviewPriority.REDUCE
        if action in _ADD_ACTIONS:
            return ReviewPriority.ADD
        if action in _OPEN_ACTIONS:
            return ReviewPriority.OPEN
        raise ReviewBatchSourceError(f"active intent has unsupported action {action.value}")
