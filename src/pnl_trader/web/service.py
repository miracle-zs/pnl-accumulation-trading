"""Read-only query service for the Web Dashboard reading state.sqlite3."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading

from ..clock import current_epoch_ms
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
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


_EQUITY_PERIOD_MS = {
    "24h": 24 * 60 * 60 * 1_000,
    "7d": 7 * 24 * 60 * 60 * 1_000,
    "30d": 30 * 24 * 60 * 60 * 1_000,
}
_BEIJING_TIMEZONE = timezone(timedelta(hours=8))
_FULL_CLOSE_ACTION_VALUES = tuple(action.value for action in FULL_CLOSE_ACTIONS)
_FULL_CLOSE_PLACEHOLDERS = ", ".join("?" for _ in _FULL_CLOSE_ACTION_VALUES)


class DashboardQueryService:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._fills_totals_lock = threading.Lock()
        self._fills_totals_cache: tuple[tuple[int, int | None], tuple[float, float, int, int, int | None]] | None = None

    def get_dashboard_snapshot(
        self,
        *,
        period: str = "7d",
        fills_limit: int = 12,
    ) -> dict[str, object]:
        """Read all dashboard panels from a single consistent SQLite snapshot."""
        period_ms = _EQUITY_PERIOD_MS[period]
        if not self._db_path.is_file():
            return {
                "overview": self._empty_overview(),
                "equity": self._empty_equity(period),
                "tier_distribution": self._empty_tier_distribution(),
                "positions": [],
                "buy_fills": [],
                "sell_fills": [],
                "analytics": self._empty_analytics(),
            }

        now_ms = current_epoch_ms()
        with closing(_get_readonly_connection(self._db_path)) as conn:
            conn.execute("BEGIN")
            try:
                positions, tier_distribution = self._query_positions_and_tiers(conn)
                overview = self._query_overview(
                    conn, active_count=len(positions)
                )
                equity = self._query_equity_history(conn, period, period_ms, now_ms)
                analytics = self._query_analytics(conn, now_ms)
                buy_fills = self._query_recent_fills(conn, limit=fills_limit, side="BUY")
                sell_fills = self._query_recent_fills(conn, limit=fills_limit, side="SELL")
            finally:
                conn.execute("ROLLBACK")

        return {
            "overview": overview,
            "equity": equity,
            "tier_distribution": tier_distribution,
            "positions": positions,
            "buy_fills": buy_fills,
            "sell_fills": sell_fills,
            "analytics": analytics,
        }

    def get_overview(self) -> dict[str, object]:
        if not self._db_path.is_file():
            return self._empty_overview()
        with closing(_get_readonly_connection(self._db_path)) as conn:
            active_count = conn.execute(
                "SELECT COUNT(*) FROM symbol_states WHERE lifecycle IN ('OPEN', 'CLOSING')"
            ).fetchone()[0]
            return self._query_overview(conn, active_count=active_count)

    def get_equity_history(self, period: str) -> dict[str, object]:
        period_ms = _EQUITY_PERIOD_MS[period]
        if not self._db_path.is_file():
            return self._empty_equity(period)
        now_ms = current_epoch_ms()
        with closing(_get_readonly_connection(self._db_path)) as conn:
            return self._query_equity_history(conn, period, period_ms, now_ms)

    def get_analytics(self) -> dict[str, object]:
        """Summarize strategy fills for the current Beijing day and by symbol."""
        if not self._db_path.is_file():
            return self._empty_analytics()
        now_ms = current_epoch_ms()
        with closing(_get_readonly_connection(self._db_path)) as conn:
            return self._query_analytics(conn, now_ms)

    def get_tier_distribution(self) -> dict[str, object]:
        if not self._db_path.is_file():
            return self._empty_tier_distribution()
        with closing(_get_readonly_connection(self._db_path)) as conn:
            _, tier_distribution = self._query_positions_and_tiers(conn)
            return tier_distribution

    def get_positions(self) -> list[dict[str, object]]:
        if not self._db_path.is_file():
            return []
        with closing(_get_readonly_connection(self._db_path)) as conn:
            positions, _ = self._query_positions_and_tiers(conn)
            return positions

    def get_recent_fills(
        self, limit: int, side: str | None = None
    ) -> list[dict[str, object]]:
        if not self._db_path.is_file():
            return []
        with closing(_get_readonly_connection(self._db_path)) as conn:
            return self._query_recent_fills(conn, limit=limit, side=side)

    def _query_fill_totals(
        self, conn: sqlite3.Connection
    ) -> tuple[float, float, int, int, int | None]:
        """Aggregate append-only trade_fills totals, cached by (max_rowid, max_time)."""
        watermark_row = conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) AS max_rowid, MAX(traded_at_ms) AS last_fill_at_ms FROM trade_fills"
        ).fetchone()
        watermark = (watermark_row["max_rowid"], watermark_row["last_fill_at_ms"])
        with self._fills_totals_lock:
            cached = self._fills_totals_cache
            if cached is not None and cached[0] == watermark:
                return cached[1]

        # Binance reports realized PnL and commissions separately. Only
        # USDT commissions can be subtracted directly from USDT PnL.
        fill_row = conn.execute("""
            SELECT
                COALESCE(SUM(CAST(realized_pnl AS REAL)), 0.0) AS realized_gross,
                COALESCE(SUM(CASE WHEN commission_asset = 'USDT'
                    THEN CAST(commission AS REAL) ELSE 0.0 END), 0.0) AS commission_usdt,
                COALESCE(SUM(CASE WHEN commission_asset != 'USDT'
                    AND CAST(commission AS REAL) != 0.0 THEN 1 ELSE 0 END), 0) AS other_fee_count,
                COUNT(*) AS fill_count,
                MAX(traded_at_ms) AS last_fill_at_ms
            FROM trade_fills
        """).fetchone()
        totals = (
            fill_row["realized_gross"],
            fill_row["commission_usdt"],
            fill_row["other_fee_count"],
            fill_row["fill_count"],
            fill_row["last_fill_at_ms"],
        )
        with self._fills_totals_lock:
            self._fills_totals_cache = (watermark, totals)
        return totals

    def _query_overview(
        self, conn: sqlite3.Connection, *, active_count: int
    ) -> dict[str, object]:
        realized_gross, commission_usdt, other_fee_count, fill_count, last_fill_at_ms = (
            self._query_fill_totals(conn)
        )
        pending_close_fill_count = conn.execute(
            f"""SELECT COUNT(*) FROM order_intents
                WHERE status = 'FILLED' AND action IN ({_FULL_CLOSE_PLACEHOLDERS})
                  AND NOT EXISTS (
                      SELECT 1 FROM trade_fills AS f
                      WHERE f.intent_id = order_intents.intent_id
                  )""",
            _FULL_CLOSE_ACTION_VALUES,
        ).fetchone()[0]

        realized_net = (
            realized_gross - commission_usdt
            if other_fee_count == 0 and pending_close_fill_count == 0 else None
        )
        return {
            "status": "LOCAL_LEDGER_ONLY",
            "realized_cash": None if realized_net is None else round(realized_net, 2),
            "realized_gross": round(realized_gross, 2),
            "total_commission": round(commission_usdt, 2),
            "other_fee_count": other_fee_count,
            "pending_close_fill_count": pending_close_fill_count,
            "fill_count": fill_count,
            "active_positions_count": active_count,
            "last_fill_at_ms": last_fill_at_ms,
        }

    def _query_equity_history(
        self,
        conn: sqlite3.Connection,
        period: str,
        period_ms: int,
        now_ms: int,
    ) -> dict[str, object]:
        rows = conn.execute(
            """SELECT captured_at_ms, total_margin_balance
               FROM account_equity_snapshots
               WHERE captured_at_ms >= ?
               ORDER BY captured_at_ms""",
            (now_ms - period_ms,),
        ).fetchall()
        latest_row = conn.execute(
            """SELECT captured_at_ms, total_margin_balance, unrealized_pnl,
                      available_balance, position_notional
               FROM account_equity_snapshots
               ORDER BY captured_at_ms DESC
               LIMIT 1"""
        ).fetchone()

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
            raw_change = end_equity - start_equity
            change = round(raw_change, 2)
            if start_equity != 0:
                change_pct = round(raw_change / start_equity * 100, 2)

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
            notional = float(latest_row["position_notional"])
            latest = {
                "captured_at_ms": latest_row["captured_at_ms"],
                "is_current_slot": latest_row["captured_at_ms"] >= now_ms - now_ms % 900_000,
                "equity": equity,
                "unrealized_pnl": float(latest_row["unrealized_pnl"]),
                "available_balance": float(latest_row["available_balance"]),
                "position_notional": notional,
                "leverage": notional / equity if equity > 0 else None,
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

    def _query_analytics(
        self, conn: sqlite3.Connection, now_ms: int
    ) -> dict[str, object]:
        day_ms = 24 * 60 * 60 * 1_000
        now_beijing = datetime.fromtimestamp(now_ms / 1_000, tz=_BEIJING_TIMEZONE)
        today_start = now_beijing.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(today_start.timestamp() * 1_000)
        week_start = today_start - timedelta(days=6)
        week_start_ms = int(week_start.timestamp() * 1_000)

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
                   COUNT(CASE WHEN full_close_confirmed_at_ms >= ? AND full_close_confirmed_at_ms < ?
                       THEN 1 END) AS today_count,
                   COUNT(CASE WHEN full_close_confirmed_at_ms >= ? AND full_close_confirmed_at_ms < ?
                       THEN 1 END) AS week_count
                FROM order_intents AS i
                WHERE status = 'FILLED' AND action IN ({_FULL_CLOSE_PLACEHOLDERS})
                  AND NOT EXISTS (
                      SELECT 1 FROM trade_fills AS f WHERE f.intent_id = i.intent_id
                  )""",
            (start_ms, start_ms + day_ms, week_start_ms, now_ms + 1, *_FULL_CLOSE_ACTION_VALUES),
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

    def _query_positions_and_tiers(
        self, conn: sqlite3.Connection
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        rows = conn.execute("""
            SELECT s.symbol, s.tier, s.lifecycle, s.qty, s.avg_entry_price,
                   s.tier_entry_qty, s.tier_anchor_price, s.completed_batches,
                   s.updated_at_ms, p.price AS scan_price,
                   p.captured_at_ms AS price_captured_at_ms
            FROM symbol_states AS s
            LEFT JOIN latest_symbol_prices AS p ON p.symbol = s.symbol
            WHERE s.lifecycle IN ('OPEN', 'CLOSING')
            ORDER BY s.tier DESC, s.updated_at_ms DESC
        """).fetchall()

        positions: list[dict[str, object]] = []
        dist: dict[str, object] = {"t1": 0, "t2": 0, "t21": 0, "t3": 0, "t31": 0, "t32": 0}
        exposure_totals = {"t1": 0.0, "t2": 0.0, "t3": 0.0, "total": 0.0}
        priced_positions = 0

        for r in rows:
            t = r["tier"]
            batches = r["completed_batches"]
            q = float(r["qty"])
            avg_p = float(r["avg_entry_price"])
            anchor_p = float(r["tier_anchor_price"])
            tier_entry_q = float(r["tier_entry_qty"])

            if t == 1 and batches == 0:
                bucket = "t1"
                tier_label = "Tier 1"
                tp_price = avg_p * (1 + float(T1_TAKE_PROFIT))
                dca_price = anchor_p * (1 - float(DCA_DROP))
                wo_price = None
            elif t == 2 and batches in {0, 1}:
                bucket = "t2" if batches == 0 else "t21"
                tier_label = "Tier 2" if batches == 0 else "Tier 2.1 (半仓)"
                threshold = T2_BATCH1_TAKE_PROFIT if batches == 0 else T2_FULL_TAKE_PROFIT
                tp_price = avg_p * (1 + float(threshold))
                dca_price = anchor_p * (1 - float(DCA_DROP))
                wo_price = None
            elif t == 3 and batches in {0, 1, 2}:
                if batches == 0:
                    bucket = "t3"
                    tier_label = "Tier 3"
                    threshold = T3_BATCH1_TAKE_PROFIT
                elif batches == 1:
                    bucket = "t31"
                    tier_label = "Tier 3.1"
                    threshold = T3_BATCH2_TAKE_PROFIT
                else:
                    bucket = "t32"
                    tier_label = "Tier 3.2"
                    threshold = T3_FULL_TAKE_PROFIT
                tp_price = avg_p * (1 + float(threshold))
                dca_price = None
                wo_price = float(T3_WRITEOFF_VALUE_LIMIT) / tier_entry_q
            else:
                raise ValueError(
                    f"open symbol state has unsupported tier/batches: "
                    f"{r['symbol']} tier={t} completed_batches={batches}"
                )

            dist[bucket] = int(dist[bucket]) + 1

            if r["lifecycle"] == "CLOSING":
                tier_label += " · 待全平"
                tp_price = None
                dca_price = None
                wo_price = None

            notional = q * avg_p
            scan_price = float(r["scan_price"]) if r["scan_price"] is not None else None
            unrealized_pnl = (scan_price - avg_p) * q if scan_price is not None else None
            if scan_price is not None:
                scan_notional = q * scan_price
                exposure_totals[f"t{t}"] += scan_notional
                exposure_totals["total"] += scan_notional
                priced_positions += 1
            else:
                scan_notional = None

            positions.append({
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
                "scan_notional": round(scan_notional, 2) if scan_notional is not None else None,
                "estimated_unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
            })

        dist["total"] = len(positions)
        dist["exposure"] = {
            "t1": round(exposure_totals["t1"], 2),
            "t2": round(exposure_totals["t2"], 2),
            "t3": round(exposure_totals["t3"], 2),
            "total": round(exposure_totals["total"], 2),
            "priced_positions": priced_positions,
            "total_positions": len(positions),
        }
        return positions, dist

    @staticmethod
    def _query_recent_fills(
        conn: sqlite3.Connection, *, limit: int, side: str | None = None
    ) -> list[dict[str, object]]:
        where = "WHERE side = ?" if side is not None else ""
        params = (side, limit) if side is not None else (limit,)
        rows = conn.execute(
            f"""SELECT symbol, side, price,
                       qty, realized_pnl, commission, commission_asset, traded_at_ms
                FROM trade_fills
                {where}
                ORDER BY traded_at_ms DESC
                LIMIT ?""",
            params,
        ).fetchall()
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

    @staticmethod
    def _empty_overview() -> dict[str, object]:
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

    @staticmethod
    def _empty_equity(period: str) -> dict[str, object]:
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

    @staticmethod
    def _empty_tier_distribution() -> dict[str, object]:
        return {
            "t1": None, "t2": None, "t21": None, "t3": None, "t31": None,
            "t32": None, "total": None,
            "exposure": {"t1": None, "t2": None, "t3": None, "total": None,
                         "priced_positions": None, "total_positions": None},
        }

    @staticmethod
    def _empty_analytics() -> dict[str, object]:
        return {
            "today": {"realized_net": None, "commission": None, "fill_count": None, "other_fee_count": None,
                      "pending_close_fill_count": None},
            "symbols": [],
            "costs": {"commission_usdt": None, "fill_count": None, "other_fee_count": None,
                      "pending_close_fill_count": None, "days": []},
            "execution_issues": [],
        }
