import json
import time
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import tier_engine as T

DATA = Path("data")
RESULTS = DATA / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

# 1. Load data
bars_full = np.load(DATA / "bars_cache_full.npy")
info_full = json.loads((DATA / "bars_info_full.json").read_text())
hours_full = np.array(info_full["hours"], dtype=np.int32)
symbols_full = info_full["symbols"]
num_bars, num_symbols, _ = bars_full.shape

print(f"Loaded full cache: {num_bars} bars, {num_symbols} symbols.")

# Precompute 08:00 Beijing time open price for every bar
# b_idx % 24 == 0 is UTC 00:00 = Beijing 08:00
# For bar b_idx, the day_start_bar is (b_idx // 24) * 24
day_start_bars = (np.arange(num_bars) // 24) * 24
base_0800_prices = bars_full[day_start_bars, :, 0]  # shape (num_bars, num_symbols)

# Calculate gains relative to 08:00 Beijing open
# gain = (current_open - base_0800_open) / base_0800_open
curr_prices = bars_full[:, :, 0]
with np.errstate(divide='ignore', invalid='ignore'):
    gain_matrix = (curr_prices - base_0800_prices) / base_0800_prices
    # mask NaNs and non-positive prices
    valid_mask = (base_0800_prices > 0) & np.isfinite(curr_prices) & np.isfinite(base_0800_prices)
    gain_matrix[~valid_mask] = -9999.0

# Build rank matrices:
# Rank matrix: higher value = higher priority to open
# 1) Top Gainers rank matrix (higher gain = higher rank)
rank_gainers = gain_matrix.copy()

# 2) Top Losers rank matrix (lower gain / larger drop = higher rank)
rank_losers = -gain_matrix.copy()
rank_losers[~valid_mask] = -9999.0

# 3) Fixed 9:00 AM snapshot rank:
# For each day, the rank computed at UTC 01:00 (Beijing 09:00) is held constant for the whole 24 hours of that day
rank_daily_0900_gainers = np.full_like(gain_matrix, -9999.0)
rank_daily_0900_losers = np.full_like(gain_matrix, -9999.0)

num_days = num_bars // 24
for d in range(num_days):
    b_0900 = d * 24 + 1  # UTC 01:00 is Beijing 09:00
    if b_0900 < num_bars:
        row_g = gain_matrix[b_0900]
        row_l = rank_losers[b_0900]
        # broadcast to all 24 hours of that day
        d_start = d * 24
        d_end = min(num_bars, (d + 1) * 24)
        rank_daily_0900_gainers[d_start:d_end] = row_g
        rank_daily_0900_losers[d_start:d_end] = row_l

# Load real funding matrix
fund_full = np.load(DATA / "funding_matrix_full.npy")
fund_frozen = np.load(DATA / "funding_matrix.npy")

# Precompute other benchmarks:
# Volatility 196
screen_vol = json.loads((DATA / "screen_vol_202512.json").read_text()) if (DATA / "screen_vol_202512.json").exists() else {}
eligible_vol = [s for s in symbols_full if s in screen_vol]
vol196 = sorted(eligible_vol, key=lambda s: -screen_vol[s])[:196]

# Lowest Mcap 196
cmc = json.loads((DATA / "cmc_20260101.json").read_text()) if (DATA / "cmc_20260101.json").exists() else {}
from build_full_cache import cmc_lookup
mcap_map = {s: cmc_lookup(s, cmc) for s in symbols_full}
mcap196 = sorted([s for s in symbols_full if mcap_map[s]], key=lambda s: mcap_map[s]["mcap"])[:196]

print("Running comparative experiments...")

configs = [
    # 1. User's proposed mode: Daily at Beijing 09:00 (UTC 01:00), Top 200 Gainers
    ("1. 涨幅榜前200 (每天北京09:00定点调仓)", {
        "cache_prefix": "_full",
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": (1,),  # UTC 01:00 = Beijing 09:00
        "rank_matrix": rank_gainers
    }),
    
    # 2. User's proposed mode: Daily starting from 09:00, Hourly dynamic refill
    ("2. 涨幅榜前200 (北京09:00起每小时动态轮动)", {
        "cache_prefix": "_full",
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": tuple(range(1, 24)),  # UTC 01:00 to 23:00 (Beijing 09:00 to 07:00 next day)
        "rank_matrix": rank_gainers
    }),

    # 3. Daily 09:00 Snapshot + Hourly refill within that day's top gainers
    ("3. 涨幅榜前200 (09:00快照锁定池 + 每小时补仓)", {
        "cache_prefix": "_full",
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": tuple(range(24)),
        "rank_matrix": rank_daily_0900_gainers
    }),

    # 4. Contrast: Top 200 Losers (跌幅榜前200，每天北京09:00定点调仓)
    ("4. 跌幅榜前200 (每天北京09:00定点抄底)", {
        "cache_prefix": "_full",
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": (1,),
        "rank_matrix": rank_losers
    }),

    # 5. Contrast: Top 200 Losers (跌幅榜前200，北京09:00起每小时动态抄底)
    ("5. 跌幅榜前200 (北京09:00起每小时动态抄底)", {
        "cache_prefix": "_full",
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": tuple(range(1, 24)),
        "rank_matrix": rank_losers
    }),

    # 6. Benchmark: Lowest Market Cap 196 + Daily
    ("6. [基准] 最小市值196 (每日08:00 UTC调仓)", {
        "cache_prefix": "_full",
        "universe_symbols": tuple(mcap196),
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": (8,)
    }),

    # 7. Benchmark: Realized Volatility 196 + Hourly
    ("7. [基准] 纯波动率196 (每小时轮动)", {
        "cache_prefix": "_full",
        "universe_symbols": tuple(vol196),
        "funding_matrix": fund_full,
        "max_open": 200,
        "rebalance_hours": tuple(range(24))
    }),

    # 8. Benchmark: Frozen Baseline Universe 200
    ("8. [原始基准] 初始冻结200标的 (Daily 08:00)", {
        "funding_matrix": fund_frozen,
        "rebalance_hours": (8,)
    })
]

results = []
for label, kw in configs:
    t0 = time.time()
    cfg = T.StrategyConfig(**kw)
    r = T.run_strategy(cfg)
    el = time.time() - t0
    r["label"] = label
    results.append(r)
    print(f"[{label}] -> Ret: {r['total_return']:+.1f}% | MaxDD: {r['max_drawdown']:.1f}% | Eff: {r['calmar']:.2f} | Lowest: ${r['lowest_equity']:.1f} | Opens: {r['tier1_opens']} | Time: {el:.1f}s")

print("\n" + "=" * 125)
print(f"{'配置方案':<44}{'期末净值':>14}{'总收益率':>12}{'最大回撤':>12}{'收益/回撤比':>14}{'最低净值':>12}{'总开仓数':>10}")
print("=" * 125)
for r in results:
    status = "💥 爆仓" if r["is_liquidated"] else f"${r['final_equity']:,.2f}"
    print(f"{r['label']:<42}{status:>14}{r['total_return']:>11.1f}%{r['max_drawdown']:>11.1f}%{r['calmar']:>13.2f}${r['lowest_equity']:>11.1f}{r['tier1_opens']:>10d}")
print("=" * 125)

# Save results JSON
res_json = [{
    "label": r["label"],
    "final_equity": r["final_equity"],
    "total_return": r["total_return"],
    "max_drawdown": r["max_drawdown"],
    "calmar": r["calmar"],
    "lowest_equity": r["lowest_equity"],
    "tier1_opens": r["tier1_opens"],
    "dca_filled": r["dca_filled"],
    "funding_paid": r["funding_paid"]
} for r in results]
(RESULTS / "comparison_gainers_screen.json").write_text(json.dumps(res_json, indent=2, ensure_ascii=False))

# Plot equity curves comparison
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), sharex=True, gridspec_kw={"height_ratios": [2.5, 1]})
timestamps = pd.date_range("2026-01-01", periods=num_bars, freq="1h")

en_labels = [
    "1. Top 200 Gainers (Daily 09:00 BJ)",
    "2. Top 200 Gainers (Hourly Rolling from 09:00 BJ)",
    "3. Top 200 Gainers (09:00 Snapshot + Hourly Refill)",
    "4. Top 200 Losers (Daily 09:00 BJ)",
    "5. Top 200 Losers (Hourly Rolling from 09:00 BJ)",
    "6. [Benchmark] Lowest Mcap 196 (Daily 08:00 UTC)",
    "7. [Benchmark] Highest Volatility 196 (Hourly)",
    "8. [Base] Frozen Universe 200 (Daily 08:00 UTC)"
]

colors = ["#FF5722", "#E91E63", "#9C27B0", "#009688", "#4CAF50", "#2196F3", "#3F51B5", "#787B86"]
for i, r in enumerate(results):
    h = np.asarray(r["history"], dtype=float)
    peak = np.maximum.accumulate(h)
    dd = (h - peak) / peak * 100.0
    lbl = f"{en_labels[i]} (Ret: {r['total_return']:+.0f}%, DD: {r['max_drawdown']:.0f}%, Calmar: {r['calmar']:.2f})"
    ax1.plot(timestamps[:len(h)], h, label=lbl, color=colors[i % len(colors)], linewidth=2.0 if i < 3 else 1.4)
    ax2.plot(timestamps[:len(dd)], dd, color=colors[i % len(colors)], linewidth=1.2)

ax1.axhline(1000.0, color="black", linestyle=":", alpha=0.5, label="Initial Capital ($1,000)")
ax1.set_title("Universe Selection: Beijing 08:00-09:00 Top Gainers vs Losers vs Mcap vs Volatility", fontsize=13, fontweight="bold", pad=12)
ax1.set_ylabel("Account Equity (USD)", fontsize=11)
ax1.legend(loc="upper left", fontsize=8.5, framealpha=0.9)
ax1.grid(True, alpha=0.25)

ax2.axhline(0, color="black", linewidth=0.8, alpha=0.4)
ax2.set_ylabel("Drawdown (%)", fontsize=11)
ax2.set_xlabel("Date (2026)", fontsize=11)
ax2.grid(True, alpha=0.25)

plt.tight_layout()
chart_path = RESULTS / "comparison_gainers_screen.png"
plt.savefig(chart_path, dpi=200)
plt.close()
print("Saved comparison chart to:", chart_path)
