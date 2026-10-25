"""
Evaluate Trend-Following and Momentum Universe Selection Strategies:
Can we find 'True Uptrends' (真正的上升趋势) where the strategy takes profit repeatedly,
locking in gains along the trend, and only gets trapped once at the final peak?

Tested Candidates:
1. Donchian Channel Breakout (30-day High / 距离30日最高价最近)
2. Kaufman Efficiency Ratio (72h 趋势平滑度 / 低噪音主升浪)
3. Multi-Timeframe Momentum (MTF: 24h + 7d + 30d 动量共振)
4. EMA Bullish Alignment (EMA24 > EMA72 > EMA168 均线多头排列)
5. Relative Strength vs BTC (强于大盘领涨币)
"""

import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import tier_engine as T

DATA = Path("data")
RESULTS = DATA / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

# 1. Load full bars cache and funding
bars_full = np.load(DATA / "bars_cache_full.npy")
info_full = json.loads((DATA / "bars_info_full.json").read_text())
symbols_full = info_full["symbols"]
fund_full = np.load(DATA / "funding_matrix_full.npy")
num_bars, num_symbols, _ = bars_full.shape

print(f"Loaded full cache: {num_bars} bars, {num_symbols} symbols.")

closes = bars_full[:, :, 3]
highs = bars_full[:, :, 1]
lows = bars_full[:, :, 2]
opens = bars_full[:, :, 0]

# --- 1. Donchian 30d High Rank (720 bars lookback) ---
print("Computing 30d High Proximity Rank...")
# Proximity = current_close / rolling_max_high_30d
rolling_high_30d = np.full_like(closes, np.nan)
for b in range(720, num_bars):
    rolling_high_30d[b] = np.nanmax(highs[b-720:b], axis=0)

with np.errstate(divide='ignore', invalid='ignore'):
    rank_donchian_30d = closes / rolling_high_30d
    rank_donchian_30d[~np.isfinite(rank_donchian_30d)] = -9999.0

# --- 2. Kaufman Efficiency Ratio (72h = 3 days) ---
print("Computing 72h Kaufman Efficiency Ratio (KER)...")
# KER = abs(P_t - P_{t-72}) / sum(abs(P_i - P_{i-1}))
# Weighted by direction: if P_t > P_{t-72}, score = +KER, else -KER
rank_ker = np.full_like(closes, -9999.0)
for b in range(72, num_bars):
    net_chg = closes[b] - closes[b-72]
    path_len = np.nansum(np.abs(np.diff(closes[b-72:b+1], axis=0)), axis=0)
    with np.errstate(divide='ignore', invalid='ignore'):
        ker = np.where(path_len > 0, net_chg / path_len, 0.0)
        ker[~np.isfinite(ker)] = -9999.0
        rank_ker[b] = ker

# --- 3. Multi-Timeframe Momentum (24h + 7d + 30d) ---
print("Computing Multi-Timeframe Momentum (MTF)...")
rank_mtf = np.full_like(closes, -9999.0)
for b in range(720, num_bars):
    r_24h = (closes[b] - closes[b-24]) / closes[b-24]
    r_7d = (closes[b] - closes[b-168]) / closes[b-168]
    r_30d = (closes[b] - closes[b-720]) / closes[b-720]
    score = 0.2 * r_24h + 0.5 * r_7d + 0.3 * r_30d
    score[~np.isfinite(score)] = -9999.0
    rank_mtf[b] = score

# --- 4. EMA Alignment (EMA 24 > EMA 72 > EMA 168) ---
print("Computing EMA Alignment Score...")
# Approximate EMA using rolling mean
rank_ema = np.full_like(closes, -9999.0)
for b in range(168, num_bars):
    ma24 = np.nanmean(closes[b-24:b], axis=0)
    ma72 = np.nanmean(closes[b-72:b], axis=0)
    ma168 = np.nanmean(closes[b-168:b], axis=0)
    # Score is higher if close > ma24 > ma72 > ma168
    bull_count = ((closes[b] > ma24).astype(float) +
                  (ma24 > ma72).astype(float) +
                  (ma72 > ma168).astype(float))
    # Combine with slope of ma24
    slope = (ma24 - np.nanmean(closes[b-48:b-24], axis=0)) / ma24
    score = bull_count + slope
    score[~np.isfinite(score)] = -9999.0
    rank_ema[b] = score

# --- 5. Relative Strength vs BTC ---
print("Computing Relative Strength vs BTC...")
btc_idx = symbols_full.index("BTCUSDT") if "BTCUSDT" in symbols_full else 0
btc_closes = closes[:, btc_idx]
rank_rs_btc = np.full_like(closes, -9999.0)
for b in range(168, num_bars):
    btc_7d = (btc_closes[b] - btc_closes[b-168]) / btc_closes[b-168]
    alt_7d = (closes[b] - closes[b-168]) / closes[b-168]
    rs = alt_7d - btc_7d
    rs[~np.isfinite(rs)] = -9999.0
    rank_rs_btc[b] = rs

# Load benchmarks
cmc = json.loads((DATA / "cmc_20260101.json").read_text()) if (DATA / "cmc_20260101.json").exists() else {}
from build_full_cache import cmc_lookup
mcap_map = {s: cmc_lookup(s, cmc) for s in symbols_full}
mcap196 = sorted([s for s in symbols_full if mcap_map[s]], key=lambda s: mcap_map[s]["mcap"])[:196]

with open(DATA / "screen_vol_202512.json") as f:
    vol_screen = json.load(f)
vol196 = sorted([s for s in symbols_full if s in vol_screen], key=lambda s: -vol_screen[s])[:196]

print("Setting up test suite...")

# Note: We test both Daily 08:00 rebalance and Hourly rebalance with cooldown_hours=1
# (cooldown_hours=1 ensures NO same-bar retroactive cheating!)

experiments = [
    # --- Benchmarks ---
    ("基准: 最小市值196 (每日08:00)", dict(
        cache_prefix="_full", universe_symbols=tuple(mcap196), funding_matrix=fund_full,
        max_open=200, rebalance_hours=(8,)
    )),
    ("基准: 纯波动率196 (每日08:00)", dict(
        cache_prefix="_full", universe_symbols=tuple(vol196), funding_matrix=fund_full,
        max_open=200, rebalance_hours=(8,)
    )),
    ("基准: 纯波动率196 (每小时真实轮动 cd=1h)", dict(
        cache_prefix="_full", universe_symbols=tuple(vol196), funding_matrix=fund_full,
        max_open=200, rebalance_hours=tuple(range(24)), cooldown_hours=1
    )),

    # --- 1. Donchian 30d High Breakout (创30日新高/顺势突破) ---
    ("1. 唐奇安30日新高 (每日08:00调仓)", dict(
        cache_prefix="_full", funding_matrix=fund_full, max_open=200,
        rebalance_hours=(8,), rank_matrix=rank_donchian_30d
    )),
    ("2. 唐奇安30日新高 (每小时轮动 cd=1h)", dict(
        cache_prefix="_full", funding_matrix=fund_full, max_open=200,
        rebalance_hours=tuple(range(24)), rank_matrix=rank_donchian_30d, cooldown_hours=1
    )),

    # --- 2. Kaufman Efficiency Ratio (平滑主升浪 / 低噪音趋势) ---
    ("3. 卡夫曼平滑趋势 (每日08:00调仓)", dict(
        cache_prefix="_full", funding_matrix=fund_full, max_open=200,
        rebalance_hours=(8,), rank_matrix=rank_ker
    )),
    ("4. 卡夫曼平滑趋势 (每小时轮动 cd=1h)", dict(
        cache_prefix="_full", funding_matrix=fund_full, max_open=200,
        rebalance_hours=tuple(range(24)), rank_matrix=rank_ker, cooldown_hours=1
    )),

    # --- 3. Multi-Timeframe Momentum (多周期动量共振 24h+7d+30d) ---
    ("5. 多周期动量共振 (每日08:00调仓)", dict(
        cache_prefix="_full", funding_matrix=fund_full, max_open=200,
        rebalance_hours=(8,), rank_matrix=rank_mtf
    )),
    ("6. 多周期动量共振 (每小时轮动 cd=1h)", dict(
        cache_prefix="_full", funding_matrix=fund_full, max_open=200,
        rebalance_hours=tuple(range(24)), rank_matrix=rank_mtf, cooldown_hours=1
    )),

    # --- 4. EMA Bullish Alignment (均线多头排列) ---
    ("7. 均线多头排列 (每日08:00调仓)", dict(
        cache_prefix="_full", funding_matrix=fund_full, max_open=200,
        rebalance_hours=(8,), rank_matrix=rank_ema
    )),

    # --- 5. Relative Strength vs BTC (强于大盘领涨币) ---
    ("8. 强于大盘RS (每日08:00调仓)", dict(
        cache_prefix="_full", funding_matrix=fund_full, max_open=200,
        rebalance_hours=(8,), rank_matrix=rank_rs_btc
    )),
]

results = []
print(f"\n{'趋势选标策略':<35}{'收益率':>10}{'最大回撤':>10}{'Calmar':>9}{'开仓数':>8}{'交易数':>8}")
print("-" * 85)
for name, kw in experiments:
    cfg = T.StrategyConfig(**kw)
    res = T.run_strategy(cfg)
    ret = res["total_return"]
    mdd = res["max_drawdown"]
    calmar = res["calmar"]
    opens = res["tier1_opens"]
    trades = res["total_trades"]
    print(f"{name:<35}{ret:>9.1f}%{mdd:>9.1f}%{calmar:>9.2f}{opens:>8d}{trades:>8d}")
    results.append({
        "name": name,
        "return": ret,
        "maxdd": mdd,
        "calmar": calmar,
        "opens": opens,
        "trades": trades,
        "history": res["history"]
    })

# Save results
(RESULTS / "trend_selection_results.json").write_text(json.dumps([
    {k: v for k, v in r.items() if k != "history"} for r in results
], indent=2, ensure_ascii=False))

# Plot
plt.figure(figsize=(14, 8))
for r in results:
    if "基准" in r["name"] or "唐奇安30日新高" in r["name"] or "多周期动量" in r["name"] or "卡夫曼" in r["name"]:
        label = f"{r['name']} ({r['return']:+.1f}%, DD {r['maxdd']:.1f}%)"
        plt.plot(r["history"], label=label)
plt.title("Trend-Following Universe Selection: Real Up-Trends vs Baseline", fontsize=14, fontweight="bold")
plt.xlabel("Hours", fontsize=11)
plt.ylabel("Equity (USDT)", fontsize=11)
plt.grid(True, alpha=0.3)
plt.legend(loc="upper left", framealpha=0.9)
plt.tight_layout()
plt.savefig(RESULTS / "comparison_trend_selection.png", dpi=150)
plt.close()
print("Saved plot to data/results/comparison_trend_selection.png")
