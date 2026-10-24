import json
import time
from pathlib import Path
import numpy as np
import pandas as pd
import tier_engine as T

DATA = Path("data")
RESULTS = DATA / "results"

bars_full = np.load(DATA / "bars_cache_full.npy")
info_full = json.loads((DATA / "bars_info_full.json").read_text())
hours_full = np.array(info_full["hours"], dtype=np.int32)
symbols_full = info_full["symbols"]
num_bars, num_symbols, _ = bars_full.shape

fund_full = np.load(DATA / "funding_matrix_full.npy")

# Compute rolling 7-day (168h) volatility rank matrix
# For bar b, vol is std of hourly log returns over [b-168, b)
print("Computing rolling 7d (168h) volatility matrix...")
t0 = time.time()
closes = bars_full[:, :, 3]

# Log returns: diff along axis 0
log_closes = np.log(np.where(closes > 0, closes, np.nan))
log_rets = np.diff(log_closes, axis=0)  # shape (num_bars - 1, num_symbols)

# Pad 1 row at top
log_rets_padded = np.vstack([np.zeros((1, num_symbols)), log_rets])

# Rolling std over 168 hours
# Using rolling window via stride or pandas
df_rets = pd.DataFrame(log_rets_padded)
rolling_7d_std = df_rets.rolling(window=168, min_periods=120).std().values
rolling_30d_std = df_rets.rolling(window=720, min_periods=500).std().values

print(f"Computed in {time.time()-t0:.1f}s.")

# Static pre-period 7d screen: last 168 hours of pre-period data if available
# Or rolling 7d updated daily at 08:00 UTC
# Let's test 4 clear variations:
# 1) Dynamic Rolling 7d Volatility (Daily 08:00 UTC update)
# 2) Dynamic Rolling 7d Volatility (Hourly update)
# 3) Dynamic Rolling 30d Volatility (Hourly update)
# 4) Benchmark: Vol-196 30d static (from simulate_final_config)
# 5) Benchmark: Lowest Mcap 196 (from simulate_final_config)

# For daily update, broadcast 08:00 values across 24h
rank_7d_daily = np.zeros_like(rolling_7d_std)
for d in range(num_bars // 24):
    b0 = d * 24 + 8
    if b0 < num_bars:
        val = rolling_7d_std[b0]
        rank_7d_daily[d*24:(d+1)*24] = val

configs = [
    ("1. 动态滚动 7日波动率 (每小时更新优选)", {
        "cache_prefix": "_full",
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": tuple(range(24)),
        "rank_matrix": rolling_7d_std
    }),
    ("2. 动态滚动 7日波动率 (每日08:00更新优选)", {
        "cache_prefix": "_full",
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": (8,),
        "rank_matrix": rank_7d_daily
    }),
    ("3. 动态滚动 30日波动率 (每小时更新优选)", {
        "cache_prefix": "_full",
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": tuple(range(24)),
        "rank_matrix": rolling_30d_std
    }),
    ("4. [对照] 静态30日波动率196 (前置样本外筛选+每小时)", {
        "cache_prefix": "_full",
        "universe_symbols": tuple(json.loads((DATA / "comparison_final_config_curves.json").read_text())["vol_screen"]["symbols"] if (DATA / "comparison_final_config_curves.json").exists() else ()),
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": tuple(range(24))
    }),
    ("5. [对照] 最小市值196 (每小时轮动)", {
        "cache_prefix": "_full",
        "universe_symbols": tuple(json.loads((DATA / "comparison_final_config_curves.json").read_text())["lowest_mcap"]["symbols"] if (DATA / "comparison_final_config_curves.json").exists() else ()),
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": tuple(range(24))
    })
]

print("\nRunning 7-day volatility comparison backtests...")
results = []
for label, kw in configs:
    if not kw.get("universe_symbols", (1,)) and "universe_symbols" in kw:
        continue
    t0 = time.time()
    cfg = T.StrategyConfig(**kw)
    r = T.run_strategy(cfg)
    el = time.time() - t0
    r["label"] = label
    results.append(r)
    print(f"[{label}] -> Ret: {r['total_return']:+.1f}% | MaxDD: {r['max_drawdown']:.1f}% | Eff: {r['calmar']:.2f} | Lowest: ${r['lowest_equity']:.1f} | Opens: {r['tier1_opens']} | Time: {el:.1f}s")

print("\n" + "=" * 120)
print(f"{'配置方案':<46}{'期末净值':>14}{'总收益率':>12}{'最大回撤':>12}{'收益/回撤比':>14}{'最低净值':>12}{'总开仓数':>10}")
print("=" * 120)
for r in results:
    status = "💥 爆仓" if r["is_liquidated"] else f"${r['final_equity']:,.2f}"
    print(f"{r['label']:<44}{status:>14}{r['total_return']:>11.1f}%{r['max_drawdown']:>11.1f}%{r['calmar']:>13.2f}${r['lowest_equity']:>11.1f}{r['tier1_opens']:>10d}")
print("=" * 120)

(RESULTS / "comparison_7d_vol.json").write_text(json.dumps([{
    "label": r["label"],
    "final_equity": r["final_equity"],
    "total_return": r["total_return"],
    "max_drawdown": r["max_drawdown"],
    "calmar": r["calmar"],
    "lowest_equity": r["lowest_equity"],
    "tier1_opens": r["tier1_opens"]
} for r in results], indent=2, ensure_ascii=False))
