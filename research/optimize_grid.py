import os
import json
import time
from pathlib import Path
from multiprocessing import Pool
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from fast_engine import prepare_data, simulate_fast

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

CACHE_BARS = DATA_DIR / "bars_cache.npy"
CACHE_INFO = DATA_DIR / "bars_info.json"

def get_cached_data():
    if CACHE_BARS.exists() and CACHE_INFO.exists():
        info = json.loads(CACHE_INFO.read_text())
        bars = np.load(CACHE_BARS)
        hours = np.array(info["hours"], dtype=np.int32)
        valid_symbols = info["symbols"]
        return valid_symbols, bars, hours
    else:
        valid_symbols, timestamps, bars, hours = prepare_data()
        np.save(CACHE_BARS, bars)
        info = {
            "symbols": valid_symbols,
            "hours": hours.tolist(),
            "num_bars": len(timestamps)
        }
        CACHE_INFO.write_text(json.dumps(info))
        return valid_symbols, bars, hours

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

def worker_task(params):
    global G_BARS, G_HOURS, G_NUM_SYMBOLS, G_NUM_BARS
    t0 = time.time()
    res = simulate_fast(params, G_BARS, G_HOURS, G_NUM_SYMBOLS, G_NUM_BARS)
    res["elapsed"] = time.time() - t0
    return res

def generate_param_grid():
    grid = []
    
    # Baseline
    grid.append({
        "name": "基准 (原版规则 200标的)",
        "dca_drop_1": 0.50, "dca_mult_1": 2.0,
        "dca_drop_2": 0.50, "dca_mult_2": 2.0,
        "max_tiers": 3,
        "tp_tier1": 0.20, "tp_escape": 0.20, "tp_splits": 3
    })
    
    # Group A: 砍掉 Tier 3 (max_tiers = 2, 强制双阶止损，杜绝 90U 死套)
    for d1 in [0.30, 0.40, 0.50]:
        for m1 in [1.0, 1.5, 2.0]:
            for tp1 in [0.10, 0.15, 0.20]:
                for esc in [0.05, 0.10]:
                    for sp in [1, 2]:
                        grid.append({
                            "name": f"双阶_跌{int(d1*100)}%_倍{m1}x_TP{int(tp1*100)}%_逃{int(esc*100)}%_分{sp}",
                            "dca_drop_1": d1, "dca_mult_1": m1,
                            "dca_drop_2": 0.50, "dca_mult_2": 1.0,
                            "max_tiers": 2,
                            "tp_tier1": tp1, "tp_escape": esc, "tp_splits": sp
                        })
                        
    # Group B: 保留 Tier 3，但进行深层快速逃生与线性补仓
    for d1 in [0.35, 0.50]:
        for m1 in [1.0, 1.5, 2.0]:
            for d2 in [0.30, 0.50]:
                for m2 in [1.0, 2.0]:
                    for esc in [0.05, 0.10]:
                        for sp in [1, 2]:
                            grid.append({
                                "name": f"三阶_D1跌{int(d1*100)}%倍{m1}_D2跌{int(d2*100)}%倍{m2}_逃{int(esc*100)}%_分{sp}",
                                "dca_drop_1": d1, "dca_mult_1": m1,
                                "dca_drop_2": d2, "dca_mult_2": m2,
                                "max_tiers": 3,
                                "tp_tier1": 0.15, "tp_escape": esc, "tp_splits": sp
                            })
                            
    # Deduplicate by parameter tuple
    seen = set()
    unique_grid = []
    for p in grid:
        k = (p["dca_drop_1"], p["dca_mult_1"], p["dca_drop_2"], p["dca_mult_2"], p["max_tiers"], p["tp_tier1"], p["tp_escape"], p["tp_splits"])
        if k not in seen:
            seen.add(k)
            unique_grid.append(p)
            
    return unique_grid

def main():
    print("Preparing and caching pre-aligned K-lines...")
    valid_symbols, bars, hours = get_cached_data()
    print(f"Dataset ready: {len(valid_symbols)} symbols, {len(hours)} hourly bars.")
    
    param_grid = generate_param_grid()
    print(f"Total parameter combinations to evaluate: {len(param_grid)}")
    
    t_start = time.time()
    results = []
    
    # Run in parallel across 8 workers
    with Pool(processes=8, initializer=init_worker, initargs=(str(CACHE_BARS), str(CACHE_INFO))) as pool:
        for i, res in enumerate(pool.imap_unordered(worker_task, param_grid), 1):
            results.append(res)
            if i % 20 == 0 or i == len(param_grid):
                print(f"Progress: {i}/{len(param_grid)} evaluated ({(i/len(param_grid))*100:.1f}%) in {time.time()-t_start:.1f}s")
                
    print(f"\nAll {len(results)} simulations completed in {time.time()-t_start:.2f}s!")
    
    # Process results into DataFrame
    flat_rows = []
    for r in results:
        p = r["params"]
        row = {
            "name": p.get("name", ""),
            "max_tiers": p["max_tiers"],
            "dca_drop_1": p["dca_drop_1"],
            "dca_mult_1": p["dca_mult_1"],
            "dca_drop_2": p["dca_drop_2"],
            "dca_mult_2": p["dca_mult_2"],
            "tp_tier1": p["tp_tier1"],
            "tp_escape": p["tp_escape"],
            "tp_splits": p["tp_splits"],
            "total_return_pct": r["total_return"],
            "final_equity": r["final_equity"],
            "lowest_equity": r["lowest_equity"],
            "max_drawdown_pct": r["max_drawdown"],
            "calmar_ratio": r["calmar"],
            "total_trades": r["total_trades"],
            "realized_pnl": r["realized_pnl"],
            "open_positions": r["open_positions"],
            "is_liquidated": r["is_liquidated"]
        }
        flat_rows.append(row)
        
    df_res = pd.DataFrame(flat_rows)
    df_res.to_csv(RESULTS_DIR / "optimization_results.csv", index=False)
    print(f"Full results saved to {RESULTS_DIR / 'optimization_results.csv'}")
    
    # Filter out liquidated
    df_survived = df_res[~df_res["is_liquidated"]].copy()
    print(f"\nSurvived configurations: {len(df_survived)} / {len(df_res)}")
    
    # 1. Top 5 by Calmar Ratio (Best Risk-Reward)
    top_calmar = df_survived.sort_values(by="calmar_ratio", ascending=False).head(5)
    print("\n" + "="*95)
    print("                TOP 5 CONFIGURATIONS BY CALMAR RATIO (最佳性价比 / 收益回撤比)                ")
    print("="*95)
    cols = ["max_tiers", "dca_drop_1", "dca_mult_1", "tp_tier1", "tp_escape", "tp_splits", "total_return_pct", "max_drawdown_pct", "calmar_ratio", "final_equity", "lowest_equity"]
    print(top_calmar[cols].to_string(index=False))
    
    # 2. Top 5 by Lowest Drawdown (Most Conservative / Safe)
    top_safe = df_survived.sort_values(by="max_drawdown_pct", ascending=False).head(5)
    print("\n" + "="*95)
    print("                TOP 5 CONFIGURATIONS BY LOWEST DRAWDOWN (最稳健防守型 / 回撤最小)              ")
    print("="*95)
    print(top_safe[cols].to_string(index=False))
    
    # 3. Top 5 by Total Return (Maximum Profit)
    top_return = df_survived.sort_values(by="total_return_pct", ascending=False).head(5)
    print("\n" + "="*95)
    print("                TOP 5 CONFIGURATIONS BY TOTAL RETURN (极致暴利型 / 绝对收益最高)               ")
    print("="*95)
    print(top_return[cols].to_string(index=False))
    print("="*95)

if __name__ == "__main__":
    main()
