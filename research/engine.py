import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KLINES_DIR = DATA_DIR / "klines"

@dataclass
class LimitSellOrder:
    price: float
    notional: float
    qty: float

@dataclass
class Position:
    symbol: str
    tier: int = 1  # 1: 10u, 2: 30u, 3: 90u
    size_notional: float = 10.0
    qty: float = 0.0
    avg_price: float = 0.0
    entry_p0: float = 0.0
    dca1_price: float = 0.0
    dca2_price: float = 0.0
    limit_buy_price: Optional[float] = None
    limit_buy_notional: float = 0.0
    limit_sells: List[LimitSellOrder] = field(default_factory=list)
    opened_at: Optional[pd.Timestamp] = None

class BacktestEngine:
    def __init__(
        self,
        initial_balance: float = 1000.0,
        initial_notional: float = 10.0,
        leverage: float = 10.0,
        maker_fee: float = 0.0002,   # 0.02% maker fee
        taker_fee: float = 0.0005,   # 0.05% taker fee
        maintenance_margin_rate: float = 0.01  # 1% Binance tier 1 MMR
    ):
        self.initial_balance = initial_balance
        self.initial_notional = initial_notional
        self.cash_balance = initial_balance
        self.leverage = leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.mmr = maintenance_margin_rate
        
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[dict] = []
        self.history: List[dict] = []
        
        self.is_liquidated = False
        self.liquidation_time = None
        self.liquidation_reason = ""
        
        # Load all available klines into memory
        self.klines: Dict[str, pd.DataFrame] = {}

    def load_data(self, symbols: List[str]):
        print(f"Loading klines for {len(symbols)} symbols into memory...")
        for sym in symbols:
            f = KLINES_DIR / f"{sym}_1h_2026.pkl"
            if f.exists():
                df = pd.read_pickle(f)
                df.set_index("open_time", inplace=True)
                self.klines[sym] = df
        print(f"Loaded data for {len(self.klines)} symbols.")

    def open_initial_position(self, symbol: str, price: float, timestamp: pd.Timestamp):
        if symbol in self.positions or self.is_liquidated:
            return False
            
        initial_notional = self.initial_notional
        initial_margin = initial_notional / self.leverage
        
        # Check available margin
        equity, free_margin, _ = self.get_account_status(current_prices={symbol: price})
        if free_margin < initial_margin:
            return False
            
        fee = initial_notional * self.taker_fee
        self.cash_balance -= fee
        
        qty = initial_notional / price
        pos = Position(
            symbol=symbol,
            tier=1,
            size_notional=initial_notional,
            qty=qty,
            avg_price=price,
            entry_p0=price,
            dca1_price=price * 0.50,
            dca2_price=price * 0.25,
            limit_buy_price=price * 0.50,
            limit_buy_notional=initial_notional * 2.0,
            limit_sells=[LimitSellOrder(price=price * 1.20, notional=initial_notional, qty=qty)],
            opened_at=timestamp
        )
        self.positions[symbol] = pos
        return True

    def get_account_status(self, current_prices: Dict[str, float]) -> Tuple[float, float, float]:
        unrealized_pnl = 0.0
        maintenance_margin = 0.0
        used_margin = 0.0
        
        for sym, pos in self.positions.items():
            p = current_prices.get(sym, pos.avg_price)
            upnl = (p - pos.avg_price) * pos.qty
            unrealized_pnl += upnl
            notional = p * pos.qty
            maintenance_margin += notional * self.mmr
            used_margin += notional / self.leverage
            
        equity = self.cash_balance + unrealized_pnl
        free_margin = max(0.0, equity - used_margin)
        return equity, free_margin, maintenance_margin

    def process_symbol_bar(self, sym: str, bar, timestamp: pd.Timestamp):
        if sym not in self.positions:
            return
            
        pos = self.positions[sym]
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        
        # Determine intra-bar price trajectory
        # Standard: if c >= o, open -> low -> high -> close; else open -> high -> low -> close
        sub_steps = [(l, h)] if c >= o else [(h, l)]
        
        for p1, p2 in sub_steps:
            # Check p1 then p2
            for price_extreme in [p1, p2]:
                # 1. Check DCA Limit Buy (Triggered if Low <= limit_buy_price)
                if pos.limit_buy_price is not None and l <= pos.limit_buy_price:
                    fill_price = pos.limit_buy_price
                    buy_notional = pos.limit_buy_notional
                    margin_needed = buy_notional / self.leverage
                    
                    equity, free_margin, _ = self.get_account_status({sym: fill_price})
                    if free_margin >= margin_needed:
                        fee = buy_notional * self.maker_fee
                        self.cash_balance -= fee
                        
                        added_qty = buy_notional / fill_price
                        new_qty = pos.qty + added_qty
                        new_notional = pos.size_notional + buy_notional
                        new_avg_price = new_notional / new_qty
                        
                        pos.tier += 1
                        pos.qty = new_qty
                        pos.size_notional = new_notional
                        pos.avg_price = new_avg_price
                        
                        if pos.tier == 2:
                            # 30u total. TP 50% at +20% avg, 50% at +50% avg
                            half_qty = new_qty * 0.5
                            half_notional = new_notional * 0.5
                            pos.limit_sells = [
                                LimitSellOrder(price=new_avg_price * 1.20, notional=half_notional, qty=half_qty),
                                LimitSellOrder(price=new_avg_price * 1.50, notional=half_notional, qty=half_qty)
                            ]
                            # DCA 2 at -50% from DCA1 price (0.25 * P0)
                            pos.limit_buy_price = pos.dca2_price
                            pos.limit_buy_notional = new_notional * 2.0
                            
                        elif pos.tier == 3:
                            # 90u total. TP 1/3 at +20%, 1/3 at +50%, 1/3 at +100% avg
                            third_qty = new_qty / 3.0
                            third_notional = new_notional / 3.0
                            pos.limit_sells = [
                                LimitSellOrder(price=new_avg_price * 1.20, notional=third_notional, qty=third_qty),
                                LimitSellOrder(price=new_avg_price * 1.50, notional=third_notional, qty=third_qty),
                                LimitSellOrder(price=new_avg_price * 2.00, notional=third_notional, qty=third_qty)
                            ]
                            pos.limit_buy_price = None  # No further DCA
                            pos.limit_buy_notional = 0.0
                    else:
                        # Insufficient margin to DCA
                        pass

                # 2. Check Limit Sells (Triggered if High >= sell.price)
                if pos.limit_sells:
                    remaining_sells = []
                    for s in pos.limit_sells:
                        if h >= s.price:
                            # Filled!
                            realized_pnl = (s.price - pos.avg_price) * s.qty
                            fee = (s.price * s.qty) * self.maker_fee
                            self.cash_balance += (realized_pnl - fee)
                            
                            pos.qty -= s.qty
                            pos.size_notional -= s.notional
                            
                            self.closed_trades.append({
                                "symbol": sym,
                                "close_time": timestamp,
                                "tier": pos.tier,
                                "pnl": realized_pnl - fee,
                                "hold_hours": (timestamp - pos.opened_at).total_seconds() / 3600 if pos.opened_at else 0
                            })
                        else:
                            remaining_sells.append(s)
                            
                    pos.limit_sells = remaining_sells
                    if pos.qty <= 1e-6 or not pos.limit_sells:
                        # Fully closed!
                        del self.positions[sym]
                        return

    def run_backtest(self, start_date: str = "2026-01-01 00:00:00", end_date: str = "2026-09-30 23:00:00"):
        all_timestamps = set()
        for df in self.klines.values():
            all_timestamps.update(df.index)
            
        timestamps = sorted([ts for ts in all_timestamps if pd.Timestamp(start_date) <= ts <= pd.Timestamp(end_date)])
        print(f"Running simulation across {len(timestamps)} hourly bars ({timestamps[0]} to {timestamps[-1]})...")
        
        # Initial opening at start_date
        initial_ts = timestamps[0]
        for sym, df in self.klines.items():
            if initial_ts in df.index:
                p = df.loc[initial_ts, "open"]
                self.open_initial_position(sym, p, initial_ts)
                
        print(f"Initial positions opened: {len(self.positions)} symbols.")
        
        for i, ts in enumerate(timestamps):
            current_prices = {}
            for sym, df in self.klines.items():
                if ts in df.index:
                    current_prices[sym] = df.loc[ts, "close"]
                    
            # 1. Process orders for existing positions
            for sym in list(self.positions.keys()):
                if sym in self.klines and ts in self.klines[sym].index:
                    bar = self.klines[sym].loc[ts]
                    self.process_symbol_bar(sym, bar, ts)
                    
            # 2. Check liquidation condition
            equity, free_margin, mm = self.get_account_status(current_prices)
            if equity <= mm:
                self.is_liquidated = True
                self.liquidation_time = ts
                self.liquidation_reason = f"Equity (${equity:.2f}) dropped below Maintenance Margin (${mm:.2f})"
                print(f"\n[!] LIQUIDATION TRIGGERED AT {ts}!")
                print(f"Reason: {self.liquidation_reason}")
                print(f"Active positions at liquidation: {len(self.positions)}")
                break
                
            # 3. Daily 08:00 check (Rebalance / Open new positions)
            if ts.hour == 8:
                for sym, df in self.klines.items():
                    if sym not in self.positions and ts in df.index:
                        p = df.loc[ts, "open"]
                        self.open_initial_position(sym, p, ts)
                        
            # Record equity curve
            self.history.append({
                "timestamp": ts,
                "equity": equity,
                "cash_balance": self.cash_balance,
                "active_positions": len(self.positions),
                "margin_used": equity - free_margin,
                "unrealized_pnl": equity - self.cash_balance
            })
            
            if (i + 1) % 500 == 0:
                print(f"Bar {i+1}/{len(timestamps)} ({ts}) - Equity: ${equity:.2f}, Positions: {len(self.positions)}")
                
        print("\nBacktest simulation completed!")
