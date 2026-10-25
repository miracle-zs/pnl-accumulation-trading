"""Apply reconciled terminal order results to the position state."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .models import (
    Action,
    BATCH_CLOSE_ACTIONS,
    BUY_ACTION_TIERS,
    CLOSE_ACTION_TIERS,
    ExecutionReceipt,
    FULL_CLOSE_ACTIONS,
    InvalidExecution,
    Lifecycle,
    SymbolState,
    Tier,
    ZERO,
)


def validate_execution_intent(
    state: SymbolState,
    action: Action,
    target_cumulative_sold_qty: Decimal | None,
) -> None:
    """Validate an action against the persisted position before submitting it."""

    target_tier = BUY_ACTION_TIERS.get(action)
    if target_tier is not None:
        if target_cumulative_sold_qty is not None:
            raise InvalidExecution("buy actions cannot carry a cumulative sell target")
        if target_tier is Tier.T1:
            if state.lifecycle not in {Lifecycle.IDLE, Lifecycle.REOPEN_PENDING}:
                raise InvalidExecution("T1 can only open an idle or confirmed-flat symbol")
        elif (
            state.lifecycle is not Lifecycle.OPEN
            or state.tier is not Tier(target_tier - 1)
        ):
            raise InvalidExecution(
                f"{target_tier.name} fill requires {Tier(target_tier - 1).name} state"
            )
        return

    expected_tier = CLOSE_ACTION_TIERS.get(action)
    if expected_tier is None:
        raise InvalidExecution(f"action {action.value} cannot create an order intent")
    if state.lifecycle is not Lifecycle.OPEN or state.tier is not expected_tier:
        raise InvalidExecution(f"{action.value} requires an open {expected_tier.name} state")
    if target_cumulative_sold_qty is None:
        raise InvalidExecution("a close intent requires its cumulative sell target")
    if not state.tier_sold_qty < target_cumulative_sold_qty <= state.tier_entry_qty:
        raise InvalidExecution("close target is outside the unsold Tier quantity")
    if (
        action in FULL_CLOSE_ACTIONS
        and target_cumulative_sold_qty != state.tier_entry_qty
    ):
        raise InvalidExecution("full close target must equal the Tier entry quantity")


def apply_confirmed_execution(
    state: SymbolState,
    receipt: ExecutionReceipt,
) -> SymbolState:
    """Return the next state after one terminal, reconciled order result.

    The caller owns persistence and must store the returned value atomically
    with the order's fills and terminal status.
    """

    if receipt.filled_qty < ZERO or receipt.quote_qty < ZERO:
        raise InvalidExecution("fill quantities must be non-negative")
    if receipt.filled_qty == ZERO:
        if receipt.quote_qty != ZERO:
            raise InvalidExecution("a zero-quantity result must have zero quote quantity")
        return state
    if receipt.quote_qty <= ZERO:
        raise InvalidExecution("a positive fill must have positive quote quantity")

    validate_execution_intent(
        state,
        receipt.action,
        receipt.target_cumulative_sold_qty,
    )
    if receipt.action in BUY_ACTION_TIERS:
        return _apply_buy(state, receipt, BUY_ACTION_TIERS[receipt.action])
    return _apply_sell(state, receipt)


def _apply_buy(
    state: SymbolState,
    receipt: ExecutionReceipt,
    target_tier: Tier,
) -> SymbolState:
    fill_price = receipt.quote_qty / receipt.filled_qty

    if target_tier is Tier.T1:
        return SymbolState(
            symbol=state.symbol,
            tier=Tier.T1,
            lifecycle=Lifecycle.OPEN,
            qty=receipt.filled_qty,
            avg_entry_price=fill_price,
            tier_entry_qty=receipt.filled_qty,
            tier_anchor_price=fill_price,
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
        completed_batches=0,
    )


def _apply_sell(
    state: SymbolState,
    receipt: ExecutionReceipt,
) -> SymbolState:
    if receipt.filled_qty > state.qty:
        raise InvalidExecution("sell fill exceeds the known long position")

    new_qty = state.qty - receipt.filled_qty
    new_sold_qty = state.tier_sold_qty + receipt.filled_qty
    target = receipt.target_cumulative_sold_qty
    if target is None:
        raise InvalidExecution("a close execution requires its cumulative sell target")
    if new_sold_qty > target:
        raise InvalidExecution("confirmed sell exceeds its cumulative target")

    if new_qty == ZERO:
        return SymbolState(
            symbol=state.symbol,
            lifecycle=Lifecycle.REOPEN_PENDING,
        )

    completed_batches = state.completed_batches
    if receipt.action in BATCH_CLOSE_ACTIONS:
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
        completed_batches=completed_batches,
    )
