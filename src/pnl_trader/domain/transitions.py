"""Apply reconciled terminal order results to the position state."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .models import (
    Action,
    ExecutionReceipt,
    InvalidExecution,
    Lifecycle,
    SymbolState,
    Tier,
    ZERO,
)


_BUY_TARGETS = {
    Action.OPEN_T1: Tier.T1,
    Action.ADD_T2: Tier.T2,
    Action.ADD_T3: Tier.T3,
}

_CLOSE_TIERS = {
    Action.CLOSE_T1: Tier.T1,
    Action.CLOSE_T2_BATCH1: Tier.T2,
    Action.CLOSE_T2_FULL: Tier.T2,
    Action.CLOSE_T3_TO_ONE_THIRD: Tier.T3,
    Action.CLOSE_T3_TO_TWO_THIRDS: Tier.T3,
    Action.CLOSE_T3_FULL: Tier.T3,
}


def apply_confirmed_execution(
    state: SymbolState,
    receipt: ExecutionReceipt,
) -> SymbolState:
    """Return the next state after one terminal, reconciled order result.

    The caller owns persistence and must apply this function and store its
    returned value atomically with the receipt's applied-cumulative-fill cursor.
    """

    if not receipt.order_terminal:
        raise InvalidExecution("only terminal order results can advance Tier state")
    if receipt.filled_qty < ZERO or receipt.quote_qty < ZERO:
        raise InvalidExecution("fill quantities must be non-negative")
    if receipt.filled_qty == ZERO:
        if receipt.quote_qty != ZERO:
            raise InvalidExecution("a zero-quantity result must have zero quote quantity")
        return state
    if receipt.quote_qty <= ZERO:
        raise InvalidExecution("a positive fill must have positive quote quantity")

    if receipt.action in _BUY_TARGETS:
        return _apply_buy(state, receipt, _BUY_TARGETS[receipt.action])
    if receipt.action in _CLOSE_TIERS:
        return _apply_sell(state, receipt, _CLOSE_TIERS[receipt.action])
    raise InvalidExecution(f"action {receipt.action.value} cannot have an execution fill")


def _apply_buy(
    state: SymbolState,
    receipt: ExecutionReceipt,
    target_tier: Tier,
) -> SymbolState:
    fill_price = receipt.quote_qty / receipt.filled_qty

    if target_tier is Tier.T1:
        if state.lifecycle not in {Lifecycle.IDLE, Lifecycle.REOPEN_PENDING}:
            raise InvalidExecution("T1 can only open an idle or confirmed-flat symbol")
        return SymbolState(
            symbol=state.symbol,
            cycle_id=state.cycle_id + 1,
            tier=Tier.T1,
            lifecycle=Lifecycle.OPEN,
            qty=receipt.filled_qty,
            avg_entry_price=fill_price,
            tier_entry_qty=receipt.filled_qty,
            tier_anchor_price=fill_price,
            version=state.version + 1,
        )

    expected_prior_tier = Tier(target_tier - 1)
    if state.lifecycle not in {Lifecycle.OPEN, Lifecycle.QUARANTINED}:
        raise InvalidExecution("DCA fill requires a known open position")
    if state.tier is not expected_prior_tier:
        raise InvalidExecution(
            f"{target_tier.name} fill requires {expected_prior_tier.name} state, found {state.tier.name}"
        )

    new_qty = state.qty + receipt.filled_qty
    new_average = (state.qty * state.avg_entry_price + receipt.quote_qty) / new_qty
    return replace(
        state,
        tier=target_tier,
        qty=new_qty,
        avg_entry_price=new_average,
        tier_entry_qty=new_qty,
        tier_anchor_price=fill_price,
        tier_sold_qty=ZERO,
        completed_batches=0,
        version=state.version + 1,
    )


def _apply_sell(
    state: SymbolState,
    receipt: ExecutionReceipt,
    expected_tier: Tier,
) -> SymbolState:
    if state.lifecycle not in {Lifecycle.OPEN, Lifecycle.QUARANTINED}:
        raise InvalidExecution("close fill requires a known open position")
    if state.tier is not expected_tier:
        raise InvalidExecution(
            f"{receipt.action.value} requires {expected_tier.name} state, found {state.tier.name}"
        )
    if receipt.filled_qty > state.qty:
        raise InvalidExecution("sell fill exceeds the known long position")

    new_qty = state.qty - receipt.filled_qty
    new_sold_qty = state.tier_sold_qty + receipt.filled_qty

    if new_qty == ZERO:
        if state.lifecycle is Lifecycle.QUARANTINED:
            return SymbolState(
                symbol=state.symbol,
                cycle_id=state.cycle_id,
                lifecycle=Lifecycle.QUARANTINED,
                version=state.version + 1,
            )
        return SymbolState(
            symbol=state.symbol,
            cycle_id=state.cycle_id,
            lifecycle=Lifecycle.REOPEN_PENDING,
            version=state.version + 1,
        )

    completed_batches = state.completed_batches
    target = receipt.target_cumulative_sold_qty
    if receipt.action in {Action.CLOSE_T2_BATCH1, Action.CLOSE_T3_TO_ONE_THIRD, Action.CLOSE_T3_TO_TWO_THIRDS}:
        if target is None or target <= ZERO:
            raise InvalidExecution("a batch close requires its exchange-aligned cumulative target")
        if target <= state.tier_sold_qty:
            raise InvalidExecution("a batch close target must exceed the quantity already sold")
        if target > state.tier_entry_qty:
            raise InvalidExecution("a batch close target exceeds the Tier entry quantity")
        if new_sold_qty >= target:
            if receipt.action is Action.CLOSE_T2_BATCH1:
                completed_batches = 1
            elif receipt.action is Action.CLOSE_T3_TO_ONE_THIRD:
                completed_batches = max(completed_batches, 1)
            else:
                completed_batches = 2

    return replace(
        state,
        qty=new_qty,
        tier_sold_qty=new_sold_qty,
        completed_batches=completed_batches,
        version=state.version + 1,
    )
