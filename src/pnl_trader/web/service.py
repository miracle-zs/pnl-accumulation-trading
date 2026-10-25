"""Read-only query service for the Web Dashboard reading state.sqlite3."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from ..domain.policy import StrategyConfig


def _get_readonly_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class DashboardQueryService:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def get_overview(self) -> dict[str, Any]:
        if not self._db_path.is_file():
            return self._empty_overview()

        conn = _get_readonly_connection(self._db_path)
        try:
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
            realized_gross = float(fill_row["realized_gross"]) if fill_row else 0.0
            commission_usdt = float(fill_row["commission_usdt"]) if fill_row else 0.0
            other_fee_count = int(fill_row["other_fee_count"]) if fill_row else 0
            fill_count = int(fill_row["fill_count"]) if fill_row else 0
            last_fill_at_ms = (
                None if fill_row is None or fill_row["last_fill_at_ms"] is None
                else int(fill_row["last_fill_at_ms"])
            )

            # Active positions are local strategy state, not a claim about
            # current exchange positions.
            cur.execute("SELECT COUNT(*) FROM symbol_states WHERE lifecycle = 'OPEN'")
            active_count = int(cur.fetchone()[0])

            realized_net = (
                realized_gross - commission_usdt if other_fee_count == 0 else None
            )

            return {
                "status": "LOCAL_LEDGER_ONLY",
                "realized_cash": _round_or_none(realized_net),
                "realized_gross": round(realized_gross, 2),
                "total_commission": round(commission_usdt, 2),
                "other_fee_count": other_fee_count,
                "fill_count": fill_count,
                "active_positions_count": active_count,
                "last_fill_at_ms": last_fill_at_ms,
            }
        finally:
            conn.close()

    def get_tier_distribution(self) -> dict[str, Any]:
        if not self._db_path.is_file():
            return {"t1": 0, "t2": 0, "t21": 0, "t3": 0, "t31": 0, "t32": 0, "idle": 0, "total": 0}

        conn = _get_readonly_connection(self._db_path)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT tier, completed_batches, lifecycle, COUNT(*) as cnt
                FROM symbol_states
                GROUP BY tier, completed_batches, lifecycle
            """)
            rows = cur.fetchall()
            dist = {"t1": 0, "t2": 0, "t21": 0, "t3": 0, "t31": 0, "t32": 0, "idle": 0}
            total = 0
            for r in rows:
                cnt = int(r["cnt"])
                tier = int(r["tier"])
                batches = int(r["completed_batches"])
                lc = r["lifecycle"]
                total += cnt
                if lc != "OPEN":
                    dist["idle"] += cnt
                elif tier == 1:
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
        finally:
            conn.close()

    def get_positions(self) -> list[dict[str, Any]]:
        if not self._db_path.is_file():
            return []

        config = StrategyConfig()
        conn = _get_readonly_connection(self._db_path)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT symbol, tier, lifecycle, qty, avg_entry_price,
                       tier_entry_qty, tier_anchor_price, tier_sold_qty,
                       completed_batches, updated_at_ms
                FROM symbol_states
                WHERE lifecycle = 'OPEN'
                ORDER BY tier DESC, updated_at_ms DESC
            """)
            rows = cur.fetchall()
            results = []
            for r in rows:
                t = int(r["tier"])
                batches = int(r["completed_batches"])
                q = float(r["qty"])
                avg_p = float(r["avg_entry_price"])
                anchor_p = float(r["tier_anchor_price"])
                tier_entry_q = float(r["tier_entry_qty"])

                if t == 1:
                    tier_label = "Tier 1"
                    tp_price = avg_p * (1 + float(config.t1_take_profit))
                    dca_price = anchor_p * (1 - float(config.dca_drop))
                    wo_price = 0.0
                elif t == 2:
                    tier_label = "Tier 2" if batches == 0 else "Tier 2.1 (半仓)"
                    threshold = (
                        config.t2_full_take_profit
                        if batches > 0
                        else config.t2_batch1_take_profit
                    )
                    tp_price = avg_p * (1 + float(threshold))
                    dca_price = anchor_p * (1 - float(config.dca_drop))
                    wo_price = 0.0
                elif t == 3:
                    if batches == 0:
                        tier_label = "Tier 3"
                        threshold = config.t3_batch1_take_profit
                    elif batches == 1:
                        tier_label = "Tier 3.1"
                        threshold = config.t3_batch2_take_profit
                    else:
                        tier_label = "Tier 3.2"
                        threshold = config.t3_full_take_profit
                    tp_price = avg_p * (1 + float(threshold))
                    dca_price = 0.0
                    wo_price = (
                        float(config.t3_writeoff_value_limit) / tier_entry_q
                        if tier_entry_q > 0
                        else 0.0
                    )
                else:
                    raise ValueError(
                        f"open symbol state has unsupported tier: {r['symbol']} tier={t}"
                    )

                notional = q * avg_p

                results.append({
                    "symbol": r["symbol"],
                    "tier": t,
                    "tier_label": tier_label,
                    "completed_batches": batches,
                    "qty": q,
                    "avg_entry_price": avg_p,
                    "anchor_price": anchor_p,
                    "tp_price": tp_price,
                    "dca_price": dca_price,
                    "writeoff_price": wo_price,
                    "entry_notional": round(notional, 2),
                    "updated_at_ms": int(r["updated_at_ms"]),
                })
            return results
        finally:
            conn.close()

    def get_recent_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._db_path.is_file():
            return []

        conn = _get_readonly_connection(self._db_path)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT symbol, exchange_trade_id, client_order_id, side, price,
                       qty, realized_pnl, commission, commission_asset, traded_at_ms
                FROM trade_fills
                ORDER BY traded_at_ms DESC
                LIMIT ?
            """, (limit,))
            rows = cur.fetchall()
            return [
                {
                    "symbol": r["symbol"],
                    "trade_id": r["exchange_trade_id"],
                    "side": r["side"],
                    "price": float(r["price"]),
                    "qty": float(r["qty"]),
                    "realized_pnl": float(r["realized_pnl"]),
                    "commission": float(r["commission"]),
                    "commission_asset": r["commission_asset"],
                    "traded_at_ms": int(r["traded_at_ms"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def _empty_overview(self) -> dict[str, Any]:
        return {
            "status": "WAITING_DATA",
            "realized_cash": 0.0,
            "realized_gross": 0.0,
            "total_commission": 0.0,
            "other_fee_count": 0,
            "fill_count": 0,
            "active_positions_count": 0,
            "last_fill_at_ms": None,
        }


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
