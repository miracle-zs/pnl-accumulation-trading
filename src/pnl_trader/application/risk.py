"""Fail-closed account budget checks for new market-order plans."""

from __future__ import annotations

from decimal import Decimal
import time
from typing import Callable

from ..domain.models import Action, Decision, SymbolState, ZERO
from .execution_models import RiskAuthorization
from .market_models import FifteenMinuteCloseSnapshot, InstrumentRules
from .order_planning import MarketOrderPlan
from .ports import AccountRiskSnapshotPort, MaintenanceMarginBracketPort
from .risk_models import AccountRiskPolicy, AccountRiskSnapshot


_BUY_ACTIONS = {Action.OPEN_T1, Action.ADD_T2, Action.ADD_T3}
_REDUCE_ACTIONS = {
    Action.CLOSE_T1,
    Action.CLOSE_T2_BATCH1,
    Action.CLOSE_T2_FULL,
    Action.CLOSE_T3_TO_ONE_THIRD,
    Action.CLOSE_T3_TO_TWO_THIRDS,
    Action.CLOSE_T3_FULL,
}


class AccountRiskGate:
    """Authorize increases against a fresh account snapshot and reserve ceiling.

    The snapshot provider owns exchange-specific account parsing. Missing,
    stale, mismatched or unhealthy data always denies the requested action.
    Confirmed reduce-only plans do not reserve increase budget, but still need
    a fresh, matching account snapshot.
    """

    def __init__(
        self,
        *,
        snapshots: AccountRiskSnapshotPort,
        policy: AccountRiskPolicy,
        brackets: MaintenanceMarginBracketPort | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._brackets = brackets
        self._policy = policy
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def authorize_order(
        self,
        *,
        account_id: str,
        state: SymbolState,
        decision: Decision,
        plan: MarketOrderPlan,
        price: FifteenMinuteCloseSnapshot,
        rules: InstrumentRules,
    ) -> RiskAuthorization:
        now_ms = self._clock_ms()
        snapshot = await self._snapshots.get_account_risk_snapshot(account_id, state.symbol)
        reason = self._snapshot_rejection(
            snapshot,
            account_id=account_id,
            symbol=state.symbol,
            now_ms=now_ms,
            price=price,
            rules=rules,
            decision=decision,
            plan=plan,
        )
        if reason is not None:
            return self._authorization(
                account_id=account_id,
                state=state,
                plan=plan,
                approved=False,
                reason=reason,
                reserved_margin=ZERO,
                max_active_reserved_margin=ZERO,
                now_ms=now_ms,
            )

        if plan.side == "SELL":
            if snapshot.symbol_position_qty is None:
                return self._denied(
                    account_id,
                    state,
                    plan,
                    now_ms,
                    "account snapshot is missing the symbol position quantity for reduction",
                )
            if snapshot.symbol_position_qty != state.qty:
                return self._denied(
                    account_id,
                    state,
                    plan,
                    now_ms,
                    "exchange position quantity changed since strategy state reconciliation",
                )
            return self._authorization(
                account_id=account_id,
                state=state,
                plan=plan,
                approved=True,
                reason="fresh account snapshot; reduce-only order does not increase margin",
                reserved_margin=ZERO,
                max_active_reserved_margin=ZERO,
                now_ms=now_ms,
            )

        if plan.side != "BUY":
            return self._denied(
                account_id, state, plan, now_ms,
                "order plan has an unsupported side",
            )
        if snapshot.equity <= ZERO:
            return self._denied(
                account_id, state, plan, now_ms,
                "account equity must be positive before increasing exposure",
            )
        if snapshot.margin_type != "CROSSED":
            return self._denied(
                account_id, state, plan, now_ms,
                "symbol is not configured for the supported cross-margin mode",
            )
        if snapshot.multi_assets_mode or snapshot.dual_side_position:
            return self._denied(
                account_id, state, plan, now_ms,
                "account must use single-asset, one-way mode before increasing exposure",
            )

        equity = snapshot.equity
        current_initial_utilization = snapshot.total_initial_margin / equity
        current_maintenance_ratio = snapshot.total_maintenance_margin / equity
        if current_maintenance_ratio > self._policy.max_maintenance_margin_ratio:
            return self._denied(
                account_id, state, plan, now_ms,
                "maintenance margin ratio exceeds the configured increase threshold",
            )
        if current_initial_utilization > self._policy.max_initial_margin_utilization:
            return self._denied(
                account_id, state, plan, now_ms,
                "initial margin utilization already exceeds the configured limit",
            )

        projected_maintenance_ratio, projection_error = await self._project_maintenance_ratio(
            snapshot=snapshot,
            account_id=account_id,
            state=state,
            plan=plan,
            price=price,
            now_ms=now_ms,
        )
        if projection_error is not None:
            return self._denied(account_id, state, plan, now_ms, projection_error)
        if projected_maintenance_ratio is None:
            return self._denied(
                account_id,
                state,
                plan,
                now_ms,
                "projected maintenance margin ratio could not be calculated",
            )
        if projected_maintenance_ratio > self._policy.max_maintenance_margin_ratio:
            return self._denied(
                account_id,
                state,
                plan,
                now_ms,
                "projected maintenance margin ratio "
                f"{projected_maintenance_ratio} exceeds configured limit "
                f"{self._policy.max_maintenance_margin_ratio}",
            )

        margin = plan.estimated_notional / snapshot.effective_leverage
        execution_cost_buffer = plan.estimated_notional * (
            self._policy.taker_fee_rate + self._policy.slippage_buffer_rate
        )
        reservation = margin + execution_cost_buffer
        utilization_capacity = max(
            ZERO,
            equity * self._policy.max_initial_margin_utilization
            - snapshot.total_initial_margin,
        )
        available_capacity = max(
            ZERO,
            snapshot.available_balance - self._policy.reserve_buffer,
        )
        reservation_ceiling = min(utilization_capacity, available_capacity)
        if margin > utilization_capacity:
            return self._denied(
                account_id, state, plan, now_ms,
                "planned initial margin would exceed the configured utilization limit",
            )
        if reservation > reservation_ceiling:
            return self._denied(
                account_id, state, plan, now_ms,
                "planned margin and execution buffer exceed spendable account capacity",
            )
        return self._authorization(
            account_id=account_id,
            state=state,
            plan=plan,
            approved=True,
            reason=(
                "account snapshot and projected order budget are within configured limits; "
                f"projected maintenance margin ratio={projected_maintenance_ratio}, "
                f"limit={self._policy.max_maintenance_margin_ratio}"
            ),
            reserved_margin=reservation,
            max_active_reserved_margin=reservation_ceiling,
            now_ms=now_ms,
        )

    async def _project_maintenance_ratio(
        self,
        *,
        snapshot: AccountRiskSnapshot,
        account_id: str,
        state: SymbolState,
        plan: MarketOrderPlan,
        price: FifteenMinuteCloseSnapshot,
        now_ms: int,
    ) -> tuple[Decimal | None, str | None]:
        if self._brackets is None:
            return None, "maintenance-margin bracket provider is unavailable"
        if (
            snapshot.symbol_position_qty is None
            or snapshot.symbol_maintenance_margin is None
        ):
            return None, "account snapshot is missing the symbol position risk fields"
        if snapshot.symbol_position_qty != state.qty:
            return None, "account position quantity changed since strategy state reconciliation"
        if snapshot.symbol_position_qty < ZERO:
            return None, "short position is unsupported for maintenance-margin projection"
        if snapshot.symbol_maintenance_margin > snapshot.total_maintenance_margin:
            return None, "symbol maintenance margin exceeds the account total"

        bracket_snapshot = await self._brackets.get_maintenance_margin_bracket_snapshot(
            account_id,
            state.symbol,
        )
        if bracket_snapshot.symbol != state.symbol:
            return None, "maintenance-margin brackets do not match the requested symbol"
        bracket_age_ms = now_ms - bracket_snapshot.fetched_at_ms
        if bracket_age_ms > self._policy.max_bracket_age_ms:
            return None, f"maintenance-margin brackets are stale ({bracket_age_ms}ms old)"
        if bracket_age_ms < -self._policy.future_clock_skew_ms:
            return None, "maintenance-margin bracket timestamp is too far in the future"
        if bracket_snapshot.notional_coefficient != Decimal("1"):
            return None, "user-adjusted notional brackets are unsupported for safe projection"

        other_positions_maintenance = (
            snapshot.total_maintenance_margin - snapshot.symbol_maintenance_margin
        )
        execution_cost_buffer = plan.estimated_notional * (
            self._policy.taker_fee_rate + self._policy.slippage_buffer_rate
        )
        projected_equity = snapshot.equity - execution_cost_buffer
        if projected_equity <= ZERO:
            return None, "projected equity is not positive after the execution cost buffer"
        projected_symbol_notional = (
            snapshot.symbol_position_qty * price.close_price
            + plan.estimated_notional * (Decimal("1") + self._policy.slippage_buffer_rate)
        )
        projected_symbol_maintenance = bracket_snapshot.maintenance_margin_for(
            projected_symbol_notional
        )
        if projected_symbol_maintenance is None:
            return (
                None,
                "projected symbol notional is outside the known maintenance-margin brackets",
            )
        projected_total_maintenance = (
            other_positions_maintenance + projected_symbol_maintenance
        )
        return projected_total_maintenance / projected_equity, None

    def _snapshot_rejection(
        self,
        snapshot: AccountRiskSnapshot,
        *,
        account_id: str,
        symbol: str,
        now_ms: int,
        price: FifteenMinuteCloseSnapshot,
        rules: InstrumentRules,
        decision: Decision,
        plan: MarketOrderPlan,
    ) -> str | None:
        if snapshot.account_id != account_id or snapshot.symbol != symbol:
            return "risk snapshot does not match the requested account and symbol"
        age_ms = now_ms - snapshot.fetched_at_ms
        if age_ms > self._policy.max_snapshot_age_ms:
            return f"account risk snapshot is stale ({age_ms}ms old)"
        if age_ms < -self._policy.future_clock_skew_ms:
            return "account risk snapshot timestamp is too far in the future"
        if (
            rules.symbol != symbol
            or price.symbol != symbol
            or decision.action is Action.HOLD
            or plan.source_action is not decision.action
        ):
            return "risk request inputs do not describe one executable symbol plan"
        if plan.side == "BUY" and plan.action not in _BUY_ACTIONS:
            return "increase plan has an unsupported executable action"
        if plan.side == "SELL" and plan.action not in _REDUCE_ACTIONS:
            return "reduce plan has an unsupported executable action"
        if plan.side not in {"BUY", "SELL"}:
            return "risk request has an unsupported order side"
        return None

    def _denied(
        self,
        account_id: str,
        state: SymbolState,
        plan: MarketOrderPlan,
        now_ms: int,
        reason: str,
    ) -> RiskAuthorization:
        return self._authorization(
            account_id=account_id,
            state=state,
            plan=plan,
            approved=False,
            reason=reason,
            reserved_margin=ZERO,
            max_active_reserved_margin=ZERO,
            now_ms=now_ms,
        )

    def _authorization(
        self,
        *,
        account_id: str,
        state: SymbolState,
        plan: MarketOrderPlan,
        approved: bool,
        reason: str,
        reserved_margin: Decimal,
        max_active_reserved_margin: Decimal,
        now_ms: int,
    ) -> RiskAuthorization:
        return RiskAuthorization(
            account_id=account_id,
            symbol=state.symbol,
            state_version=state.version,
            action=plan.action,
            approved=approved,
            reason=reason,
            reserved_margin=reserved_margin,
            max_active_reserved_margin=max_active_reserved_margin,
            valid_until_ms=now_ms + self._policy.authorization_ttl_ms,
        )
