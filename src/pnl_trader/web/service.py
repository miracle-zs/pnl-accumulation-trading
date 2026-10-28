"""Read-only query service for the Web Dashboard reading state.sqlite3."""

from __future__ import annotations

from contextlib import closing
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
                      AND intent_id NOT IN (SELECT intent_id FROM trade_fills)""",
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
        }

    def get_analytics(self) -> dict[str, Any]:
        """Summarize strategy fills for the current Beijing day and by symbol."""

        if not self._db_path.is_file():
            return {
                "today": {"realized_net": 0.0, "commission": 0.0, "fill_count": 0, "other_fee_count": 0},
                "symbols": [],
            }
        now_ms = time.time_ns() // 1_000_000
        day_ms = 24 * 60 * 60 * 1_000
        offset_ms = 8 * 60 * 60 * 1_000
        start_ms = ((now_ms + offset_ms) // day_ms) * day_ms - offset_ms
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
            symbols = conn.execute(
                """SELECT symbol, SUM(CAST(realized_pnl AS REAL)) AS gross
                   FROM trade_fills WHERE side = 'SELL'
                   GROUP BY symbol ORDER BY gross DESC, symbol LIMIT 10"""
            ).fetchall()
        return {
            "today": {
                "realized_net": (
                    round(today["gross"] - today["commission"], 2)
                    if today["other_fee_count"] == 0 else None
                ),
                "commission": round(today["commission"], 2),
                "fill_count": today["fill_count"],
                "other_fee_count": today["other_fee_count"],
            },
            "symbols": [
                {"symbol": row["symbol"], "realized_gross": round(row["gross"], 2)}
                for row in symbols
            ],
        }

    def get_tier_distribution(self) -> dict[str, Any]:
        if not self._db_path.is_file():
            return {"t1": 0, "t2": 0, "t21": 0, "t3": 0, "t31": 0, "t32": 0, "total": 0}

        with closing(_get_readonly_connection(self._db_path)) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT tier, completed_batches, COUNT(*) as cnt
                FROM symbol_states
                WHERE lifecycle IN ('OPEN', 'CLOSING')
                GROUP BY tier, completed_batches
            """)
            rows = cur.fetchall()
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
            return dist

    def get_positions(self) -> list[dict[str, Any]]:
        if not self._db_path.is_file():
            return []

        with closing(_get_readonly_connection(self._db_path)) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT symbol, tier, lifecycle, qty, avg_entry_price,
                       tier_entry_qty, tier_anchor_price, completed_batches,
                       updated_at_ms
                FROM symbol_states
                WHERE lifecycle IN ('OPEN', 'CLOSING')
                ORDER BY tier DESC, updated_at_ms DESC
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
