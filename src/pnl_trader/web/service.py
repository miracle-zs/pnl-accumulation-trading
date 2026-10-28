"""Read-only query service for the Web Dashboard reading state.sqlite3."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time
from typing import Any

from ..domain.models import FULL_CLOSE_ACTIONS
from ..domain.policy import (
    DCA_DROP,
    T1_TAKE_PROFIT,
    T2_BATCH1_TAKE_PROFIT,
    T2_FULL_TAKE_PROFIT,
    T3_BATCH1_TAKE_PROFIT,
    T3_BATCH2_TAKE_PROFIT,
    T3_FULL_TAKE_PROFIT,
    T3_WRITEOFF_VALUE_LIMIT,
)


def _get_readonly_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_EQUITY_PERIOD_MS = {
    "24h": 24 * 60 * 60 * 1_000,
    "7d": 7 * 24 * 60 * 60 * 1_000,
    "30d": 30 * 24 * 60 * 60 * 1_000,
}
_BEIJING_TIMEZONE = timezone(timedelta(hours=8))


class DashboardQueryService:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def get_overview(self) -> dict[str, Any]:
        if not self._db_path.is_file():
            return {
                "status": "WAITING_DATA",
                "realized_cash": None,
                "realized_gross": None,
                "total_commission": None,
                "other_fee_count": None,
                "pending_close_fill_count": None,
                "fill_count": None,
                "active_positions_count": None,
                "last_fill_at_ms": None,
            }

        with closing(_get_readonly_connection(self._db_path)) as conn:
            cur = conn.cursor()

            # Binance reports realized PnL and commissions separately. Only
            # USDT commissions can be subtracted directly from USDT PnL.
            cur.execute("""
                SELECT
                    COALESCE(SUM(CAST(realized_pnl AS REAL)), 0.0) AS realized_gross,
                    COALESCE(SUM(CASE WHEN commission_asset = 'USDT'
                        THEN CAST(commission AS REAL) ELSE 0.0 END), 0.0) AS commission_usdt,
                    COALESCE(SUM(CASE WHEN commission_asset != 'USDT'
                        AND CAST(commission AS REAL) != 0.0 THEN 1 ELSE 0 END), 0) AS other_fee_count,
                    COUNT(*) AS fill_count,
                    MAX(traded_at_ms) AS last_fill_at_ms
                FROM trade_fills
            """)
            fill_row = cur.fetchone()
            realized_gross = fill_row["realized_gross"]
            commission_usdt = fill_row["commission_usdt"]
            other_fee_count = fill_row["other_fee_count"]
            fill_count = fill_row["fill_count"]
            last_fill_at_ms = fill_row["last_fill_at_ms"]

            # Active positions are local strategy state, not a claim about
            # current exchange positions.
            cur.execute("SELECT COUNT(*) FROM symbol_states WHERE lifecycle IN ('OPEN', 'CLOSING')")
            active_count = cur.fetchone()[0]

            actions = tuple(action.value for action in FULL_CLOSE_ACTIONS)
            placeholders = ", ".join("?" for _ in actions)
            cur.execute(
                f"""SELECT COUNT(*) FROM order_intents
                    WHERE status = 'FILLED' AND action IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM trade_fills AS f
                          WHERE f.intent_id = order_intents.intent_id
                      )""",
                actions,
            )
            pending_close_fill_count = cur.fetchone()[0]

            realized_net = (
                realized_gross - commission_usdt
                if other_fee_count == 0 and pending_close_fill_count == 0 else None
            )

            return {
                "status": "LOCAL_LEDGER_ONLY",
                "realized_cash": (
                    None if realized_net is None else round(realized_net, 2)
                ),
                "realized_gross": round(realized_gross, 2),
                "total_commission": round(commission_usdt, 2),
                "other_fee_count": other_fee_count,
                "pending_close_fill_count": pending_close_fill_count,
                "fill_count": fill_count,
                "active_positions_count": active_count,
                "last_fill_at_ms": last_fill_at_ms,
            }

    def get_equity_history(self, period: str) -> dict[str, Any]:
        period_ms = _EQUITY_PERIOD_MS[period]
        if not self._db_path.is_file():
            return {
                "period": period,
                "points": [],
                "latest": None,
                "change": None,
                "change_pct": None,
                "max_drawdown": None,
                "max_drawdown_pct": None,
                "current_drawdown_pct": None,
            }

        now_ms = time.time_ns() // 1_000_000
        with closing(_get_readonly_connection(self._db_path)) as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT captured_at_ms, total_margin_balance
                   FROM account_equity_snapshots
                   WHERE captured_at_ms >= ?
                   ORDER BY captured_at_ms""",
                (now_ms - period_ms,),
            )
            rows = cur.fetchall()
            cur.execute(
                """SELECT captured_at_ms, total_margin_balance, unrealized_pnl,
                          available_balance, position_notional
                   FROM account_equity_snapshots
                   ORDER BY captured_at_ms DESC
                   LIMIT 1"""
            )
            latest_row = cur.fetchone()

        points = [
            {
                "captured_at_ms": row["captured_at_ms"],
                "equity": float(row["total_margin_balance"]),
            }
            for row in rows
        ]
        change = None
        change_pct = None
        if len(points) >= 2:
            start_equity = points[0]["equity"]
            end_equity = points[-1]["equity"]
            change = round(end_equity - start_equity, 2)
            if start_equity != 0:
                change_pct = round(change / start_equity * 100, 2)

        peak_equity = None
        max_drawdown = 0.0
        max_drawdown_pct = 0.0
        for point in points:
            peak_equity = point["equity"] if peak_equity is None else max(peak_equity, point["equity"])
            if peak_equity > 0:
                drawdown = peak_equity - point["equity"]
                drawdown_pct = drawdown / peak_equity * 100
                if drawdown_pct > max_drawdown_pct:
                    max_drawdown = drawdown
                    max_drawdown_pct = drawdown_pct
        current_drawdown_pct = None
        if len(points) >= 2 and peak_equity is not None and peak_equity > 0:
            current_drawdown_pct = round((peak_equity - points[-1]["equity"]) / peak_equity * 100, 2)

        latest = None
        if latest_row is not None:
            equity = float(latest_row["total_margin_balance"])
            notional = latest_row["position_notional"]
            unrealized = latest_row["unrealized_pnl"]
            available = latest_row["available_balance"]
            notional = float(notional) if notional is not None else None
            latest = {
                "captured_at_ms": latest_row["captured_at_ms"],
                "is_current_slot": latest_row["captured_at_ms"] >= now_ms - now_ms % 900_000,
                "equity": equity,
                "unrealized_pnl": float(unrealized) if unrealized is not None else None,
                "available_balance": float(available) if available is not None else None,
                "position_notional": notional,
                "leverage": notional / equity if notional is not None and equity > 0 else None,
            }

        return {
            "period": period,
            "points": points,
            "latest": latest,
            "change": change,
            "change_pct": change_pct,
            "max_drawdown": round(max_drawdown, 2) if len(points) >= 2 else None,
            "max_drawdown_pct": round(max_drawdown_pct, 2) if len(points) >= 2 else None,
            "current_drawdown_pct": current_drawdown_pct,
        }

    def get_analytics(self) -> dict[str, Any]:
        """Summarize strategy fills for the current Beijing day and by symbol."""

        if not self._db_path.is_file():
            return {
                "today": {"realized_net": 0.0, "commission": 0.0, "fill_count": 0, "other_fee_count": 0,
                          "pending_close_fill_count": 0},
                "symbols": [],
                "costs": {"commission_usdt": 0.0, "fill_count": 0, "other_fee_count": 0,
                          "pending_close_fill_count": 0, "days": []},
                "execution_issues": [],
            }
        now_ms = time.time_ns() // 1_000_000
        day_ms = 24 * 60 * 60 * 1_000
        now_beijing = datetime.fromtimestamp(now_ms / 1_000, tz=_BEIJING_TIMEZONE)
        today_start = now_beijing.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(today_start.timestamp() * 1_000)
        week_start = today_start - timedelta(days=6)
        week_start_ms = int(week_start.timestamp() * 1_000)
        close_actions = tuple(action.value for action in FULL_CLOSE_ACTIONS)
        close_placeholders = ", ".join("?" for _ in close_actions)
        with closing(_get_readonly_connection(self._db_path)) as conn:
            today = conn.execute(
                """SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) AS gross,
                          COALESCE(SUM(CASE WHEN commission_asset = 'USDT'
                              THEN CAST(commission AS REAL) ELSE 0 END), 0) AS commission,
                          COALESCE(SUM(CASE WHEN commission_asset != 'USDT'
                              AND CAST(commission AS REAL) != 0 THEN 1 ELSE 0 END), 0) AS other_fee_count,
                          COUNT(*) AS fill_count
                   FROM trade_fills WHERE traded_at_ms >= ? AND traded_at_ms < ?""",
                (start_ms, start_ms + day_ms),
            ).fetchone()
            pending_by_period = conn.execute(
                f"""SELECT
                       COUNT(CASE WHEN full_close_confirmed_at_ms IS NULL OR
                           (full_close_confirmed_at_ms >= ? AND full_close_confirmed_at_ms < ?)
                           THEN 1 END) AS today_count,
                       COUNT(CASE WHEN full_close_confirmed_at_ms IS NULL OR
                           (full_close_confirmed_at_ms >= ? AND full_close_confirmed_at_ms < ?)
                           THEN 1 END) AS week_count
                    FROM order_intents AS i
                    WHERE status = 'FILLED' AND action IN ({close_placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM trade_fills AS f WHERE f.intent_id = i.intent_id
                      )""",
                (start_ms, start_ms + day_ms, week_start_ms, now_ms + 1, *close_actions),
            ).fetchone()
            symbols = conn.execute(
                """SELECT symbol, SUM(CAST(realized_pnl AS REAL)) AS gross
                   FROM trade_fills WHERE side = 'SELL'
                   GROUP BY symbol ORDER BY gross DESC, symbol LIMIT 10"""
            ).fetchall()
            cost_days = conn.execute(
                """SELECT strftime('%Y-%m-%d', traded_at_ms / 1000, 'unixepoch', '+8 hours') AS day,
                          COALESCE(SUM(CASE WHEN commission_asset = 'USDT'
                              THEN CAST(commission AS REAL) ELSE 0 END), 0) AS commission_usdt,
                          COALESCE(SUM(CASE WHEN commission_asset != 'USDT'
                              AND CAST(commission AS REAL) != 0 THEN 1 ELSE 0 END), 0) AS other_fee_count,
                          COUNT(*) AS fill_count
                   FROM trade_fills WHERE traded_at_ms >= ? AND traded_at_ms < ?
                   GROUP BY day ORDER BY day""",
                (week_start_ms, now_ms + 1),
            ).fetchall()
            issues = conn.execute(
                """SELECT symbol, category, detail, occurred_at_ms
                   FROM dashboard_issues ORDER BY occurred_at_ms DESC, symbol LIMIT 50"""
            ).fetchall()
        cost_by_day = {row["day"]: row for row in cost_days}
        week_days = []
        for day_offset in range(7):
            day = (week_start.date() + timedelta(days=day_offset)).isoformat()
            row = cost_by_day.get(day)
            week_days.append(
                {
                    "day": day,
                    "commission_usdt": round(row["commission_usdt"], 4) if row else 0.0,
                    "fill_count": row["fill_count"] if row else 0,
                    "other_fee_count": row["other_fee_count"] if row else 0,
                }
            )

        return {
            "today": {
                "realized_net": (
                    round(today["gross"] - today["commission"], 2)
                    if today["other_fee_count"] == 0 and pending_by_period["today_count"] == 0 else None
                ),
                "commission": round(today["commission"], 2),
                "fill_count": today["fill_count"],
                "other_fee_count": today["other_fee_count"],
                "pending_close_fill_count": pending_by_period["today_count"],
            },
            "symbols": [
                {"symbol": row["symbol"], "realized_gross": round(row["gross"], 2)}
                for row in symbols
            ],
            "costs": {
                "commission_usdt": round(sum(row["commission_usdt"] for row in week_days), 2),
                "fill_count": sum(row["fill_count"] for row in week_days),
                "other_fee_count": sum(row["other_fee_count"] for row in week_days),
                "pending_close_fill_count": pending_by_period["week_count"],
                "days": week_days,
            },
            "execution_issues": [
                {
                    "symbol": row["symbol"],
                    "category": row["category"],
                    "detail": row["detail"],
                    "occurred_at_ms": row["occurred_at_ms"],
                }
                for row in issues
            ],
        }

    def get_tier_distribution(self) -> dict[str, Any]:
        if not self._db_path.is_file():
            return {
                "t1": 0, "t2": 0, "t21": 0, "t3": 0, "t31": 0,
                "t32": 0, "total": 0,
                "exposure": {"t1": 0.0, "t2": 0.0, "t3": 0.0, "total": 0.0,
                             "priced_positions": 0, "total_positions": 0},
            }

        with closing(_get_readonly_connection(self._db_path)) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT tier, completed_batches, COUNT(*) as cnt
                FROM symbol_states
                WHERE lifecycle IN ('OPEN', 'CLOSING')
                GROUP BY tier, completed_batches
            """)
            rows = cur.fetchall()
            exposure_rows = cur.execute(
                """SELECT s.tier, s.qty, p.price
                   FROM symbol_states AS s
                   LEFT JOIN latest_symbol_prices AS p ON p.symbol = s.symbol
                   WHERE s.lifecycle IN ('OPEN', 'CLOSING')"""
            ).fetchall()
            dist = {"t1": 0, "t2": 0, "t21": 0, "t3": 0, "t31": 0, "t32": 0}
            total = 0
            for r in rows:
                cnt = r["cnt"]
                tier = r["tier"]
                batches = r["completed_batches"]
                total += cnt
                if tier == 1:
                    dist["t1"] += cnt
                elif tier == 2:
                    if batches == 0:
                        dist["t2"] += cnt
                    else:
                        dist["t21"] += cnt
                elif tier == 3:
                    if batches == 0:
                        dist["t3"] += cnt
                    elif batches == 1:
                        dist["t31"] += cnt
                    else:
                        dist["t32"] += cnt
            dist["total"] = total
            exposure = {"t1": 0.0, "t2": 0.0, "t3": 0.0, "total": 0.0,
                        "priced_positions": 0, "total_positions": len(exposure_rows)}
            for row in exposure_rows:
                if row["price"] is None:
                    continue
                tier_key = f"t{row['tier']}"
                notional = float(row["qty"]) * float(row["price"])
                exposure[tier_key] += notional
                exposure["total"] += notional
                exposure["priced_positions"] += 1
            exposure = {
                key: round(value, 2) if isinstance(value, float) else value
                for key, value in exposure.items()
            }
            dist["exposure"] = exposure
            return dist

    def get_positions(self) -> list[dict[str, Any]]:
        if not self._db_path.is_file():
            return []

        with closing(_get_readonly_connection(self._db_path)) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT s.symbol, s.tier, s.lifecycle, s.qty, s.avg_entry_price,
                       s.tier_entry_qty, s.tier_anchor_price, s.completed_batches,
                       s.updated_at_ms, p.price AS scan_price,
                       p.captured_at_ms AS price_captured_at_ms
                FROM symbol_states AS s
                LEFT JOIN latest_symbol_prices AS p ON p.symbol = s.symbol
                WHERE lifecycle IN ('OPEN', 'CLOSING')
                ORDER BY s.tier DESC, s.updated_at_ms DESC
            """)
            rows = cur.fetchall()
            results = []
            for r in rows:
                t = r["tier"]
                batches = r["completed_batches"]
                q = float(r["qty"])
                avg_p = float(r["avg_entry_price"])
                anchor_p = float(r["tier_anchor_price"])
                tier_entry_q = float(r["tier_entry_qty"])

                if t == 1:
                    tier_label = "Tier 1"
                    tp_price = avg_p * (1 + float(T1_TAKE_PROFIT))
                    dca_price = anchor_p * (1 - float(DCA_DROP))
                    wo_price = None
                elif t == 2:
                    tier_label = "Tier 2" if batches == 0 else "Tier 2.1 (半仓)"
                    threshold = (
                        T2_FULL_TAKE_PROFIT
                        if batches > 0
                        else T2_BATCH1_TAKE_PROFIT
                    )
                    tp_price = avg_p * (1 + float(threshold))
                    dca_price = anchor_p * (1 - float(DCA_DROP))
                    wo_price = None
                elif t == 3:
                    if batches == 0:
                        tier_label = "Tier 3"
                        threshold = T3_BATCH1_TAKE_PROFIT
                    elif batches == 1:
                        tier_label = "Tier 3.1"
                        threshold = T3_BATCH2_TAKE_PROFIT
                    else:
                        tier_label = "Tier 3.2"
                        threshold = T3_FULL_TAKE_PROFIT
                    tp_price = avg_p * (1 + float(threshold))
                    dca_price = None
                    wo_price = float(T3_WRITEOFF_VALUE_LIMIT) / tier_entry_q
                else:
                    raise ValueError(
                        f"open symbol state has unsupported tier: {r['symbol']} tier={t}"
                    )

                if r["lifecycle"] == "CLOSING":
                    tier_label += " · 待全平"
                    tp_price = None
                    dca_price = None
                    wo_price = None

                notional = q * avg_p
                scan_price = float(r["scan_price"]) if r["scan_price"] is not None else None
                unrealized_pnl = (scan_price - avg_p) * q if scan_price is not None else None

                results.append({
                    "symbol": r["symbol"],
                    "tier": t,
                    "tier_label": tier_label,
                    "completed_batches": batches,
                    "qty": q,
                    "avg_entry_price": avg_p,
                    "tp_price": tp_price,
                    "dca_price": dca_price,
                    "writeoff_price": wo_price,
                    "entry_notional": round(notional, 2),
                    "scan_price": scan_price,
                    "price_captured_at_ms": r["price_captured_at_ms"],
                    "scan_notional": round(q * scan_price, 2) if scan_price is not None else None,
                    "estimated_unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
                })
            return results

    def get_recent_fills(self, limit: int, side: str | None = None) -> list[dict[str, Any]]:
        if not self._db_path.is_file():
            return []

        with closing(_get_readonly_connection(self._db_path)) as conn:
            cur = conn.cursor()
            where = "WHERE side = ?" if side is not None else ""
            params = (side, limit) if side is not None else (limit,)
            cur.execute(f"""
                SELECT symbol, side, price,
                       qty, realized_pnl, commission, commission_asset, traded_at_ms
                FROM trade_fills
                {where}
                ORDER BY traded_at_ms DESC
                LIMIT ?
            """, params)
            rows = cur.fetchall()
            return [
                {
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "price": float(r["price"]),
                    "qty": float(r["qty"]),
                    "realized_pnl": float(r["realized_pnl"]),
                    "commission": float(r["commission"]),
                    "commission_asset": r["commission_asset"],
                    "traded_at_ms": r["traded_at_ms"],
                }
                for r in rows
            ]
