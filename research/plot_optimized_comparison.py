import json
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from optimize_grid import get_cached_data

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

CACHE_BARS = DATA_DIR / "bars_cache.npy"
CACHE_INFO = DATA_DIR / "bars_info.json"

def run_sim_with_history(params, bars, hours, valid_symbols):
    """Run simulation and record hourly equity curve"""
    initial_balance = 1000.0
    initial_notional = 10.0
    leverage = 10.0
    taker_fee = 0.0005
    maker_fee = 0.0002
    mmr = 0.01
    
    num_symbols = len(valid_symbols)
    num_bars = bars.shape[0]
    
    cash_balance = initial_balance
    is_open = np.zeros(num_symbols, dtype=bool)
    tier = np.zeros(num_symbols, dtype=np.int32)
    qty = np.zeros(num_symbols, dtype=np.float64)
    size_notional = np.zeros(num_symbols, dtype=np.float64)
    avg_price = np.zeros(num_symbols, dtype=np.float64)
    entry_p0 = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_notional = np.zeros(num_symbols, dtype=np.float64)
    
    sell_active = np.zeros((num_symbols, 3), dtype=bool)
    sell_price = np.zeros((num_symbols, 3), dtype=np.float64)
    sell_qty = np.zeros((num_symbols, 3), dtype=np.float64)
    sell_notional = np.zeros((num_symbols, 3), dtype=np.float64)
    
    history_equity = np.zeros(num_bars, dtype=np.float64)
    last_p = np.zeros(num_symbols, dtype=np.float64)
    
    def open_pos(s_idx, p):
        nonlocal cash_balance
        if np.isnan(p) or p <= 0:
            return
        
        upnl = 0.0
        used_margin = 0.0
        for i in range(num_symbols):
            if is_open[i]:
                upnl += (last_p[i] - avg_price[i]) * qty[i]
                used_margin += (last_p[i] * qty[i]) / leverage
        cur_eq = cash_balance + upnl
        if (cur_eq - used_margin) < (initial_notional / leverage):
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
        
        limit_buy_price[s_idx] = p * (1.0 - params["dca_drop_1"])
        limit_buy_notional[s_idx] = initial_notional * params["dca_mult_1"]
        
        sell_active[s_idx, :] = False
        sell_active[s_idx, 0] = True
        sell_price[s_idx, 0] = p * (1.0 + params["tp_tier1"])
        sell_qty[s_idx, 0] = q
        sell_notional[s_idx, 0] = initial_notional

    # Initial open
    for s in range(num_symbols):
        p = bars[0, s, 0]
        if not np.isnan(p):
            last_p[s] = p
            open_pos(s, p)
            
    for b_idx in range(num_bars):
        for s in range(num_symbols):
            c = bars[b_idx, s, 3]
            if not np.isnan(c):
                last_p[s] = c
                
        for s in range(num_symbols):
            if not is_open[s]:
                continue
            o, h, l, c = bars[b_idx, s, 0], bars[b_idx, s, 1], bars[b_idx, s, 2], bars[b_idx, s, 3]
            if np.isnan(o):
                continue
            sub_extremes = [l, h] if c >= o else [h, l]
            for pe in sub_extremes:
                lb_p = limit_buy_price[s]
                if lb_p > 0 and l <= lb_p:
                    lb_notional = limit_buy_notional[s]
                    cash_balance -= lb_notional * maker_fee
                    added_q = lb_notional / lb_p
                    new_q = qty[s] + added_q
                    new_notional = size_notional[s] + lb_notional
                    new_avg = new_notional / new_q
                    
                    tier[s] += 1
                    qty[s] = new_q
                    size_notional[s] = new_notional
                    avg_price[s] = new_avg
                    
                    if tier[s] == 2:
                        sell_active[s, :] = False
                        if params["tp_splits"] == 1:
                            sell_active[s, 0] = True
                            sell_price[s, 0] = new_avg * (1.0 + params["tp_escape"])
                            sell_qty[s, 0] = new_q
                            sell_notional[s, 0] = new_notional
                        else:
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
                        limit_buy_price[s] = 0.0
                        
                for slot in range(3):
                    if sell_active[s, slot] and h >= sell_price[s, slot]:
                        sp, sq, sn = sell_price[s, slot], sell_qty[s, slot], sell_notional[s, slot]
                        pnl = (sp - avg_price[s]) * sq
                        fee = (sp * sq) * maker_fee
                        cash_balance += (pnl - fee)
                        qty[s] -= sq
                        size_notional[s] -= sn
                        sell_active[s, slot] = False
                        
                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    break
                    
        # Compute equity
        tot_upnl = 0.0
        for s in range(num_symbols):
            if is_open[s]:
                tot_upnl += (last_p[s] - avg_price[s]) * qty[s]
        eq = cash_balance + tot_upnl
        history_equity[b_idx] = eq
        
        if hours[b_idx] == 8:
            for s in range(num_symbols):
                if not is_open[s]:
                    p = bars[b_idx, s, 0]
                    if not np.isnan(p):
                        open_pos(s, p)
                        
    return history_equity

def main():
    info = json.loads(CACHE_INFO.read_text())
    bars = np.load(CACHE_BARS, mmap_mode="r")
    hours = np.array(info["hours"], dtype=np.int32)
    valid_symbols = info["symbols"]
    
    # Generate timestamp index
    timestamps = pd.date_range("2026-01-01 00:00:00", periods=info["num_bars"], freq="1h")
    
    configs = {
        "Baseline (Original Rules)": {
            "params": {"dca_drop_1": 0.50, "dca_mult_1": 2.0, "dca_drop_2": 0.50, "dca_mult_2": 2.0, "max_tiers": 3, "tp_tier1": 0.20, "tp_escape": 0.20, "tp_splits": 3},
            "color": "#787B86", "lw": 1.5, "ls": "--"
        },
        "Plan A: Best Calmar (Escape Hatch +10%)": {
            "params": {"dca_drop_1": 0.50, "dca_mult_1": 2.0, "dca_drop_2": 0.50, "dca_mult_2": 2.0, "max_tiers": 3, "tp_tier1": 0.15, "tp_escape": 0.10, "tp_splits": 1},
            "color": "#2962FF", "lw": 2.2, "ls": "-"
        },
        "Plan B: Max Profit (Early DCA -35%)": {
            "params": {"dca_drop_1": 0.35, "dca_mult_1": 2.0, "dca_drop_2": 0.50, "dca_mult_2": 2.0, "max_tiers": 3, "tp_tier1": 0.15, "tp_escape": 0.10, "tp_splits": 2},
            "color": "#FF1744", "lw": 1.8, "ls": "-"
        },
        "Plan C: Safe Defensive (Strict 2-Tier)": {
            "params": {"dca_drop_1": 0.50, "dca_mult_1": 1.0, "dca_drop_2": 0.50, "dca_mult_2": 1.0, "max_tiers": 2, "tp_tier1": 0.10, "tp_escape": 0.10, "tp_splits": 1},
            "color": "#00C853", "lw": 2.0, "ls": "-"
        }
    }
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    
    print("Simulating 4 key configurations for comparison chart...")
    for label, cfg in configs.items():
        eq = run_sim_with_history(cfg["params"], bars, hours, valid_symbols)
        s_eq = pd.Series(eq, index=timestamps)
        peak = s_eq.cummax()
        dd = (s_eq - peak) / peak * 100
        
        final_val = s_eq.iloc[-1]
        max_dd_val = dd.min()
        lowest_val = s_eq.min()
        
        legend_label = f"{label} -> Return: +{(final_val-1000)/10:.1f}%, MaxDD: {max_dd_val:.1f}%, Min: ${lowest_val:.0f}"
        
        ax1.plot(timestamps, s_eq, label=legend_label, color=cfg["color"], linewidth=cfg["lw"], linestyle=cfg["ls"])
        ax2.plot(timestamps, dd, label=f"{label} ({max_dd_val:.1f}%)", color=cfg["color"], linewidth=cfg["lw"], linestyle=cfg["ls"], alpha=0.75)
        
    ax1.axhline(1000.0, color="#B0BEC5", linestyle=":", label="Initial Capital ($1,000)")
    ax1.set_title("PnL Accumulation Optimization: 4 Distinct Strategy Profiles Comparison (2026)", fontsize=14, fontweight="bold", pad=12)
    ax1.set_ylabel("Account Equity (USD)", fontsize=12)
    ax1.legend(loc="upper left", framealpha=0.9, fontsize=10)
    ax1.grid(True, alpha=0.25)
    
    # Annotate the Feb crash zone
    ax1.axvspan(pd.Timestamp("2026-02-01"), pd.Timestamp("2026-02-15"), color="#FFCDD2", alpha=0.25, label="Feb Crash Zone")
    ax1.text(pd.Timestamp("2026-02-08"), 4100, "Feb 2026 Altcoin\nCorrection Dip", color="#C62828", fontsize=10, fontweight="bold", ha="center")
    
    ax2.set_ylabel("Drawdown (%)", fontsize=12)
    ax2.set_xlabel("Date", fontsize=12)
    ax2.grid(True, alpha=0.25)
    ax2.legend(loc="lower left", fontsize=9)
    
    plt.tight_layout()
    chart_path = RESULTS_DIR / "optimized_strategies_comparison.png"
    plt.savefig(chart_path, dpi=200)
    plt.close()
    print(f"Chart successfully saved to: {chart_path}")

if __name__ == "__main__":
    main()
