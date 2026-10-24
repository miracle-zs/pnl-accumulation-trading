"""Build auditable, revisioned daily account reports from financial ledgers."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, time as day_time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo

from .daily_report_models import (
    DailyReportInputs,
    DailyReportRevision,
    ReportAccountSnapshot,
    TradeHistoryCoverage,
)
from .income_models import IncomeLedgerRecord
from .trade_history import reconcile_trade_income_records
from .trade_history_models import TradeIncomeReconciliationResult


SHANGHAI = ZoneInfo("Asia/Shanghai")
ZERO = Decimal("0")
BOUNDARY_TOLERANCE_MS = 120_000
CURRENT_SNAPSHOT_MAX_AGE_MS = 20 * 60_000
_RECONCILIATION_ONLY_INCOME_TYPES = {"REALIZED_PNL", "COMMISSION"}
_SUPPORTED_INCOME_TYPES = {
    "TRANSFER",
    "FUNDING_FEE",
    "REALIZED_PNL",
    "COMMISSION",
}


class DailyReportStorePort(Protocol):
    def load_daily_report_inputs(
        self,
        account_id: str,
        strategy_version: str,
        period_start_ms: int,
        period_end_ms: int,
        generated_at_ms: int,
    ) -> DailyReportInputs: ...

    def persist_daily_report_revision(
        self,
        *,
        account_id: str,
        strategy_version: str,
        period_start_ms: int,
        period_end_ms: int,
        status: str,
        content_hash: str,
        payload_json: str,
        generated_at_ms: int,
        notification_channel: str | None = None,
        notify: bool = False,
        notification_payload_json: str | None = None,
    ) -> tuple[int, bool]: ...


class DailyReportBuilder:
    """Hide period selection, ledger classification and revision semantics."""

    def __init__(
        self,
        *,
        store: DailyReportStorePort,
        notification_channel: str | None = None,
    ) -> None:
        self._store = store
        self._notification_channel = notification_channel

    def refresh_latest(
        self,
        account_id: str,
        strategy_version: str,
        *,
        now_ms: int | None = None,
        notify: bool = False,
    ) -> DailyReportRevision:
        if not account_id or not strategy_version:
            raise ValueError("account_id and strategy_version must not be empty")
        generated_at_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        if isinstance(generated_at_ms, bool) or not isinstance(generated_at_ms, int) or generated_at_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")

        period_end_ms = _latest_completed_boundary_ms(generated_at_ms)
        if period_end_ms <= 0:
            raise ValueError("now_ms is before the first daily report boundary")
        period_start_ms = period_end_ms - 86_400_000
        inputs = self._store.load_daily_report_inputs(
            account_id,
            strategy_version,
            period_start_ms,
            period_end_ms,
            generated_at_ms,
        )
        payload = _build_report_payload(inputs)
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        revision, changed = self._store.persist_daily_report_revision(
            account_id=account_id,
            strategy_version=strategy_version,
            period_start_ms=period_start_ms,
            period_end_ms=period_end_ms,
            status=payload["status"],
            content_hash=content_hash,
            payload_json=payload_json,
            generated_at_ms=generated_at_ms,
            notification_channel=self._notification_channel,
            notify=notify,
            notification_payload_json=json.dumps(
                _daily_report_notification_payload(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return DailyReportRevision(
            account_id=account_id,
            strategy_version=strategy_version,
            period_start_ms=period_start_ms,
            period_end_ms=period_end_ms,
            revision=revision,
            status=payload["status"],
            content_hash=content_hash,
            generated_at_ms=generated_at_ms,
            changed=changed,
        )


def _daily_report_notification_payload(report: Mapping[str, object]) -> dict[str, object]:
    period = report.get("period")
    period_label = period.get("label", "前一日") if isinstance(period, dict) else "前一日"
    trading = report.get("trading_result_by_asset")
    net_realized = trading.get("net_realized", {}) if isinstance(trading, dict) else {}
    realized_text = _display_amounts(net_realized)

    account = report.get("current_account")
    unrealized = account.get("unrealized_pnl") if isinstance(account, dict) else None
    equity_asset = account.get("equity_asset", "未知资产") if isinstance(account, dict) else "未知资产"
    margin_safety = account.get("margin_safety_ratio") if isinstance(account, dict) else None
    if isinstance(margin_safety, str):
        try:
            margin_safety_text = f"{Decimal(margin_safety) * Decimal('100'):.2f}%"
        except InvalidOperation:
            margin_safety_text = "不可用"
    else:
        margin_safety_text = "不可用"

    positions = report.get("current_strategy_positions")
    if isinstance(positions, dict):
        tiers = positions.get("tier_counts", {})
        tiers = tiers if isinstance(tiers, dict) else {}
        positions_text = (
            f"{positions.get('held_symbol_count', 0)} 个标的；"
            f"T1 {tiers.get('T1', 0)} / T2 {tiers.get('T2', 0)} / T3 {tiers.get('T3', 0)}"
        )
    else:
        positions_text = "不可用"

    curve = report.get("cumulative_equity_curve")
    points = curve.get("points", []) if isinstance(curve, dict) else []
    curve_coverage = curve.get("coverage", {}) if isinstance(curve, dict) else {}
    curve_is_complete = (
        isinstance(curve_coverage, dict) and curve_coverage.get("complete") is True
    )
    curve_points: list[str] = []
    if isinstance(points, list):
        for point in points[-10:]:
            if not isinstance(point, dict):
                continue
            sample_ms = point.get("sampled_at_ms")
            value = point.get("net_pnl_since_baseline") if curve_is_complete else point.get("equity")
            if not isinstance(sample_ms, int) or value is None:
                continue
            label = datetime.fromtimestamp(sample_ms / 1000, UTC).astimezone(SHANGHAI)
            curve_points.append(f"{label:%m-%d} {value}")
    curve_text = " → ".join(curve_points) if curve_points else "暂无可用曲线数据"
    curve_basis = "累计净收益" if curve_is_complete else "账户权益（覆盖不完整）"

    status = str(report.get("status", "PROVISIONAL"))
    gaps = report.get("coverage", {})
    gap_rows = gaps.get("gaps", []) if isinstance(gaps, dict) else []
    gap_note = f"\n数据缺口：{', '.join(str(item) for item in gap_rows[:5])}" if gap_rows else ""
    floating_text = "不可用" if unrealized is None else f"{unrealized} {equity_asset}"
    body = (
        f"报告区间：{period_label}\n"
        f"状态：{status}\n"
        f"当日已实现净利润：{realized_text}\n"
        f"{curve_basis}曲线：{curve_text}\n"
        f"当前浮动盈亏：{floating_text}\n"
        f"持仓分布：{positions_text}\n"
        f"保证金安全率：{margin_safety_text}"
        f"{gap_note}"
    )
    return {
        "title": "每日账户晨报",
        "body": body,
        "priority": 0,
        "data": {
            "period": period_label,
            "status": status,
            "net_realized_by_asset": net_realized if isinstance(net_realized, dict) else {},
            "cumulative_curve": points[-10:] if isinstance(points, list) else [],
            "unrealized_pnl": unrealized,
            "equity_asset": equity_asset,
            "held_symbol_distribution": positions if isinstance(positions, dict) else {},
            "margin_safety_ratio": margin_safety,
            "gaps": gap_rows,
        },
    }


def _display_amounts(amounts: object) -> str:
    if not isinstance(amounts, dict) or not amounts:
        return "暂无已实现净利润记录"
    return "；".join(f"{asset} {value}" for asset, value in sorted(amounts.items()))


def _latest_completed_boundary_ms(now_ms: int) -> int:
    now = datetime.fromtimestamp(now_ms / 1000, UTC).astimezone(SHANGHAI)
    boundary = datetime.combine(now.date(), day_time(hour=8), tzinfo=SHANGHAI)
    if now < boundary:
        boundary -= timedelta(days=1)
    return int(boundary.astimezone(UTC).timestamp()) * 1000


def _build_report_payload(inputs: DailyReportInputs) -> dict[str, object]:
    start_snapshot = _nearest_snapshot(inputs.account_snapshots, inputs.period_start_ms)
    end_snapshot = _nearest_snapshot(inputs.account_snapshots, inputs.period_end_ms)
    current_snapshot = next(
        (snapshot for snapshot in reversed(inputs.account_snapshots)
         if snapshot.fetched_at_ms <= inputs.generated_at_ms),
        None,
    )
    income_period = tuple(
        record for record in inputs.income_records
        if inputs.period_start_ms <= record.occurred_at_ms < inputs.period_end_ms
    )
    gaps: list[str] = []

    if start_snapshot is None:
        gaps.append("missing_start_boundary_account_snapshot")
    if end_snapshot is None:
        gaps.append("missing_end_boundary_account_snapshot")
    if current_snapshot is None:
        gaps.append("missing_current_account_snapshot")
    elif inputs.generated_at_ms - current_snapshot.fetched_at_ms > CURRENT_SNAPSHOT_MAX_AGE_MS:
        gaps.append("current_account_snapshot_stale")
    if current_snapshot is not None and current_snapshot.equity <= ZERO:
        gaps.append("current_account_equity_non_positive")

    cursor = inputs.income_cursor
    income_complete = bool(
        cursor is not None
        and cursor.coverage_from_ms is not None
        and cursor.coverage_from_ms <= inputs.period_start_ms
        and cursor.reconciled_until_ms >= inputs.period_end_ms - 1
    )
    if not income_complete:
        gaps.append("income_ledger_does_not_cover_period")

    coverage_by_symbol = {row.symbol: row for row in inputs.trade_history_coverage}
    uncovered_symbols = tuple(
        symbol for symbol in inputs.trade_symbols
        if not _coverage_includes_period(
            coverage_by_symbol.get(symbol),
            inputs.period_start_ms,
            inputs.period_end_ms - 1,
        )
    )
    if uncovered_symbols:
        gaps.append("trade_history_incomplete_for_known_symbols")
    reconciliation = reconcile_trade_income_records(
        account_id=inputs.account_id,
        symbols=inputs.trade_symbols,
        start_time_ms=inputs.period_start_ms,
        end_time_ms=inputs.period_end_ms - 1,
        income_covered=income_complete,
        trade_covered=not uncovered_symbols,
        fills=inputs.trade_fills,
        incomes=income_period,
    )

    if start_snapshot is not None and end_snapshot is not None:
        if start_snapshot.multi_assets_mode != end_snapshot.multi_assets_mode:
            gaps.append("account_asset_mode_changed_during_period")
        elif start_snapshot.multi_assets_mode:
            gaps.append("multi_asset_equity_requires_asset_conversion")

    realized_by_asset: Counter[str] = Counter()
    fees_by_asset: Counter[str] = Counter()
    funding_by_asset: Counter[str] = Counter()
    missing_margin_asset_fills = 0
    for fill in inputs.trade_fills:
        if inputs.period_start_ms <= fill.traded_at_ms < inputs.period_end_ms:
            margin_asset = fill.margin_asset or "UNKNOWN"
            realized_by_asset[margin_asset] += fill.realized_pnl
            fees_by_asset[fill.commission_asset] += fill.commission
            if not fill.margin_asset:
                missing_margin_asset_fills += 1
    if missing_margin_asset_fills:
        gaps.append("trade_fills_missing_margin_asset")
    if not reconciliation.coverage_complete:
        gaps.append("trade_income_reconciliation_incomplete")
    if reconciliation.amount_mismatches:
        gaps.append("trade_income_amount_mismatch")

    income_by_type_asset: Counter[tuple[str, str]] = Counter()
    transfer_by_asset: Counter[str] = Counter()
    unclassified_types: set[str] = set()
    for record in income_period:
        income_by_type_asset[(record.income_type, record.asset)] += record.amount
        if record.income_type == "FUNDING_FEE":
            funding_by_asset[record.asset] += record.amount
        elif record.income_type == "TRANSFER":
            transfer_by_asset[record.asset] += record.amount
        elif record.income_type not in _RECONCILIATION_ONLY_INCOME_TYPES:
            unclassified_types.add(record.income_type)
    if unclassified_types:
        gaps.append("income_contains_unclassified_types")

    net_by_asset: dict[str, Decimal] = {}
    for asset in set(realized_by_asset) | set(fees_by_asset) | set(funding_by_asset):
        net_by_asset[asset] = (
            realized_by_asset[asset] - fees_by_asset[asset] + funding_by_asset[asset]
        )

    equity_asset: str | None = None
    equity_change: Decimal | None = None
    transfer_in_equity_asset: Decimal | None = None
    flow_adjusted_equity_change: Decimal | None = None
    if (
        start_snapshot is not None
        and end_snapshot is not None
        and start_snapshot.multi_assets_mode == end_snapshot.multi_assets_mode
    ):
        equity_asset = "USD" if end_snapshot.multi_assets_mode else "USDT"
        equity_change = end_snapshot.equity - start_snapshot.equity
        transfer_in_equity_asset = transfer_by_asset.get(equity_asset, ZERO)
        incompatible_transfer_assets = {
            asset for asset, amount in transfer_by_asset.items()
            if amount != ZERO and asset != equity_asset
        }
        if (
            income_complete
            and not unclassified_types
            and not incompatible_transfer_assets
            and start_snapshot.multi_assets_mode == end_snapshot.multi_assets_mode
            and not end_snapshot.multi_assets_mode
        ):
            flow_adjusted_equity_change = equity_change - transfer_in_equity_asset
        elif incompatible_transfer_assets:
            gaps.append("transfer_assets_do_not_match_equity_unit")

    if transfer_by_asset and set(transfer_by_asset) - {equity_asset}:
        if "transfer_assets_do_not_match_equity_unit" not in gaps:
            gaps.append("transfer_assets_do_not_match_equity_unit")

    current_metrics: dict[str, object] | None = None
    if current_snapshot is not None:
        equity = current_snapshot.equity
        current_metrics = {
            "sampled_at_ms": current_snapshot.fetched_at_ms,
            "age_ms": max(0, inputs.generated_at_ms - current_snapshot.fetched_at_ms),
            "equity": _decimal(current_snapshot.equity),
            "equity_asset": "USD" if current_snapshot.multi_assets_mode else "USDT",
            "wallet_balance": _decimal(current_snapshot.wallet_balance),
            "available_balance": _decimal(current_snapshot.available_balance),
            "unrealized_pnl": _decimal(current_snapshot.unrealized_pnl),
            "initial_margin": _decimal(current_snapshot.initial_margin),
            "maintenance_margin": _decimal(current_snapshot.maintenance_margin),
            "initial_margin_utilization": _decimal(
                current_snapshot.initial_margin / equity if equity > ZERO else None
            ),
            "maintenance_margin_ratio": _decimal(
                current_snapshot.maintenance_margin / equity if equity > ZERO else None
            ),
            "margin_safety_ratio": _decimal(
                Decimal("1") - current_snapshot.maintenance_margin / equity
                if equity > ZERO else None
            ),
        }

    tier_counts = Counter(str(int(state.tier)) for state in inputs.symbol_states if state.qty > ZERO)
    lifecycle_counts = Counter(
        state.lifecycle.value for state in inputs.symbol_states if state.qty > ZERO
    )
    active_counts = dict(inputs.active_intent_counts)
    review_counts = dict(inputs.review_counts)
    skipped_counts = {
        key: value for key, value in sorted(review_counts.items())
        if key.startswith("skipped")
    }
    symbol_failure_count = review_counts.get("symbol_failures", 0)

    cumulative = _build_cumulative_curve(inputs)
    if not cumulative["coverage"]["complete"]:
        gaps.append("cumulative_equity_curve_incomplete")
    status = "COMPLETE_WITHIN_KNOWN_SCOPE" if not gaps else "PROVISIONAL"
    return {
        "schema_version": 1,
        "account_id": inputs.account_id,
        "strategy_version": inputs.strategy_version,
        "status": status,
        "period": {
            "start_ms": inputs.period_start_ms,
            "end_ms": inputs.period_end_ms,
            "timezone": "Asia/Shanghai",
            "label": _period_label(inputs.period_start_ms, inputs.period_end_ms),
            "interval": "[start, end)",
        },
        "equity": {
            "asset": equity_asset,
            "start": _snapshot_summary(start_snapshot),
            "end": _snapshot_summary(end_snapshot),
            "change": _decimal(equity_change),
            "net_transfer_by_asset": _amount_map(transfer_by_asset),
            "net_transfer_in_equity_asset": _decimal(transfer_in_equity_asset),
            "change_after_transfers": _decimal(flow_adjusted_equity_change),
        },
        "trading_result_by_asset": {
            "realized_pnl": _amount_map(realized_by_asset),
            "fees": _amount_map(fees_by_asset),
            "funding_fee": _amount_map(funding_by_asset),
            "net_realized": _amount_map(net_by_asset),
        },
        "trade_income_reconciliation": {
            "coverage_complete": reconciliation.coverage_complete,
            "matched_commission_count": reconciliation.matched_commission_count,
            "matched_realized_pnl_count": reconciliation.matched_realized_pnl_count,
            "unmatched_trade_event_count": reconciliation.unmatched_trade_event_count,
            "unmatched_income_event_count": reconciliation.unmatched_income_event_count,
            "amount_mismatches": [
                {
                    "income_type": item.income_type,
                    "symbol": item.symbol,
                    "trade_id": item.trade_id,
                    "asset": item.asset,
                    "trade_amount": item.trade_amount,
                    "income_amount": item.income_amount,
                }
                for item in reconciliation.amount_mismatches
            ],
        },
        "income_ledger_by_type_asset": {
            f"{income_type}:{asset}": _decimal(amount)
            for (income_type, asset), amount in sorted(income_by_type_asset.items())
        },
        "current_account": current_metrics,
        "current_strategy_positions": {
            "held_symbol_count": sum(tier_counts.values()),
            "tier_counts": {
                "T1": tier_counts.get("1", 0),
                "T2": tier_counts.get("2", 0),
                "T3": tier_counts.get("3", 0),
            },
            "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
            "basis": "local strategy state; not an exchange position reconciliation",
        },
        "review_activity": {
            "skipped_items_by_reason": skipped_counts,
            "symbol_failures": symbol_failure_count,
        },
        "orders": {
            "active_intents_by_status": dict(sorted(active_counts.items())),
            "unknown_order_count": active_counts.get("UNKNOWN", 0),
        },
        "coverage": {
            "income": {
                "complete_for_period": income_complete,
                "coverage_from_ms": None if cursor is None else cursor.coverage_from_ms,
                "reconciled_until_ms": None if cursor is None else cursor.reconciled_until_ms,
            },
            "trade_history": {
                "account_wide_complete": False,
                "complete_for_known_symbols": not uncovered_symbols,
                "known_symbol_count": len(inputs.trade_symbols),
                "uncovered_symbols": list(uncovered_symbols),
                "scope": "symbols in the period universe snapshot plus locally observed account symbols",
                "limitation": "unobserved delisted symbols cannot be proven covered",
            },
            "cumulative_curve": cumulative["coverage"],
            "unclassified_income_types": sorted(unclassified_types),
            "gaps": sorted(set(gaps)),
        },
        "cumulative_equity_curve": cumulative["points"],
    }


def _build_cumulative_curve(inputs: DailyReportInputs) -> dict[str, object]:
    cursor = inputs.income_cursor
    eligible = tuple(
        snapshot for snapshot in inputs.account_snapshots
        if snapshot.fetched_at_ms <= inputs.generated_at_ms
        and cursor is not None
        and cursor.coverage_from_ms is not None
        and snapshot.fetched_at_ms >= cursor.coverage_from_ms
    )
    if not eligible:
        return {"coverage": {"complete": False, "reason": "no_snapshot_inside_income_coverage"}, "points": []}
    baseline = eligible[0]
    latest = eligible[-1]
    complete = bool(
        cursor is not None
        and cursor.reconciled_until_ms >= latest.fetched_at_ms
        and not (latest.multi_assets_mode or baseline.multi_assets_mode)
        and latest.multi_assets_mode == baseline.multi_assets_mode
    )
    selected_types = {
        record.income_type for record in inputs.income_records
        if baseline.fetched_at_ms <= record.occurred_at_ms <= latest.fetched_at_ms
    }
    unknown_types = selected_types - _SUPPORTED_INCOME_TYPES
    if unknown_types:
        complete = False
    transfers = tuple(
        record for record in inputs.income_records
        if record.income_type == "TRANSFER"
        and baseline.fetched_at_ms <= record.occurred_at_ms <= latest.fetched_at_ms
    )
    base_asset = "USD" if baseline.multi_assets_mode else "USDT"
    incompatible_assets = {record.asset for record in transfers if record.asset != base_asset}
    if incompatible_assets:
        complete = False
    points: list[dict[str, object]] = []
    boundaries: list[int] = []
    missing_boundaries: list[int] = []
    local_start = datetime.fromtimestamp(baseline.fetched_at_ms / 1000, UTC).astimezone(SHANGHAI)
    next_day = datetime.combine(local_start.date(), day_time(hour=8), tzinfo=SHANGHAI)
    if int(next_day.astimezone(UTC).timestamp()) * 1000 < baseline.fetched_at_ms:
        next_day += timedelta(days=1)
    while int(next_day.astimezone(UTC).timestamp()) * 1000 <= latest.fetched_at_ms:
        boundaries.append(int(next_day.astimezone(UTC).timestamp()) * 1000)
        next_day += timedelta(days=1)
    samples = [baseline]
    for boundary in boundaries:
        candidate = _nearest_snapshot(eligible, boundary)
        if candidate is None:
            missing_boundaries.append(boundary)
            continue
        if candidate.fetched_at_ms != samples[-1].fetched_at_ms:
            samples.append(candidate)
    if missing_boundaries:
        complete = False
    if latest.fetched_at_ms != samples[-1].fetched_at_ms:
        samples.append(latest)
    for snapshot in samples:
        transfer_total = sum(
            (record.amount for record in transfers
             if record.asset == base_asset
             and baseline.fetched_at_ms < record.occurred_at_ms <= snapshot.fetched_at_ms),
            ZERO,
        )
        net = snapshot.equity - baseline.equity - transfer_total
        points.append({
            "sampled_at_ms": snapshot.fetched_at_ms,
            "equity": _decimal(snapshot.equity),
            "net_pnl_since_baseline": _decimal(net) if complete else None,
            "equity_asset": "USD" if snapshot.multi_assets_mode else "USDT",
        })
    return {
        "coverage": {
            "complete": complete,
            "baseline_at_ms": baseline.fetched_at_ms,
            "baseline_equity": _decimal(baseline.equity),
            "equity_asset": base_asset,
            "income_coverage_from_ms": None if cursor is None else cursor.coverage_from_ms,
            "income_reconciled_until_ms": None if cursor is None else cursor.reconciled_until_ms,
            "unclassified_income_types": sorted(unknown_types),
            "incompatible_transfer_assets": sorted(incompatible_assets),
            "missing_daily_snapshot_boundaries_ms": missing_boundaries,
        },
        "points": points,
    }


def _nearest_snapshot(
    snapshots: tuple[ReportAccountSnapshot, ...],
    target_ms: int,
) -> ReportAccountSnapshot | None:
    candidates = (
        snapshot for snapshot in snapshots
        if abs(snapshot.fetched_at_ms - target_ms) <= BOUNDARY_TOLERANCE_MS
    )
    return min(candidates, key=lambda snapshot: (abs(snapshot.fetched_at_ms - target_ms), snapshot.fetched_at_ms), default=None)


def _coverage_includes_period(
    coverage: TradeHistoryCoverage | None,
    start_ms: int,
    end_ms: int,
) -> bool:
    return bool(
        coverage is not None
        and coverage.coverage_from_ms is not None
        and coverage.reconciled_until_ms is not None
        and coverage.coverage_from_ms <= start_ms
        and coverage.reconciled_until_ms >= end_ms
    )


def _snapshot_summary(snapshot: ReportAccountSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "sampled_at_ms": snapshot.fetched_at_ms,
        "equity": _decimal(snapshot.equity),
        "wallet_balance": _decimal(snapshot.wallet_balance),
        "unrealized_pnl": _decimal(snapshot.unrealized_pnl),
        "available_balance": _decimal(snapshot.available_balance),
        "initial_margin": _decimal(snapshot.initial_margin),
        "maintenance_margin": _decimal(snapshot.maintenance_margin),
        "equity_asset": "USD" if snapshot.multi_assets_mode else "USDT",
    }


def _period_label(start_ms: int, end_ms: int) -> str:
    start = datetime.fromtimestamp(start_ms / 1000, UTC).astimezone(SHANGHAI)
    end = datetime.fromtimestamp(end_ms / 1000, UTC).astimezone(SHANGHAI)
    return f"{start:%Y-%m-%d %H:%M}–{end:%Y-%m-%d %H:%M} Asia/Shanghai"


def _amount_map(amounts: Counter[str] | dict[str, Decimal]) -> dict[str, str]:
    return {asset: _decimal(amount) or "0" for asset, amount in sorted(amounts.items())}


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
