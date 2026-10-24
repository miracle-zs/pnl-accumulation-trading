import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)
KLINES_DIR = DATA_DIR / "klines"

@dataclass
class LimitSellOrder:
    price: float
    notional: float
    qty: float

@dataclass
class Position:
    symbol: str
    tier: int = 1
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

class CustomEngine:
    def __init__(
        self,
        initial_balance: float = 1000.0,
        initial_notional: float = 10.0,
        leverage: float = 10.0,
        tier1_tp: float = 0.15,
        tier2_tp1: float = 0.20,
        tier3_tp1: float = 0.20
    ):
        self.initial_balance = initial_balance
        self.initial_notional = initial_notional
        self.cash_balance = initial_balance
        self.leverage = leverage
        self.maker_fee = 0.0002
        self.taker_fee = 0.0005
        self.mmr = 0.01
        self.tier1_tp = tier1_tp
        self.tier2_tp1 = tier2_tp1
        self.tier3_tp1 = tier3_tp1
        
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[dict] = []
        self.history: List[dict] = []
        self.is_liquidated = False
        self.liquidation_time = None
        self.liquidation_reason = ""
        self.klines: Dict[str, pd.DataFrame] = {}
        
        self.dca1_count = 0
        self.dca2_count = 0

    def load_data(self, symbols: List[str]):
        for sym in symbols:
            f = KLINES_DIR / f"{sym}_1h_2026.pkl"
            if f.exists():
                df = pd.read_pickle(f)
                df.set_index("open_time", inplace=True)
                self.klines[sym] = df

    def open_initial_position(self, symbol: str, price: float, timestamp: pd.Timestamp):
        if symbol in self.positions or self.is_liquidated:
            return False
            
        initial_margin = self.initial_notional / self.leverage
        equity, free_margin, _ = self.get_account_status(current_prices={symbol: price})
        if free_margin < initial_margin:
            return False
            
        self.cash_balance -= self.initial_notional * self.taker_fee
        qty = self.initial_notional / price
        pos = Position(
            symbol=symbol,
            tier=1,
            size_notional=self.initial_notional,
            qty=qty,
            avg_price=price,
            entry_p0=price,
            dca1_price=price * 0.50,
            dca2_price=price * 0.25,
            limit_buy_price=price * 0.50,
            limit_buy_notional=self.initial_notional * 2.0,
            limit_sells=[LimitSellOrder(price=price * (1.0 + self.tier1_tp), notional=self.initial_notional, qty=qty)],
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
            unrealized_pnl += (p - pos.avg_price) * pos.qty
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
        
        sub_steps = [(l, h)] if c >= o else [(h, l)]
        for p1, p2 in sub_steps:
            for pe in [p1, p2]:
                # DCA Buy
                if pos.limit_buy_price is not None and l <= pos.limit_buy_price:
                    fill_price = pos.limit_buy_price
                    buy_notional = pos.limit_buy_notional
                    margin_needed = buy_notional / self.leverage
                    equity, free_margin, _ = self.get_account_status({sym: fill_price})
                    if free_margin >= margin_needed:
                        self.cash_balance -= buy_notional * self.maker_fee
                        added_qty = buy_notional / fill_price
                        new_qty = pos.qty + added_qty
                        new_notional = pos.size_notional + buy_notional
                        new_avg_price = new_notional / new_qty
                        
                        pos.tier += 1
                        pos.qty = new_qty
                        pos.size_notional = new_notional
                        pos.avg_price = new_avg_price
                        
                        if pos.tier == 2:
                            self.dca1_count += 1
                            half_qty = new_qty * 0.5
                            half_notional = new_notional * 0.5
                            pos.limit_sells = [
                                LimitSellOrder(price=new_avg_price * (1.0 + self.tier2_tp1), notional=half_notional, qty=half_qty),
                                LimitSellOrder(price=new_avg_price * 1.50, notional=half_notional, qty=half_qty)
                            ]
                            pos.limit_buy_price = pos.dca2_price
                            pos.limit_buy_notional = new_notional * 2.0
                        elif pos.tier == 3:
                            self.dca2_count += 1
                            third_qty = new_qty / 3.0
                            third_notional = new_notional / 3.0
                            pos.limit_sells = [
                                LimitSellOrder(price=new_avg_price * (1.0 + self.tier3_tp1), notional=third_notional, qty=third_qty),
                                LimitSellOrder(price=new_avg_price * 1.50, notional=third_notional, qty=third_qty),
                                LimitSellOrder(price=new_avg_price * 2.00, notional=third_notional, qty=third_qty)
                            ]
                            pos.limit_buy_price = None
                            pos.limit_buy_notional = 0.0

                # Limit Sells
                if pos.limit_sells:
                    remaining_sells = []
                    for s in pos.limit_sells:
                        if h >= s.price:
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
                        del self.positions[sym]
                        return

    def run_backtest(self, start_date: str = "2026-01-01 00:00:00", end_date: str = "2026-09-30 23:00:00"):
        all_timestamps = set()
        for df in self.klines.values():
            all_timestamps.update(df.index)
        timestamps = sorted([ts for ts in all_timestamps if pd.Timestamp(start_date) <= ts <= pd.Timestamp(end_date)])
        
        initial_ts = timestamps[0]
        for sym, df in self.klines.items():
            if initial_ts in df.index:
                p = df.loc[initial_ts, "open"]
                self.open_initial_position(sym, p, initial_ts)
                
        for i, ts in enumerate(timestamps):
            current_prices = {}
            for sym, df in self.klines.items():
                if ts in df.index:
                    current_prices[sym] = df.loc[ts, "close"]
                    
            for sym in list(self.positions.keys()):
                if sym in self.klines and ts in self.klines[sym].index:
                    bar = self.klines[sym].loc[ts]
                    self.process_symbol_bar(sym, bar, ts)
                    
            equity, free_margin, mm = self.get_account_status(current_prices)
            if equity <= mm:
                self.is_liquidated = True
                self.liquidation_time = ts
                self.liquidation_reason = f"Liquidation at {ts}"
                break
                
            if ts.hour == 8:
                for sym, df in self.klines.items():
                    if sym not in self.positions and ts in df.index:
                        p = df.loc[ts, "open"]
                        self.open_initial_position(sym, p, ts)
                        
            self.history.append({
                "timestamp": ts,
                "equity": equity,
                "cash_balance": self.cash_balance,
                "active_positions": len(self.positions),
                "margin_used": equity - free_margin,
                "unrealized_pnl": equity - self.cash_balance
            })

def main():
    u_file = DATA_DIR / "universe_200_20260101.json"
    universe = json.loads(u_file.read_text())
    symbols = [x["symbol"] for x in universe]
    print(f"Loaded {len(symbols)} symbols.")
    
    setups = [
        ("TP +10%", 0.10),
        ("TP +15%", 0.15),
        ("Base (TP +20%)", 0.20),
        ("TP +25%", 0.25),
        ("TP +30%", 0.30)
    ]
    
    results = []
    equity_series = {}
    drawdown_series = {}
    
    for name, tp1 in setups:
        print(f"Running {name}...")
        engine = CustomEngine(
            initial_balance=1000.0,
            initial_notional=10.0,
            leverage=10.0,
            tier1_tp=tp1,
            tier2_tp1=0.20,
            tier3_tp1=0.20
        )
        engine.load_data(symbols)
        engine.run_backtest()
        
        df_h = pd.DataFrame(engine.history)
        df_h.set_index("timestamp", inplace=True)
        peak = df_h["equity"].cummax()
        dd = (df_h["equity"] - peak) / peak * 100
        
        equity_series[name] = df_h["equity"]
        drawdown_series[name] = dd
        
        final_eq = df_h["equity"].iloc[-1]
        lowest_eq = df_h["equity"].min()
        max_dd = dd.min()
        tot_ret = (final_eq - 1000.0) / 10.0
        
        df_t = pd.DataFrame(engine.closed_trades)
        total_trades = len(df_t)
        tier1_trades = len(df_t[df_t["tier"] == 1]) if not df_t.empty else 0
        tier2_trades = len(df_t[df_t["tier"] == 2]) if not df_t.empty else 0
        tier3_trades = len(df_t[df_t["tier"] == 3]) if not df_t.empty else 0
        avg_hold_h_t1 = df_t[df_t["tier"] == 1]["hold_hours"].mean() if tier1_trades > 0 else 0
        tot_pnl = df_t["pnl"].sum() if not df_t.empty else 0
        
        open_count = len(engine.positions)
        t1_open = sum(1 for p in engine.positions.values() if p.tier == 1)
        t2_open = sum(1 for p in engine.positions.values() if p.tier == 2)
        t3_open = sum(1 for p in engine.positions.values() if p.tier == 3)
        open_notional = sum(p.size_notional for p in engine.positions.values())
        unrealized = final_eq - engine.cash_balance
        
        results.append({
            "name": name,
            "tp_pct": tp1 * 100,
            "final_equity": final_eq,
            "total_return": tot_ret,
            "lowest_equity": lowest_eq,
            "max_drawdown": max_dd,
            "calmar": tot_ret / abs(max_dd) if max_dd < 0 else 0,
            "total_trades": total_trades,
            "t1_trades": tier1_trades,
            "t2_trades": tier2_trades,
            "t3_trades": tier3_trades,
            "avg_hold_days_t1": avg_hold_h_t1 / 24.0,
            "dca1_triggers": engine.dca1_count,
            "dca2_triggers": engine.dca2_count,
            "t1_open": t1_open,
            "t2_open": t2_open,
            "t3_open": t3_open,
            "open_notional": open_notional,
            "unrealized_loss": unrealized,
            "total_realized_pnl": tot_pnl
        })
        
    df_res = pd.DataFrame(results)
    
    print("\n" + "="*115)
    print("           SENSITIVITY COMPARISON: TIER 1 TP RANGE (10% ~ 30%) (200 SYMBOLS, 1,000U)           ")
    print("="*115)
    for _, r in df_res.iterrows():
        print(f"Strategy: {r['name']:<16} | Final: ${r['final_equity']:<8,.2f} | Ret: {r['total_return']:<+7.1f}% | Lowest: ${r['lowest_equity']:<6,.2f} | MaxDD: {r['max_drawdown']:<6.1f}% | Calmar: {r['calmar']:<5.2f} | T1 Trades: {r['t1_trades']:<5} | Hold: {r['avg_hold_days_t1']:<4.1f}d")
    print("="*115)
    
    # Chart 1: Equity & Drawdown comparison (focus on 15% vs 20% vs 30%)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    colors = ["#9C27B0", "#00B0FF", "#2962FF", "#00C853", "#FF6D00"]
    linestyles = [":", "-", "-", "--", ":"]
    linewidths = [1.2, 2.2, 2.0, 1.5, 1.2]
    
    for i, (name, _) in enumerate(setups):
        s_eq = equity_series[name]
        s_dd = drawdown_series[name]
        r = df_res.iloc[i]
        label = f"{name} -> Ret: {r['total_return']:+.1f}%, MaxDD: {r['max_drawdown']:.1f}%, Calmar: {r['calmar']:.2f}, T1 Trades: {r['t1_trades']}"
        ax1.plot(s_eq.index, s_eq, label=label, color=colors[i], linestyle=linestyles[i], linewidth=linewidths[i])
        ax2.plot(s_dd.index, s_dd, label=name, color=colors[i], linestyle=linestyles[i], linewidth=linewidths[i], alpha=0.8)
        
    ax1.axhline(1000.0, color="#787B86", linestyle=":", label="Initial Capital ($1,000)")
    ax1.set_title("Tier 1 Take-Profit Sensitivity: 10% vs 15% vs 20% vs 25% vs 30% (200 Symbols, 2026)", fontsize=13, fontweight="bold", pad=12)
    ax1.set_ylabel("Account Equity (USD)", fontsize=11)
    ax1.legend(loc="upper left", framealpha=0.9, fontsize=9.5)
    ax1.grid(True, alpha=0.25)
    
    ax2.set_ylabel("Drawdown (%)", fontsize=11)
    ax2.set_xlabel("Date", fontsize=11)
    ax2.grid(True, alpha=0.25)
    ax2.legend(loc="lower left", fontsize=8.5)
    
    plt.tight_layout()
    chart_path = RESULTS_DIR / "comparison_tp_sensitivity_10_to_30.png"
    plt.savefig(chart_path, dpi=200)
    plt.close()
    print(f"\nSaved comparison chart to: {chart_path}")
    
    with open(RESULTS_DIR / "comparison_tp_sensitivity.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    main()
