import os
import json
import time
from pathlib import Path
from multiprocessing import Pool
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

CACHE_BARS = DATA_DIR / "bars_cache.npy"
CACHE_INFO = DATA_DIR / "bars_info.json"

# Global vars for worker processes
G_BARS = None
G_HOURS = None
G_NUM_SYMBOLS = 0
G_NUM_BARS = 0

def init_worker(bars_file, info_file):
    global G_BARS, G_HOURS, G_NUM_SYMBOLS, G_NUM_BARS
    info = json.loads(Path(info_file).read_text())
    G_BARS = np.load(bars_file, mmap_mode="r")
    G_HOURS = np.array(info["hours"], dtype=np.int32)
    G_NUM_SYMBOLS = len(info["symbols"])
    G_NUM_BARS = info["num_bars"]

def simulate_one(params):
    global G_BARS, G_HOURS, G_NUM_SYMBOLS, G_NUM_BARS
    
    p_t1_tp = params["t1_tp"]
    p_t2_tp1 = params["t2_tp1"]
    p_t2_tp2 = params["t2_tp2"]
    p_t3_tp1 = params["t3_tp1"]
    p_t3_tp2 = params["t3_tp2"]
    p_t3_tp3 = params["t3_tp3"]
    
    initial_balance = 1000.0
    initial_notional = 10.0
    leverage = 10.0
    taker_fee = 0.0005
    maker_fee = 0.0002
    mmr = 0.01
    
    cash_balance = initial_balance
    is_open = np.zeros(G_NUM_SYMBOLS, dtype=bool)
    tier = np.zeros(G_NUM_SYMBOLS, dtype=np.int32)
    qty = np.zeros(G_NUM_SYMBOLS, dtype=np.float64)
    size_notional = np.zeros(G_NUM_SYMBOLS, dtype=np.float64)
    avg_price = np.zeros(G_NUM_SYMBOLS, dtype=np.float64)
    
    limit_buy_price = np.zeros(G_NUM_SYMBOLS, dtype=np.float64)
    limit_buy_notional = np.zeros(G_NUM_SYMBOLS, dtype=np.float64)
    
    # 3 sell slots
    sell_active = np.zeros((G_NUM_SYMBOLS, 3), dtype=bool)
    sell_price = np.zeros((G_NUM_SYMBOLS, 3), dtype=np.float64)
    sell_qty = np.zeros((G_NUM_SYMBOLS, 3), dtype=np.float64)
    sell_notional = np.zeros((G_NUM_SYMBOLS, 3), dtype=np.float64)
    
    lowest_equity = initial_balance
    peak_equity = initial_balance
    max_drawdown = 0.0
    total_closed_trades = 0
    t1_trades = 0
    t2_trades = 0
    t3_trades = 0
    total_realized_pnl = 0.0
    
    last_p = np.zeros(G_NUM_SYMBOLS, dtype=np.float64)
    
    # Bar 0 open
    for s in range(G_NUM_SYMBOLS):
        p = G_BARS[0, s, 0]
        if not np.isnan(p):
            last_p[s] = p
            fee = initial_notional * taker_fee
            cash_balance -= fee
            q = initial_notional / p
            is_open[s] = True
            tier[s] = 1
            qty[s] = q
            size_notional[s] = initial_notional
            avg_price[s] = p
            limit_buy_price[s] = p * 0.50
            limit_buy_notional[s] = 20.0
            sell_active[s, 0] = True
            sell_price[s, 0] = p * (1.0 + p_t1_tp)
            sell_qty[s, 0] = q
            sell_notional[s, 0] = initial_notional
            
    for b_idx in range(G_NUM_BARS):
        c_bar = G_BARS[b_idx, :, 3]
        valid_c = ~np.isnan(c_bar)
        last_p[valid_c] = c_bar[valid_c]
        
        o_bar = G_BARS[b_idx, :, 0]
        h_bar = G_BARS[b_idx, :, 1]
        l_bar = G_BARS[b_idx, :, 2]
        
        open_indices = np.where(is_open)[0]
        for s in open_indices:
            o = o_bar[s]
            if np.isnan(o):
                continue
            h = h_bar[s]
            l = l_bar[s]
            c = c_bar[s]
            
            sub_extremes = [l, h] if c >= o else [h, l]
            for pe in sub_extremes:
                # DCA Buy
                lb_p = limit_buy_price[s]
                if lb_p > 0 and l <= lb_p:
                    lb_n = limit_buy_notional[s]
                    fee = lb_n * maker_fee
                    cash_balance -= fee
                    added_q = lb_n / lb_p
                    new_q = qty[s] + added_q
                    new_n = size_notional[s] + lb_n
                    new_avg = new_n / new_q
                    tier[s] += 1
                    qty[s] = new_q
                    size_notional[s] = new_n
                    avg_price[s] = new_avg
                    
                    sell_active[s, :] = False
                    if tier[s] == 2:
                        half_q = new_q * 0.5
                        half_n = new_n * 0.5
                        sell_active[s, 0] = True
                        sell_price[s, 0] = new_avg * (1.0 + p_t2_tp1)
                        sell_qty[s, 0] = half_q
                        sell_notional[s, 0] = half_n
                        
                        sell_active[s, 1] = True
                        sell_price[s, 1] = new_avg * (1.0 + p_t2_tp2)
                        sell_qty[s, 1] = half_q
                        sell_notional[s, 1] = half_n
                        
                        limit_buy_price[s] = lb_p * 0.50
                        limit_buy_notional[s] = new_n * 2.0
                    elif tier[s] == 3:
                        third_q = new_q / 3.0
                        third_n = new_n / 3.0
                        sell_active[s, 0] = True
                        sell_price[s, 0] = new_avg * (1.0 + p_t3_tp1)
                        sell_qty[s, 0] = third_q
                        sell_notional[s, 0] = third_n
                        
                        sell_active[s, 1] = True
                        sell_price[s, 1] = new_avg * (1.0 + p_t3_tp2)
                        sell_qty[s, 1] = third_q
                        sell_notional[s, 1] = third_n
                        
                        sell_active[s, 2] = True
                        sell_price[s, 2] = new_avg * (1.0 + p_t3_tp3)
                        sell_qty[s, 2] = third_q
                        sell_notional[s, 2] = third_n
                        
                        limit_buy_price[s] = 0.0
                        
                # Limit Sells
                for slot in range(3):
                    if sell_active[s, slot] and h >= sell_price[s, slot]:
                        sp = sell_price[s, slot]
                        sq = sell_qty[s, slot]
                        sn = sell_notional[s, slot]
                        pnl = (sp - avg_price[s]) * sq
                        fee = (sp * sq) * maker_fee
                        cash_balance += (pnl - fee)
                        qty[s] -= sq
                        size_notional[s] -= sn
                        sell_active[s, slot] = False
                        total_closed_trades += 1
                        if tier[s] == 1:
                            t1_trades += 1
                        elif tier[s] == 2:
                            t2_trades += 1
                        elif tier[s] == 3:
                            t3_trades += 1
                        total_realized_pnl += (pnl - fee)
                        
                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    break
                    
        op_mask = is_open
        if np.any(op_mask):
            lp_sub = last_p[op_mask]
            q_sub = qty[op_mask]
            tot_upnl = np.sum((lp_sub - avg_price[op_mask]) * q_sub)
            tot_notional = np.sum(lp_sub * q_sub)
        else:
            tot_upnl = 0.0
            tot_notional = 0.0
            
        equity = cash_balance + tot_upnl
        mm = tot_notional * mmr
        if equity <= mm:
            return {
                **params,
                'final_equity': equity,
                'total_return': -100.0,
                'lowest_equity': equity,
                'max_drawdown': -100.0,
                'calmar': 0.0,
                'total_trades': total_closed_trades,
                'realized_pnl': total_realized_pnl,
                'is_liquidated': True
            }
            
        if equity > peak_equity:
            peak_equity = equity
        dd = (equity - peak_equity) / peak_equity
        if dd < max_drawdown:
            max_drawdown = dd
        if equity < lowest_equity:
            lowest_equity = equity
            
        # Daily 08:00 rebalance
        if G_HOURS[b_idx] == 8:
            free_margin = max(0.0, equity - (tot_notional / leverage))
            closed_indices = np.where(~is_open)[0]
            for s in closed_indices:
                if free_margin < (initial_notional / leverage):
                    break
                p = G_BARS[b_idx, s, 0]
                if not np.isnan(p):
                    fee = initial_notional * taker_fee
                    cash_balance -= fee
                    free_margin -= (initial_notional / leverage)
                    q = initial_notional / p
                    is_open[s] = True
                    tier[s] = 1
                    qty[s] = q
                    size_notional[s] = initial_notional
                    avg_price[s] = p
                    limit_buy_price[s] = p * 0.50
                    limit_buy_notional[s] = 20.0
                    sell_active[s, :] = False
                    sell_active[s, 0] = True
                    sell_price[s, 0] = p * (1.0 + p_t1_tp)
                    sell_qty[s, 0] = q
                    sell_notional[s, 0] = initial_notional
                    
    tot_ret = (equity - initial_balance) / initial_balance * 100
    calmar = tot_ret / abs(max_drawdown * 100) if max_drawdown < 0 else 0.0
    
    t1_open = int(np.sum(tier == 1))
    t2_open = int(np.sum(tier == 2))
    t3_open = int(np.sum(tier == 3))
    
    return {
        **params,
        'final_equity': float(equity),
        'total_return': float(tot_ret),
        'lowest_equity': float(lowest_equity),
        'max_drawdown': float(max_drawdown * 100),
        'calmar': float(calmar),
        'total_trades': int(total_closed_trades),
        't1_trades': int(t1_trades),
        't2_trades': int(t2_trades),
        't3_trades': int(t3_trades),
        't1_open': t1_open,
        't2_open': t2_open,
        't3_open': t3_open,
        'open_positions': int(np.sum(is_open)),
        'realized_pnl': float(total_realized_pnl),
        'is_liquidated': False
    }

def generate_param_combinations():
    t1_tp_list = [0.10, 0.15, 0.20, 0.25]
    
    t2_pairs = [
        (0.10, 0.30),
        (0.10, 0.40),
        (0.10, 0.50),
        (0.15, 0.35),
        (0.15, 0.50),
        (0.20, 0.40),
        (0.20, 0.50),  # Base
        (0.20, 0.60)
    ]
    
    t3_triplets = [
        (0.08, 0.20, 0.50), # Ultra fast escape
        (0.10, 0.25, 0.50), # Fast rescue
        (0.10, 0.35, 0.70), # Balanced rescue
        (0.15, 0.35, 0.70), # Moderate rescue
        (0.15, 0.50, 1.00),
        (0.20, 0.50, 1.00), # Base
        (0.20, 0.60, 1.20)  # Aggressive moonshot
    ]
    
    grid = []
    # Always include exact Base
    base_param = {
        "name": "Base (原版 20/20/50/20/50/100)",
        "t1_tp": 0.20,
        "t2_tp1": 0.20, "t2_tp2": 0.50,
        "t3_tp1": 0.20, "t3_tp2": 0.50, "t3_tp3": 1.00
    }
    grid.append(base_param)
    
    for t1 in t1_tp_list:
        for t2_1, t2_2 in t2_pairs:
            for t3_1, t3_2, t3_3 in t3_triplets:
                name = f"T1_{int(t1*100)}|T2_{int(t2_1*100)}_{int(t2_2*100)}|T3_{int(t3_1*100)}_{int(t3_2*100)}_{int(t3_3*100)}"
                p = {
                    "name": name,
                    "t1_tp": t1,
                    "t2_tp1": t2_1, "t2_tp2": t2_2,
                    "t3_tp1": t3_1, "t3_tp2": t3_2, "t3_tp3": t3_3
                }
                grid.append(p)
                
    # Deduplicate
    seen = set()
    dedup_grid = []
    for g in grid:
        key = (g["t1_tp"], g["t2_tp1"], g["t2_tp2"], g["t3_tp1"], g["t3_tp2"], g["t3_tp3"])
        if key not in seen:
            seen.add(key)
            dedup_grid.append(g)
            
    return dedup_grid

def main():
    print("Generating parameter grid for 6 TP parameters...")
    grid = generate_param_combinations()
    print(f"Total parameter combinations to evaluate: {len(grid)}")
    
    t0 = time.time()
    num_cpus = min(8, os.cpu_count() or 4)
    print(f"Launching multiprocessing pool with {num_cpus} cores...")
    
    with Pool(processes=num_cpus, initializer=init_worker, initargs=(CACHE_BARS, CACHE_INFO)) as pool:
        results = pool.map(simulate_one, grid)
        
    elapsed = time.time() - t0
    print(f"\nAll {len(results)} simulations completed in {elapsed:.1f}s ({elapsed/len(results):.2f}s per simulation on average).")
    
    df = pd.DataFrame(results)
    df.to_csv(RESULTS_DIR / "tp_6params_optimization.csv", index=False)
    with open(RESULTS_DIR / "tp_6params_optimization.json", "w") as f:
        json.dump(results, f, indent=2)
        
    # Analysis
    base_row = df[(df["t1_tp"] == 0.20) & (df["t2_tp1"] == 0.20) & (df["t2_tp2"] == 0.50) & 
                  (df["t3_tp1"] == 0.20) & (df["t3_tp2"] == 0.50) & (df["t3_tp3"] == 1.00)].iloc[0]
                  
    print("\n" + "="*115)
    print(f"Base Benchmark: Final Equity = ${base_row['final_equity']:,.2f} | Return = {base_row['total_return']:+.2f}% | MaxDD = {base_row['max_drawdown']:.2f}% | Calmar = {base_row['calmar']:.2f} | T3 Stuck = {base_row['t3_open']}")
    print("="*115)
    
    # Top by Calmar
    df_calmar = df.sort_values(by="calmar", ascending=False).head(5)
    print("\n--- TOP 5 BY CALMAR RATIO (风险收益性价比最优) ---")
    for idx, r in df_calmar.iterrows():
        print(f"Rank {r['calmar']:.2f} Calmar | Final: ${r['final_equity']:,.2f} (+{r['total_return']:.1f}%) | MaxDD: {r['max_drawdown']:.1f}% | Lowest: ${r['lowest_equity']:,.2f} | T3 Stuck: {r['t3_open']} | Params: T1={int(r['t1_tp']*100)}% | T2=({int(r['t2_tp1']*100)}%, {int(r['t2_tp2']*100)}%) | T3=({int(r['t3_tp1']*100)}%, {int(r['t3_tp2']*100)}%, {int(r['t3_tp3']*100)}%)")
        
    # Top by Return
    df_ret = df.sort_values(by="total_return", ascending=False).head(5)
    print("\n--- TOP 5 BY TOTAL RETURN (绝对收益最高) ---")
    for idx, r in df_ret.iterrows():
        print(f"Rank +{r['total_return']:.1f}% Ret | Final: ${r['final_equity']:,.2f} | MaxDD: {r['max_drawdown']:.1f}% | Calmar: {r['calmar']:.2f} | Lowest: ${r['lowest_equity']:,.2f} | T3 Stuck: {r['t3_open']} | Params: T1={int(r['t1_tp']*100)}% | T2=({int(r['t2_tp1']*100)}%, {int(r['t2_tp2']*100)}%) | T3=({int(r['t3_tp1']*100)}%, {int(r['t3_tp2']*100)}%, {int(r['t3_tp3']*100)}%)")

    # Top by MaxDD (Lowest Drawdown)
    df_dd = df.sort_values(by="max_drawdown", ascending=False).head(5)
    print("\n--- TOP 5 BY LOWEST DRAWDOWN (回撤控制最强) ---")
    for idx, r in df_dd.iterrows():
        print(f"Rank {r['max_drawdown']:.1f}% MaxDD | Final: ${r['final_equity']:,.2f} (+{r['total_return']:.1f}%) | Calmar: {r['calmar']:.2f} | Lowest: ${r['lowest_equity']:,.2f} | T3 Stuck: {r['t3_open']} | Params: T1={int(r['t1_tp']*100)}% | T2=({int(r['t2_tp1']*100)}%, {int(r['t2_tp2']*100)}%) | T3=({int(r['t3_tp1']*100)}%, {int(r['t3_tp2']*100)}%, {int(r['t3_tp3']*100)}%)")

    # Plot Pareto Chart & Comparison
    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(16, 12))
    
    # 1. Pareto Frontier
    sc = ax1.scatter(-df["max_drawdown"], df["total_return"], c=df["calmar"], cmap="viridis", alpha=0.75, s=40, edgecolors="none")
    cbar = plt.colorbar(sc, ax=ax1)
    cbar.set_label("Calmar Ratio", fontsize=10)
    
    # Highlight Base
    ax1.scatter(-base_row["max_drawdown"], base_row["total_return"], color="red", s=150, marker="*", label=f"Base (Calmar: {base_row['calmar']:.2f})", zorder=5)
    
    # Highlight Best Calmar
    best_c = df_calmar.iloc[0]
    ax1.scatter(-best_c["max_drawdown"], best_c["total_return"], color="blue", s=130, marker="^", label=f"Best Calmar ({best_c['calmar']:.2f})", zorder=5)
    
    # Highlight Best Return
    best_r = df_ret.iloc[0]
    ax1.scatter(-best_r["max_drawdown"], best_r["total_return"], color="green", s=130, marker="D", label=f"Best Return (+{best_r['total_return']:.1f}%)", zorder=5)

    ax1.set_title("Pareto Frontier: Total Return vs Max Drawdown", fontsize=12, fontweight="bold")
    ax1.set_xlabel("Max Drawdown Magnitude (%)", fontsize=10)
    ax1.set_ylabel("Total Return (%)", fontsize=10)
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="lower left", fontsize=9.5)
    
    # 2. Marginal Effect of Tier 1 TP
    m_t1 = df.groupby("t1_tp")[["total_return", "max_drawdown", "calmar"]].mean()
    ax2.plot(m_t1.index * 100, m_t1["calmar"], marker="o", color="#2962FF", linewidth=2.0, label="Mean Calmar")
    ax2_twin = ax2.twinx()
    ax2_twin.plot(m_t1.index * 100, m_t1["total_return"], marker="s", color="#FF6D00", linewidth=2.0, linestyle="--", label="Mean Return (%)")
    ax2.set_title("Marginal Effect: Tier 1 TP Target", fontsize=12, fontweight="bold")
    ax2.set_xlabel("Tier 1 TP (%)", fontsize=10)
    ax2.set_ylabel("Calmar Ratio", color="#2962FF", fontsize=10)
    ax2_twin.set_ylabel("Total Return (%)", color="#FF6D00", fontsize=10)
    ax2.grid(True, alpha=0.3)
    
    # 3. Marginal Effect of Tier 2 Fast Escape (t2_tp1)
    m_t2_1 = df.groupby("t2_tp1")[["total_return", "max_drawdown", "calmar"]].mean()
    ax3.bar([f"{int(x*100)}%" for x in m_t2_1.index], m_t2_1["calmar"], color="#00C853", alpha=0.85, width=0.4)
    ax3.set_title("Marginal Effect: Tier 2 Batch 1 TP (Fast De-Risking)", fontsize=12, fontweight="bold")
    ax3.set_xlabel("Tier 2 Batch 1 TP", fontsize=10)
    ax3.set_ylabel("Mean Calmar Ratio", fontsize=10)
    ax3.grid(True, alpha=0.3, axis="y")
    for i, v in enumerate(m_t2_1["calmar"]):
        ax3.text(i, v + 0.05, f"{v:.2f}", ha="center", fontsize=10, fontweight="bold")
        
    # 4. Marginal Effect of Tier 3 Escape (t3_tp1)
    m_t3_1 = df.groupby("t3_tp1")[["total_return", "max_drawdown", "calmar", "t3_open"]].mean()
    ax4.bar([f"{int(x*100)}%" for x in m_t3_1.index], m_t3_1["calmar"], color="#AA00FF", alpha=0.85, width=0.4)
    ax4.set_title("Marginal Effect: Tier 3 Batch 1 TP (Deep Escape)", fontsize=12, fontweight="bold")
    ax4.set_xlabel("Tier 3 Batch 1 TP", fontsize=10)
    ax4.set_ylabel("Mean Calmar Ratio", fontsize=10)
    ax4.grid(True, alpha=0.3, axis="y")
    for i, v in enumerate(m_t3_1["calmar"]):
        ax4.text(i, v + 0.05, f"{v:.2f}", ha="center", fontsize=10, fontweight="bold")
        
    plt.tight_layout()
    chart_path = RESULTS_DIR / "tp_6params_optimization_charts.png"
    plt.savefig(chart_path, dpi=200)
    plt.close()
    print(f"\nOptimization dashboard saved to: {chart_path}")

if __name__ == "__main__":
    main()
