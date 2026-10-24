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
    tier3_entered_at: Optional[pd.Timestamp] = None
    is_degraded: bool = False

class HybridBacktestEngine:
    def __init__(
        self,
        initial_balance: float = 1000.0,
        initial_notional: float = 10.0,
        leverage: float = 10.0,
        tier3_decay_days: Optional[float] = None, # None = hold forever (Base), or 90, 60, 45
        degraded_escape_pct: float = 0.10         # +10% above avg_price
    ):
        self.initial_balance = initial_balance
        self.initial_notional = initial_notional
        self.cash_balance = initial_balance
        self.leverage = leverage
        self.maker_fee = 0.0002
        self.taker_fee = 0.0005
        self.mmr = 0.01
        self.tier3_decay_days = tier3_decay_days
        self.degraded_escape_pct = degraded_escape_pct
        
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[dict] = []
        self.history: List[dict] = []
        self.is_liquidated = False
        self.liquidation_time = None
        self.liquidation_reason = ""
        self.klines: Dict[str, pd.DataFrame] = {}

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
            limit_sells=[LimitSellOrder(price=price * 1.20, notional=self.initial_notional, qty=qty)],
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
        
        # Check Time-Decay Degradation for Tier 3
        if pos.tier == 3 and self.tier3_decay_days is not None and not pos.is_degraded:
            t3_start = pos.tier3_entered_at or pos.opened_at
            days_held = (timestamp - t3_start).total_seconds() / 86400.0
            if days_held >= self.tier3_decay_days:
                # Downgrade remaining sell orders to +10% escape
                escape_p = pos.avg_price * (1.0 + self.degraded_escape_pct)
                pos.limit_sells = [LimitSellOrder(price=escape_p, notional=pos.size_notional, qty=pos.qty)]
                pos.is_degraded = True
                
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
                            half_qty = new_qty * 0.5
                            half_notional = new_notional * 0.5
                            pos.limit_sells = [
                                LimitSellOrder(price=new_avg_price * 1.20, notional=half_notional, qty=half_qty),
                                LimitSellOrder(price=new_avg_price * 1.50, notional=half_notional, qty=half_qty)
                            ]
                            pos.limit_buy_price = pos.dca2_price
                            pos.limit_buy_notional = new_notional * 2.0
                        elif pos.tier == 3:
                            pos.tier3_entered_at = timestamp
                            third_qty = new_qty / 3.0
                            third_notional = new_notional / 3.0
                            pos.limit_sells = [
                                LimitSellOrder(price=new_avg_price * 1.20, notional=third_notional, qty=third_qty),
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
                                "is_degraded": pos.is_degraded,
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
                self.liquidation_reason = f"Equity (${equity:.2f}) dropped below Maintenance Margin (${mm:.2f})"
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
    print(f"Loaded {len(symbols)} symbols for Hybrid Mechanism Backtest.")
    
    test_setups = [
        ("Base (Hold Forever)", None),
        ("Hybrid 90-Day Degradation", 90.0),
        ("Hybrid 60-Day Degradation", 60.0),
        ("Hybrid 45-Day Degradation", 45.0)
    ]
    
    results = []
    equity_series = {}
    drawdown_series = {}
    
    for name, decay_days in test_setups:
        print(f"\nRunning {name}...")
        engine = HybridBacktestEngine(
            initial_balance=1000.0,
            initial_notional=10.0,
            leverage=10.0,
            tier3_decay_days=decay_days,
            degraded_escape_pct=0.10
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
        
        # Open positions stats
        open_pos_count = len(engine.positions)
        t3_open_count = sum(1 for p in engine.positions.values() if p.tier == 3)
        open_notional = sum(p.size_notional for p in engine.positions.values())
        unrealized_loss = final_eq - engine.cash_balance
        
        # Trades stats
        df_t = pd.DataFrame(engine.closed_trades)
        total_trades = len(df_t)
        degraded_escaped = len(df_t[df_t.get("is_degraded", False) == True]) if not df_t.empty and "is_degraded" in df_t else 0
        
        results.append({
            "name": name,
            "final_equity": final_eq,
            "total_return": tot_ret,
            "lowest_equity": lowest_eq,
            "max_drawdown": max_dd,
            "calmar": tot_ret / abs(max_dd) if max_dd < 0 else 0,
            "total_trades": total_trades,
            "degraded_escapes": degraded_escaped,
            "open_positions": open_pos_count,
            "t3_stuck": t3_open_count,
            "open_notional": open_notional,
            "unrealized_loss": unrealized_loss
        })
        
    df_res = pd.DataFrame(results)
    
    print("\n" + "="*110)
    print("                 HYBRID TIME-DECAY MECHANISM COMPARISON (200 SYMBOLS, 1,000U)                 ")
    print("="*110)
    print(f"{'Mechanism':<27} | {'Final Eq':<9} | {'Return':<8} | {'Lowest Eq':<9} | {'MaxDD':<8} | {'Calmar':<7} | {'Trades':<7} | {'Escapes':<8} | {'T3 Stuck':<8} | {'Open Notional':<13}")
    print("-"*110)
    for _, r in df_res.iterrows():
        print(f"{r['name']:<27} | ${r['final_equity']:<8,.2f} | {r['total_return']:<+7.1f}% | ${r['lowest_equity']:<8,.2f} | {r['max_drawdown']:<7.1f}% | {r['calmar']:<7.2f} | {r['total_trades']:<7} | {r['degraded_escapes']:<8} | {r['t3_stuck']:<8} | ${r['open_notional']:<12,.2f}")
    print("="*110)
    
    # Plot comparison chart
    colors = ["#787B86", "#2962FF", "#00C853", "#FF6D00"]
    linestyles = ["--", "-", "-", "-"]
    linewidths = [1.5, 2.0, 2.0, 1.8]
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    
    for i, (name, _) in enumerate(test_setups):
        s_eq = equity_series[name]
        s_dd = drawdown_series[name]
        r = df_res.iloc[i]
        label = f"{name} -> Return: {r['total_return']:+.1f}%, MaxDD: {r['max_drawdown']:.1f}%, T3 Stuck: {r['t3_stuck']}"
        ax1.plot(s_eq.index, s_eq, label=label, color=colors[i], linestyle=linestyles[i], linewidth=linewidths[i])
        ax2.plot(s_dd.index, s_dd, label=name, color=colors[i], linestyle=linestyles[i], linewidth=linewidths[i], alpha=0.75)
        
    ax1.axhline(1000.0, color="#B0BEC5", linestyle=":", label="Initial Capital ($1,000)")
    ax1.set_title("Hybrid Strategy: Base vs Time-Decay Degradation (90d / 60d / 45d) (2026)", fontsize=14, fontweight="bold", pad=12)
    ax1.set_ylabel("Account Equity (USD)", fontsize=12)
    ax1.legend(loc="upper left", framealpha=0.9, fontsize=10)
    ax1.grid(True, alpha=0.25)
    
    ax2.set_ylabel("Drawdown (%)", fontsize=12)
    ax2.set_xlabel("Date", fontsize=12)
    ax2.grid(True, alpha=0.25)
    ax2.legend(loc="lower left", fontsize=9)
    
    plt.tight_layout()
    chart_path = RESULTS_DIR / "hybrid_mechanism_comparison.png"
    plt.savefig(chart_path, dpi=200)
    plt.close()
    print(f"\nHybrid comparison chart saved to: {chart_path}")

if __name__ == "__main__":
    main()
