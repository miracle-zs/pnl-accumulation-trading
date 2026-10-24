import os
import json
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Tuple
import pandas as pd
import numpy as np

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

# 1. Load universe and pre-align klines
def prepare_data():
    u_file = DATA_DIR / "universe_200_20260101.json"
    universe = json.loads(u_file.read_text())
    symbols = [x["symbol"] for x in universe]
    
    # Load all klines
    dfs = {}
    all_ts = set()
    for sym in symbols:
        pkl = DATA_DIR / "klines" / f"{sym}_1h_2026.pkl"
        if pkl.exists():
            df = pd.read_pickle(pkl)
            df.set_index("open_time", inplace=True)
            dfs[sym] = df
            all_ts.update(df.index)
            
    timestamps = sorted([ts for ts in all_ts if pd.Timestamp("2026-01-01") <= ts <= pd.Timestamp("2026-09-30 23:00")])
    valid_symbols = [sym for sym in symbols if sym in dfs]
    
    # Build fast array structures
    num_symbols = len(valid_symbols)
    num_bars = len(timestamps)
    
    # 3D array: (num_bars, num_symbols, 4) -> 0: open, 1: high, 2: low, 3: close
    # Use NaN for missing bars
    bars = np.full((num_bars, num_symbols, 4), np.nan, dtype=np.float64)
    hours = np.array([ts.hour for ts in timestamps], dtype=np.int32)
    
    sym_to_idx = {sym: i for i, sym in enumerate(valid_symbols)}
    idx_to_sym = {i: sym for i, sym in enumerate(valid_symbols)}
    
    for sym, df in dfs.items():
        s_idx = sym_to_idx[sym]
        common_ts = df.index.intersection(timestamps)
        # Find bar indices
        ts_to_bar_idx = {ts: b_idx for b_idx, ts in enumerate(timestamps)}
        for ts in common_ts:
            b_idx = ts_to_bar_idx[ts]
            row = df.loc[ts]
            bars[b_idx, s_idx, 0] = row["open"]
            bars[b_idx, s_idx, 1] = row["high"]
            bars[b_idx, s_idx, 2] = row["low"]
            bars[b_idx, s_idx, 3] = row["close"]
            
    return valid_symbols, timestamps, bars, hours

# Fast Simulation Class
def simulate_fast(params, bars, hours, num_symbols, num_bars):
    """
    params:
      dca_drop_1: float (e.g. 0.30, 0.40, 0.50)
      dca_mult_1: float (e.g. 1.0, 1.5, 2.0)
      dca_drop_2: float (e.g. 0.30, 0.50)
      dca_mult_2: float (e.g. 1.0, 2.0)
      max_tiers: int (2 or 3)
      tp_tier1: float (e.g. 0.10, 0.15, 0.20)
      tp_escape: float (e.g. 0.05, 0.10, 0.20)
      tp_splits: int (1 or 2 or 3)
    """
    initial_balance = 1000.0
    initial_notional = 10.0
    leverage = 10.0
    taker_fee = 0.0005
    maker_fee = 0.0002
    mmr = 0.01
    
    cash_balance = initial_balance
    
    # Position tracking per symbol
    # Arrays of length num_symbols
    is_open = np.zeros(num_symbols, dtype=bool)
    tier = np.zeros(num_symbols, dtype=np.int32)
    qty = np.zeros(num_symbols, dtype=np.float64)
    size_notional = np.zeros(num_symbols, dtype=np.float64)
    avg_price = np.zeros(num_symbols, dtype=np.float64)
    entry_p0 = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_notional = np.zeros(num_symbols, dtype=np.float64)
    
    # Limit sell orders: up to 3 slots per symbol: (price, qty, notional)
    sell_active = np.zeros((num_symbols, 3), dtype=bool)
    sell_price = np.zeros((num_symbols, 3), dtype=np.float64)
    sell_qty = np.zeros((num_symbols, 3), dtype=np.float64)
    sell_notional = np.zeros((num_symbols, 3), dtype=np.float64)
    
    total_closed_trades = 0
    total_realized_pnl = 0.0
    
    lowest_equity = initial_balance
    peak_equity = initial_balance
    max_drawdown = 0.0
    is_liquidated = False
    
    # Helper to open position
    def open_pos(s_idx, p):
        nonlocal cash_balance
        if np.isnan(p) or p <= 0:
            return
        
        # Check free margin
        # Current equity
        upnl = 0.0
        used_margin = 0.0
        for i in range(num_symbols):
            if is_open[i]:
                # approximate with current price or avg_price
                upnl += (last_p[i] - avg_price[i]) * qty[i]
                used_margin += (last_p[i] * qty[i]) / leverage
        cur_eq = cash_balance + upnl
        free_m = cur_eq - used_margin
        
        init_m = initial_notional / leverage
        if free_m < init_m:
            return
            
        fee = initial_notional * taker_fee
        cash_balance -= fee
        
        q = initial_notional / p
        is_open[s_idx] = True
        tier[s_idx] = 1
        qty[s_idx] = q
        size_notional[s_idx] = initial_notional
        avg_price[s_idx] = p
        entry_p0[s_idx] = p
        
        # DCA 1 order
        limit_buy_price[s_idx] = p * (1.0 - params["dca_drop_1"])
        limit_buy_notional[s_idx] = initial_notional * params["dca_mult_1"]
        
        # TP order (Tier 1)
        sell_active[s_idx, :] = False
        sell_active[s_idx, 0] = True
        sell_price[s_idx, 0] = p * (1.0 + params["tp_tier1"])
        sell_qty[s_idx, 0] = q
        sell_notional[s_idx, 0] = initial_notional
        
    last_p = np.zeros(num_symbols, dtype=np.float64)
    
    # Bar 0: open initial positions
    for s in range(num_symbols):
        p = bars[0, s, 0]
        if not np.isnan(p):
            last_p[s] = p
            open_pos(s, p)
            
    # Simulation loop
    for b_idx in range(num_bars):
        # Update last prices
        for s in range(num_symbols):
            c = bars[b_idx, s, 3]
            if not np.isnan(c):
                last_p[s] = c
                
        # 1. Process orders for open positions
        for s in range(num_symbols):
            if not is_open[s]:
                continue
                
            o = bars[b_idx, s, 0]
            if np.isnan(o):
                continue
            h = bars[b_idx, s, 1]
            l = bars[b_idx, s, 2]
            c = bars[b_idx, s, 3]
            
            # Sub steps
            sub_extremes = [l, h] if c >= o else [h, l]
            
            for pe in sub_extremes:
                # Check DCA buy
                lb_p = limit_buy_price[s]
                if lb_p > 0 and l <= lb_p:
                    lb_notional = limit_buy_notional[s]
                    fee = lb_notional * maker_fee
                    cash_balance -= fee
                    
                    added_q = lb_notional / lb_p
                    new_q = qty[s] + added_q
                    new_notional = size_notional[s] + lb_notional
                    new_avg = new_notional / new_q
                    
                    tier[s] += 1
                    qty[s] = new_q
                    size_notional[s] = new_notional
                    avg_price[s] = new_avg
                    
                    # Update next orders based on tier
                    if tier[s] == 2:
                        # Set TP
                        sell_active[s, :] = False
                        if params["tp_splits"] == 1:
                            # 100% at escape price
                            sell_active[s, 0] = True
                            sell_price[s, 0] = new_avg * (1.0 + params["tp_escape"])
                            sell_qty[s, 0] = new_q
                            sell_notional[s, 0] = new_notional
                        else:
                            # 50% at escape, 50% at 1.5x escape or 1.5x avg
                            half_q = new_q * 0.5
                            half_notional = new_notional * 0.5
                            sell_active[s, 0] = True
                            sell_price[s, 0] = new_avg * (1.0 + params["tp_escape"])
                            sell_qty[s, 0] = half_q
                            sell_notional[s, 0] = half_notional
                            
                            sell_active[s, 1] = True
                            sell_price[s, 1] = new_avg * (1.0 + params["tp_escape"] * 2.0)
                            sell_qty[s, 1] = half_q
                            sell_notional[s, 1] = half_notional
                            
                        # DCA 2 (if max_tiers >= 3)
                        if params["max_tiers"] >= 3:
                            limit_buy_price[s] = lb_p * (1.0 - params["dca_drop_2"])
                            limit_buy_notional[s] = new_notional * params["dca_mult_2"]
                        else:
                            limit_buy_price[s] = 0.0
                            
                    elif tier[s] == 3:
                        sell_active[s, :] = False
                        if params["tp_splits"] == 1:
                            sell_active[s, 0] = True
                            sell_price[s, 0] = new_avg * (1.0 + params["tp_escape"])
                            sell_qty[s, 0] = new_q
                            sell_notional[s, 0] = new_notional
                        else:
                            # Split into 2 or 3
                            third_q = new_q / 3.0
                            third_notional = new_notional / 3.0
                            sell_active[s, 0] = True
                            sell_price[s, 0] = new_avg * (1.0 + params["tp_escape"])
                            sell_qty[s, 0] = third_q
                            sell_notional[s, 0] = third_notional
                            
                            sell_active[s, 1] = True
                            sell_price[s, 1] = new_avg * (1.0 + params["tp_escape"] * 2.0)
                            sell_qty[s, 1] = third_q
                            sell_notional[s, 1] = third_notional
                            
                            sell_active[s, 2] = True
                            sell_price[s, 2] = new_avg * (1.0 + params["tp_escape"] * 3.0)
                            sell_qty[s, 2] = third_q
                            sell_notional[s, 2] = third_notional
                            
                        limit_buy_price[s] = 0.0  # No more DCA
                        
                # Check Limit Sells
                for slot in range(3):
                    if sell_active[s, slot] and h >= sell_price[s, slot]:
                        sp = sell_price[s, slot]
                        sq = sell_qty[s, slot]
                        s_notional = sell_notional[s, slot]
                        
                        pnl = (sp - avg_price[s]) * sq
                        fee = (sp * sq) * maker_fee
                        cash_balance += (pnl - fee)
                        
                        qty[s] -= sq
                        size_notional[s] -= s_notional
                        sell_active[s, slot] = False
                        
                        total_closed_trades += 1
                        total_realized_pnl += (pnl - fee)
                        
                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    break
                    
        # 2. Check Liquidation & Drawdown
        tot_upnl = 0.0
        tot_notional = 0.0
        for s in range(num_symbols):
            if is_open[s]:
                lp = last_p[s]
                tot_upnl += (lp - avg_price[s]) * qty[s]
                tot_notional += lp * qty[s]
                
        equity = cash_balance + tot_upnl
        mm = tot_notional * mmr
        
        if equity <= mm:
            is_liquidated = True
            final_equity = equity
            lowest_equity = min(lowest_equity, equity)
            max_drawdown = -1.0
            return {
                "params": params,
                "final_equity": equity,
                "total_return": (equity - initial_balance) / initial_balance * 100,
                "lowest_equity": equity,
                "max_drawdown": -100.0,
                "calmar": 0.0,
                "total_trades": total_closed_trades,
                "realized_pnl": total_realized_pnl,
                "is_liquidated": True,
                "open_positions": np.sum(is_open)
            }
            
        if equity > peak_equity:
            peak_equity = equity
        dd = (equity - peak_equity) / peak_equity
        if dd < max_drawdown:
            max_drawdown = dd
        if equity < lowest_equity:
            lowest_equity = equity
            
        # 3. Daily 08:00 Rebalance
        if hours[b_idx] == 8:
            for s in range(num_symbols):
                if not is_open[s]:
                    p = bars[b_idx, s, 0]
                    if not np.isnan(p):
                        open_pos(s, p)
                        
    tot_return = (equity - initial_balance) / initial_balance * 100
    calmar = tot_return / abs(max_drawdown * 100) if max_drawdown < 0 else 0.0
    
    return {
        "params": params,
        "final_equity": equity,
        "total_return": tot_return,
        "lowest_equity": lowest_equity,
        "max_drawdown": max_drawdown * 100,
        "calmar": calmar,
        "total_trades": total_closed_trades,
        "realized_pnl": total_realized_pnl,
        "is_liquidated": False,
        "open_positions": int(np.sum(is_open))
    }

if __name__ == "__main__":
    valid_symbols, timestamps, bars, hours = prepare_data()
    print(f"Data ready: {len(valid_symbols)} symbols, {len(timestamps)} bars.")
    
    # Test baseline params (original strategy on 200 symbols)
    baseline_params = {
        "dca_drop_1": 0.50,
        "dca_mult_1": 2.0,
        "dca_drop_2": 0.50,
        "dca_mult_2": 2.0,
        "max_tiers": 3,
        "tp_tier1": 0.20,
        "tp_escape": 0.20,
        "tp_splits": 3
    }
    t0 = time.time()
    res = simulate_fast(baseline_params, bars, hours, len(valid_symbols), len(timestamps))
    print(f"Single fast sim took: {time.time()-t0:.4f}s")
    print("Baseline result:", res)
