"""Deterministic Tier decisions for one periodic market observation."""

from __future__ import annotations

from decimal import Decimal

from .models import Action, Decision, Lifecycle, Observation, SymbolState, Tier, ZERO


T1_TAKE_PROFIT = Decimal("0.10")
T2_BATCH1_TAKE_PROFIT = Decimal("0.20")
T2_FULL_TAKE_PROFIT = Decimal("0.50")
T3_BATCH1_TAKE_PROFIT = Decimal("0.20")
T3_BATCH2_TAKE_PROFIT = Decimal("0.50")
T3_FULL_TAKE_PROFIT = Decimal("1.00")
DCA_DROP = Decimal("0.50")
T1_NOTIONAL = Decimal("10.0")
T2_NOTIONAL = Decimal("20.0")
T3_NOTIONAL = Decimal("60.0")
T3_WRITEOFF_VALUE_LIMIT = Decimal("5.0")


class StrategyPolicy:
    """Map an aligned position and one latest-price observation to an intent."""

    def decide(self, state: SymbolState, observation: Observation) -> Decision:
        price = observation.price

        if state.lifecycle in {Lifecycle.IDLE, Lifecycle.REOPEN_PENDING}:
            if observation.admitted:
                return Decision(
                    Action.OPEN_T1,
                    buy_notional=T1_NOTIONAL,
                )
            return Decision(Action.HOLD)

        ret = (price - state.avg_entry_price) / state.avg_entry_price
        dca_price = state.tier_anchor_price * (Decimal("1") - DCA_DROP)

        if state.tier is Tier.T1:
            if ret >= T1_TAKE_PROFIT:
                return self._close_all(state, Action.CLOSE_T1)
            if price <= dca_price and observation.admitted:
                return Decision(
                    Action.ADD_T2,
                    buy_notional=T2_NOTIONAL,
                )
            return Decision(Action.HOLD)

        if state.tier is Tier.T2:
            if ret >= T2_FULL_TAKE_PROFIT:
                return self._close_all(state, Action.CLOSE_T2_FULL)
            if ret >= T2_BATCH1_TAKE_PROFIT and state.completed_batches == 0:
                target = state.tier_entry_qty / Decimal("2")
                return self._close_to_target(
                    state,
                    Action.CLOSE_T2_BATCH1,
                    target,
                )
            if price <= dca_price and observation.admitted:
                return Decision(
                    Action.ADD_T3,
                    buy_notional=T3_NOTIONAL,
                )
            return Decision(Action.HOLD)

        if state.tier is Tier.T3:
            writeoff_price = T3_WRITEOFF_VALUE_LIMIT / state.tier_entry_qty
            if price <= writeoff_price or ret >= T3_FULL_TAKE_PROFIT:
                return self._close_all(state, Action.CLOSE_T3_FULL)
            if ret >= T3_BATCH2_TAKE_PROFIT and state.completed_batches < 2:
                target = state.tier_entry_qty * Decimal("2") / Decimal("3")
                return self._close_to_target(
                    state,
                    Action.CLOSE_T3_TO_TWO_THIRDS,
                    target,
                )
            if ret >= T3_BATCH1_TAKE_PROFIT and state.completed_batches == 0:
                target = state.tier_entry_qty / Decimal("3")
                return self._close_to_target(
                    state,
                    Action.CLOSE_T3_TO_ONE_THIRD,
                    target,
                )
            return Decision(Action.HOLD)

        raise ValueError("strategy policy received an unsupported position state")

    @staticmethod
    def _close_all(state: SymbolState, action: Action) -> Decision:
        return StrategyPolicy._close_to_target(state, action, state.tier_entry_qty)

    @staticmethod
    def _close_to_target(
        state: SymbolState,
        action: Action,
        cumulative_target: Decimal,
    ) -> Decision:
        close_qty = cumulative_target - state.tier_sold_qty
        if close_qty <= ZERO:
            return Decision(Action.HOLD)
        return Decision(action, close_qty=close_qty)
