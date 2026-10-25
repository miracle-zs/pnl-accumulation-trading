"""Deterministic Tier decisions for one periodic market observation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Action, Decision, Lifecycle, Observation, SymbolState, Tier, ZERO


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    t1_take_profit: Decimal = Decimal("0.10")
    t2_batch1_take_profit: Decimal = Decimal("0.20")
    t2_full_take_profit: Decimal = Decimal("0.50")
    t3_batch1_take_profit: Decimal = Decimal("0.20")
    t3_batch2_take_profit: Decimal = Decimal("0.50")
    t3_full_take_profit: Decimal = Decimal("1.00")
    dca_drop: Decimal = Decimal("0.50")
    t1_notional: Decimal = Decimal("10.0")
    t2_notional: Decimal = Decimal("20.0")
    t3_notional: Decimal = Decimal("60.0")
    t3_writeoff_value_limit: Decimal = Decimal("5.0")

    def __post_init__(self) -> None:
        names = (
            "t1_take_profit",
            "t2_batch1_take_profit",
            "t2_full_take_profit",
            "t3_batch1_take_profit",
            "t3_batch2_take_profit",
            "t3_full_take_profit",
            "dca_drop",
            "t1_notional",
            "t2_notional",
            "t3_notional",
            "t3_writeoff_value_limit",
        )
        for name in names:
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be positive and finite")
        if self.dca_drop >= Decimal("1"):
            raise ValueError("dca_drop must be less than 1")
        if self.t2_full_take_profit <= self.t2_batch1_take_profit:
            raise ValueError("T2 full take-profit must exceed its first batch")
        if not self.t3_batch1_take_profit < self.t3_batch2_take_profit < self.t3_full_take_profit:
            raise ValueError("T3 take-profit thresholds must increase by batch")


class StrategyPolicy:
    """Map an aligned position and one valid observation to a single intent."""

    def __init__(self, config: StrategyConfig = StrategyConfig()) -> None:
        self._config = config

    def decide(self, state: SymbolState, observation: Observation) -> Decision:
        if not observation.valid:
            return Decision(Action.HOLD, "observation is not valid")
        price = (
            observation.close_price
            if observation.close_price is not None
            else observation.mark_price
        )
        if price is None or price <= ZERO:
            return Decision(Action.HOLD, "15-minute close price is missing or non-positive")

        if state.lifecycle is Lifecycle.IDLE:
            if observation.admitted:
                return Decision(
                    Action.OPEN_T1,
                    "eligible idle symbol",
                    buy_notional=self._config.t1_notional,
                )
            return Decision(Action.HOLD, "symbol is not admitted to the universe")

        if state.lifecycle is Lifecycle.REOPEN_PENDING:
            if observation.admitted and observation.reopen_authorized:
                return Decision(
                    Action.OPEN_T1,
                    "position is flat and reopen passed risk authorization",
                    buy_notional=self._config.t1_notional,
                )
            return Decision(Action.HOLD, "reopen is not authorized")

        ret = (price - state.avg_entry_price) / state.avg_entry_price
        dca_price = state.tier_anchor_price * (Decimal("1") - self._config.dca_drop)

        if state.tier is Tier.T1:
            if ret >= self._config.t1_take_profit:
                return self._close_all(state, Action.CLOSE_T1, "T1 take-profit reached")
            if price <= dca_price:
                if not observation.scale_in_authorized:
                    return Decision(Action.HOLD, "additional capital is not authorized for this symbol")
                return Decision(
                    Action.ADD_T2,
                    "price reached the T1 DCA threshold",
                    buy_notional=self._config.t2_notional,
                )
            return Decision(Action.HOLD, "T1 thresholds are not reached")

        if state.tier is Tier.T2:
            if ret >= self._config.t2_full_take_profit:
                return self._close_all(state, Action.CLOSE_T2_FULL, "T2 full take-profit reached")
            if ret >= self._config.t2_batch1_take_profit and state.completed_batches == 0:
                target = state.tier_entry_qty / Decimal("2")
                return self._close_to_target(
                    state,
                    Action.CLOSE_T2_BATCH1,
                    target,
                    "T2 first take-profit batch reached",
                )
            if price <= dca_price:
                if not observation.scale_in_authorized:
                    return Decision(Action.HOLD, "additional capital is not authorized for this symbol")
                return Decision(
                    Action.ADD_T3,
                    "price reached the T2 DCA threshold",
                    buy_notional=self._config.t3_notional,
                )
            return Decision(Action.HOLD, "T2 thresholds are not reached")

        if state.tier is Tier.T3:
            writeoff_price = (
                self._config.t3_writeoff_value_limit / state.tier_entry_qty
            )
            if price <= writeoff_price:
                return self._close_all(
                    state,
                    Action.CLOSE_T3_FULL,
                    "T3 write-off threshold reached; close and restart at T1",
                )
            if ret >= self._config.t3_full_take_profit:
                return self._close_all(state, Action.CLOSE_T3_FULL, "T3 full take-profit reached")
            if ret >= self._config.t3_batch2_take_profit and state.completed_batches < 2:
                target = state.tier_entry_qty * Decimal("2") / Decimal("3")
                return self._close_to_target(
                    state,
                    Action.CLOSE_T3_TO_TWO_THIRDS,
                    target,
                    "T3 cumulative two-thirds take-profit reached",
                )
            if ret >= self._config.t3_batch1_take_profit and state.completed_batches == 0:
                target = state.tier_entry_qty / Decimal("3")
                return self._close_to_target(
                    state,
                    Action.CLOSE_T3_TO_ONE_THIRD,
                    target,
                    "T3 first take-profit batch reached",
                )
            return Decision(Action.HOLD, "T3 thresholds are not reached; Tier 3 is capped")

        return Decision(Action.HOLD, "position has no supported Tier")

    @staticmethod
    def _close_all(state: SymbolState, action: Action, reason: str) -> Decision:
        return StrategyPolicy._close_to_target(state, action, state.tier_entry_qty, reason)

    @staticmethod
    def _close_to_target(
        state: SymbolState,
        action: Action,
        cumulative_target: Decimal,
        reason: str,
    ) -> Decision:
        close_qty = max(ZERO, cumulative_target - state.tier_sold_qty)
        if close_qty == ZERO:
            return Decision(Action.HOLD, "the cumulative close target is already met")
        return Decision(action, reason, close_qty=close_qty)
