import json
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

from run_compare_tp_sweep import CustomEngine

u_file = DATA_DIR / "universe_200_20260101.json"
symbols = [x["symbol"] for x in json.loads(u_file.read_text())]

# Define 3 setups:
# 1. Base
# 2. Optimized (Best Return & Best Calmar: T1=10%, T2=(20%, 60%), T3=(20%, 60%, 120%))
# 3. Defensive (Lowest Drawdown: T1=10%, T2=(10%, 50%), T3=(20%, 50%, 100%))

class MultiTPEngine(CustomEngine):
    def __init__(self, name, t1, t2_1, t2_2, t3_1, t3_2, t3_3):
        super().__init__(tier1_tp=t1, tier2_tp1=t2_1, tier3_tp1=t3_1)
        self.name = name
        self.t2_tp2 = t2_2
        self.t3_tp2 = t3_2
        self.t3_tp3 = t3_3
        
    def process_symbol_bar(self, sym: str, bar, timestamp: pd.Timestamp):
        if sym not in self.positions:
            return
        pos = self.positions[sym]
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        sub_steps = [(l, h)] if c >= o else [(h, l)]
        for p1, p2 in sub_steps:
            for pe in [p1, p2]:
                if pos.limit_buy_price is not None and l <= pos.limit_buy_price:
                    fill_p = pos.limit_buy_price
                    buy_notional = pos.limit_buy_notional
                    margin_needed = buy_notional / self.leverage
                    equity, free_m, _ = self.get_account_status({sym: fill_p})
                    if free_m >= margin_needed:
                        self.cash_balance -= buy_notional * self.maker_fee
                        add_q = buy_notional / fill_p
                        new_q = pos.qty + add_q
                        new_n = pos.size_notional + buy_notional
                        new_avg = new_n / new_q
                        pos.tier += 1
                        pos.qty = new_q
                        pos.size_notional = new_n
                        pos.avg_price = new_avg
                        
                        from run_compare_tp_sweep import LimitSellOrder
                        if pos.tier == 2:
                            hq = new_q * 0.5
                            hn = new_n * 0.5
                            pos.limit_sells = [
                                LimitSellOrder(price=new_avg * (1.0 + self.tier2_tp1), notional=hn, qty=hq),
                                LimitSellOrder(price=new_avg * (1.0 + self.t2_tp2), notional=hn, qty=hq)
                            ]
                            pos.limit_buy_price = pos.dca2_price
                            pos.limit_buy_notional = new_n * 2.0
                        elif pos.tier == 3:
                            tq = new_q / 3.0
                            tn = new_n / 3.0
                            pos.limit_sells = [
                                LimitSellOrder(price=new_avg * (1.0 + self.tier3_tp1), notional=tn, qty=tq),
                                LimitSellOrder(price=new_avg * (1.0 + self.t3_tp2), notional=tn, qty=tq),
                                LimitSellOrder(price=new_avg * (1.0 + self.t3_tp3), notional=tn, qty=tq)
                            ]
                            pos.limit_buy_price = None
                            pos.limit_buy_notional = 0.0
                            
                if pos.limit_sells:
                    rem_sells = []
                    for s in pos.limit_sells:
                        if h >= s.price:
                            pnl = (s.price - pos.avg_price) * s.qty
                            fee = (s.price * s.qty) * self.maker_fee
                            self.cash_balance += (pnl - fee)
                            pos.qty -= s.qty
                            pos.size_notional -= s.notional
                            self.closed_trades.append({
                                "symbol": sym, "close_time": timestamp, "tier": pos.tier, "pnl": pnl - fee
                            })
                        else:
                            rem_sells.append(s)
                    pos.limit_sells = rem_sells
                    if pos.qty <= 1e-6 or not pos.limit_sells:
                        del self.positions[sym]
                        return

setups = [
    ("Base (20% | 20%, 50% | 20%, 50%, 100%)", 0.20, 0.20, 0.50, 0.20, 0.50, 1.00),
    ("Optimal (10% | 20%, 60% | 20%, 60%, 120%)", 0.10, 0.20, 0.60, 0.20, 0.60, 1.20),
    ("Defensive (10% | 10%, 50% | 20%, 50%, 100%)", 0.10, 0.10, 0.50, 0.20, 0.50, 1.00)
]

histories = {}
drawdowns = {}
stats = []

for name, t1, t2_1, t2_2, t3_1, t3_2, t3_3 in setups:
    eng = MultiTPEngine(name, t1, t2_1, t2_2, t3_1, t3_2, t3_3)
    eng.load_data(symbols)
    eng.run_backtest()
    df_h = pd.DataFrame(eng.history).set_index("timestamp")
    peak = df_h["equity"].cummax()
    dd = (df_h["equity"] - peak) / peak * 100
    histories[name] = df_h["equity"]
    drawdowns[name] = dd
    
    final_eq = df_h["equity"].iloc[-1]
    lowest_eq = df_h["equity"].min()
    max_dd = dd.min()
    tot_ret = (final_eq - 1000.0) / 10.0
    calmar = tot_ret / abs(max_dd) if max_dd < 0 else 0
    stats.append({
        "name": name,
        "final_equity": final_eq,
        "total_return": tot_ret,
        "lowest_equity": lowest_eq,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "trades": len(eng.closed_trades)
    })

# Plot
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
colors = ["#787B86", "#2962FF", "#00C853"]
linestyles = ["--", "-", "-"]
linewidths = [1.8, 2.2, 2.0]

for i, (name, *_) in enumerate(setups):
    st = stats[i]
    lbl = f"{name} -> Ret: {st['total_return']:+.1f}%, MaxDD: {st['max_drawdown']:.1f}%, Lowest: ${st['lowest_equity']:.0f}, Calmar: {st['calmar']:.2f}"
    ax1.plot(histories[name].index, histories[name], label=lbl, color=colors[i], linestyle=linestyles[i], linewidth=linewidths[i])
    ax2.plot(drawdowns[name].index, drawdowns[name], label=name, color=colors[i], linestyle=linestyles[i], linewidth=linewidths[i], alpha=0.8)

ax1.axhline(1000.0, color="#B0BEC5", linestyle=":", label="Initial Capital ($1,000)")
ax1.set_title("Strategy Comparison: Base vs 6-Param Optimal vs Defensive Setup (200 Symbols, 2026)", fontsize=13, fontweight="bold", pad=12)
ax1.set_ylabel("Account Equity (USD)", fontsize=11)
ax1.legend(loc="upper left", framealpha=0.9, fontsize=9.5)
ax1.grid(True, alpha=0.25)

ax2.set_ylabel("Drawdown (%)", fontsize=11)
ax2.set_xlabel("Date", fontsize=11)
ax2.grid(True, alpha=0.25)
ax2.legend(loc="lower left", fontsize=8.5)

plt.tight_layout()
chart_path = RESULTS_DIR / "comparison_optimal_vs_base_tp.png"
plt.savefig(chart_path, dpi=200)
plt.close()
print("Saved comparison chart to:", chart_path)
