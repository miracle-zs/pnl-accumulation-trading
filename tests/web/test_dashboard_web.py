"""Tests for the Web Dashboard FastAPI app and Query Service."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx

from pnl_trader.web import DashboardQueryService, create_app


class DashboardWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._temp_dir.name)
        self._db_path = self._data_dir / "state.sqlite3"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _init_sample_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE symbol_states (
                symbol TEXT NOT NULL,
                tier INTEGER NOT NULL,
                lifecycle TEXT NOT NULL,
                qty TEXT NOT NULL,
                avg_entry_price TEXT NOT NULL,
                tier_entry_qty TEXT NOT NULL,
                tier_anchor_price TEXT NOT NULL,
                completed_batches INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (symbol)
            );

            CREATE TABLE trade_fills (
                symbol TEXT NOT NULL,
                exchange_trade_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                qty TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                commission TEXT NOT NULL,
                commission_asset TEXT NOT NULL,
                traded_at_ms INTEGER NOT NULL,
                intent_id TEXT NOT NULL,
                PRIMARY KEY (symbol, exchange_trade_id)
            );

            INSERT INTO symbol_states VALUES
            ('BTCUSDT', 1, 'OPEN', '0.001', '60000.0', '0.001', '60000.0', 0, 1700000000000),
            ('ETHUSDT', 2, 'OPEN', '0.05', '3000.0', '0.05', '3000.0', 0, 1700000000000),
            ('SOLUSDT', 2, 'OPEN', '0.2', '150.0', '0.4', '150.0', 1, 1700000000000),
            ('DOGEUSDT', 0, 'IDLE', '0', '0', '0', '0', 0, 1700000000000);

            INSERT INTO trade_fills VALUES
            ('BTCUSDT', 't-1', 'SELL', '66000.0', '0.001', '6.0', '0.033', 'USDT', 1700000000000, 'intent-1');

        """)
        conn.commit()
        conn.close()

    async def test_dashboard_routes_return_valid_data(self) -> None:
        self._init_sample_db()
        app = create_app(data_dir=self._data_dir)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # 1. Test root dashboard HTML
            resp = await client.get("/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("PNL Periodic Review Trader", resp.text)

            # 2. Test overview API
            resp = await client.get("/api/overview")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "LOCAL_LEDGER_ONLY")
            self.assertAlmostEqual(data["realized_cash"], 5.97)
            self.assertEqual(data["realized_gross"], 6.0)
            self.assertEqual(data["fill_count"], 1)
            self.assertAlmostEqual(data["total_commission"], 0.03)
            self.assertEqual(data["active_positions_count"], 3)

            # 3. Test tier distribution API
            resp = await client.get("/api/tier-distribution")
            self.assertEqual(resp.status_code, 200)
            td = resp.json()
            self.assertEqual(td["t1"], 1)
            self.assertEqual(td["t2"], 1)
            self.assertEqual(td["t21"], 1)
            self.assertEqual(td["total"], 3)

            # 4. Test positions API
            resp = await client.get("/api/positions")
            self.assertEqual(resp.status_code, 200)
            positions = resp.json()
            self.assertEqual(len(positions), 3)
            syms = [p["symbol"] for p in positions]
            self.assertIn("BTCUSDT", syms)
            self.assertIn("ETHUSDT", syms)
            self.assertIn("SOLUSDT", syms)

            # 5. Test fills API
            resp = await client.get("/api/fills")
            self.assertEqual(resp.status_code, 200)
            fills = resp.json()
            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0]["symbol"], "BTCUSDT")
            self.assertEqual(fills[0]["realized_pnl"], 6.0)


if __name__ == "__main__":
    unittest.main()
