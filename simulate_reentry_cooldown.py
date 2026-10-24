"""
Simulate and analyze the "Post-Take-Profit Re-entry Trap" (止盈即追高复开 / 顶峰接盘)
and evaluate Cooldown Timers and Pullback Gating across Gainers, Volatility, and Mcap universes.
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

# Precompute Beijing 08:00 gain matrix
day_start_bars = (np.arange(num_bars) // 24) * 24
base_0800_prices = bars_full[day_start_bars, :, 0]
curr_prices = bars_full[:, :, 0]
with np.errstate(divide='ignore', invalid='ignore'):
    gain_matrix = (curr_prices - base_0800_prices) / base_0800_prices
    valid_mask = (base_0800_prices > 0) & np.isfinite(curr_prices) & np.isfinite(base_0800_prices)
    gain_matrix[~valid_mask] = -9999.0

# Volatility universe
screen_vol = json.loads((DATA / "screen_vol_202512.json").read_text()) if (DATA / "screen_vol_202512.json").exists() else {}
eligible_vol = [s for s in symbols_full if s in screen_vol]
vol196 = sorted(eligible_vol, key=lambda s: -screen_vol[s])[:196]

# Lowest Mcap universe
from build_full_cache import cmc_lookup
cmc = json.loads((DATA / "cmc_20260101.json").read_text()) if (DATA / "cmc_20260101.json").exists() else {}
mcap_map = {s: cmc_lookup(s, cmc) for s in symbols_full}
mcap196 = sorted([s for s in symbols_full if mcap_map[s]], key=lambda s: mcap_map[s]["mcap"])[:196]

def analyze_reentries(label, res):
    events = res["reentry_events"]
    if not events:
        print(f"[{label}] No re-entries recorded.")
        return {}
    df = pd.DataFrame(events)
    n_total = len(df)
    n_1h = (df["delta_h"] <= 1).sum()
    n_6h = (df["delta_h"] <= 6).sum()
    n_24h = (df["delta_h"] <= 24).sum()
    n_higher = (df["ratio"] >= 1.0).sum()
    n_higher_24h = ((df["delta_h"] <= 24) & (df["ratio"] >= 1.0)).sum()
    pct_higher = n_higher / n_total * 100 if n_total > 0 else 0
    pct_1h = n_1h / n_total * 100 if n_total > 0 else 0
    pct_24h = n_24h / n_total * 100 if n_total > 0 else 0
    avg_ratio_24h = df[df["delta_h"] <= 24]["ratio"].mean() if n_24h > 0 else 1.0

    print(f"\n--- [{label}] 追高复开统计 ---")
    print(f"  总复开次数 (Re-entries): {n_total} 次 (占总开仓 {res['tier1_opens']} 次的 {n_total/res['tier1_opens']*100:.1f}%)")
    print(f"  <=1小时内立即复开: {n_1h} 次 ({pct_1h:.1f}%)")
    print(f"  <=6小时内快速复开: {n_6h} 次 ({n_6h/n_total*100:.1f}%)")
    print(f"  <=24小时内复开:    {n_24h} 次 ({pct_24h:.1f}%)")
    print(f"  复开买入价 >= 上次止盈价 (直接追高买回): {n_higher} 次 ({pct_higher:.1f}%)")
    print(f"  24h内且追高买回次数: {n_higher_24h} 次 (24h内复开中占 {n_higher_24h/max(1,n_24h)*100:.1f}%)")
    print(f"  24h内复开买入价/止盈价比率均值: {avg_ratio_24h:.4f} (即平均在止盈价的 {(avg_ratio_24h-1)*100:+.2f}% 追入)")
    
    return {
        "n_reentries": int(n_total),
        "n_1h": int(n_1h),
        "n_6h": int(n_6h),
        "n_24h": int(n_24h),
        "n_higher": int(n_higher),
        "n_higher_24h": int(n_higher_24h),
        "pct_higher": float(pct_higher),
        "pct_1h": float(pct_1h),
        "pct_24h": float(pct_24h),
        "avg_ratio_24h": float(avg_ratio_24h)
    }

def run_suite():
    print("=" * 80)
    print("PART 1: 验证用户的直觉——每小时轮动下，是否存在'刚平仓下一小时就追高开回'？")
    print("=" * 80)
    
    # 1. Gainers screen with Hourly refill (No cooldown)
    cfg_gainers_hourly = T.StrategyConfig(
        cache_prefix="_full",
        funding_matrix=fund_full,
        max_open=200,
        rebalance_hours=tuple(range(1, 24)),
        rank_matrix=gain_matrix
    )
    res_gainers_hourly = T.run_strategy(cfg_gainers_hourly)
    stat_gainers = analyze_reentries("涨幅榜每小时轮动 (无冷却)", res_gainers_hourly)
    
    # 2. Volatility 196 with Hourly refill (No cooldown)
    cfg_vol_hourly = T.StrategyConfig(
        cache_prefix="_full",
        universe_symbols=tuple(vol196),
        funding_matrix=fund_full,
        max_open=200,
        rebalance_hours=tuple(range(24))
    )
    res_vol_hourly = T.run_strategy(cfg_vol_hourly)
    stat_vol = analyze_reentries("波动率196每小时轮动 (无冷却)", res_vol_hourly)

    # 3. Mcap 196 with Daily refill
    cfg_mcap_daily = T.StrategyConfig(
        cache_prefix="_full",
        universe_symbols=tuple(mcap196),
        funding_matrix=fund_full,
        max_open=200,
        rebalance_hours=(8,)
    )
    res_mcap_daily = T.run_strategy(cfg_mcap_daily)
    stat_mcap = analyze_reentries("市值196每日调仓 (无冷却)", res_mcap_daily)

    print("\n" + "=" * 80)
    print("PART 2: 对比实验——引入【冷却期 (Cooldown)】与【回调开仓过滤 (Pullback)】的效果")
    print("=" * 80)

    experiments = [
        # --- 涨幅榜系列 ---
        ("涨幅榜: 无冷却 (Baseline)", dict(
            cache_prefix="_full", funding_matrix=fund_full, max_open=200,
            rebalance_hours=tuple(range(1, 24)), rank_matrix=gain_matrix
        )),
        ("涨幅榜: 冷却 24小时", dict(
            cache_prefix="_full", funding_matrix=fund_full, max_open=200,
            rebalance_hours=tuple(range(1, 24)), rank_matrix=gain_matrix,
            cooldown_hours=24
        )),
        ("涨幅榜: 冷却 48小时", dict(
            cache_prefix="_full", funding_matrix=fund_full, max_open=200,
            rebalance_hours=tuple(range(1, 24)), rank_matrix=gain_matrix,
            cooldown_hours=48
        )),
        ("涨幅榜: 冷却 72小时", dict(
            cache_prefix="_full", funding_matrix=fund_full, max_open=200,
            rebalance_hours=tuple(range(1, 24)), rank_matrix=gain_matrix,
            cooldown_hours=72
        )),
        ("涨幅榜: 需回调 >=5% 方可复开", dict(
            cache_prefix="_full", funding_matrix=fund_full, max_open=200,
            rebalance_hours=tuple(range(1, 24)), rank_matrix=gain_matrix,
            reentry_pullback_pct=0.05
        )),
        ("涨幅榜: 冷却24h + 回调>=5%", dict(
            cache_prefix="_full", funding_matrix=fund_full, max_open=200,
            rebalance_hours=tuple(range(1, 24)), rank_matrix=gain_matrix,
            cooldown_hours=24, reentry_pullback_pct=0.05
        )),

        # --- 波动率系列 ---
        ("波动率196: 无冷却 (Baseline)", dict(
            cache_prefix="_full", universe_symbols=tuple(vol196), funding_matrix=fund_full,
            max_open=200, rebalance_hours=tuple(range(24))
        )),
        ("波动率196: 冷却 24小时", dict(
            cache_prefix="_full", universe_symbols=tuple(vol196), funding_matrix=fund_full,
            max_open=200, rebalance_hours=tuple(range(24)),
            cooldown_hours=24
        )),
        ("波动率196: 冷却 48小时", dict(
            cache_prefix="_full", universe_symbols=tuple(vol196), funding_matrix=fund_full,
            max_open=200, rebalance_hours=tuple(range(24)),
            cooldown_hours=48
        )),
        ("波动率196: 需回调 >=5% 方可复开", dict(
            cache_prefix="_full", universe_symbols=tuple(vol196), funding_matrix=fund_full,
            max_open=200, rebalance_hours=tuple(range(24)),
            reentry_pullback_pct=0.05
        )),
        ("波动率196: 冷却24h + 回调>=5%", dict(
            cache_prefix="_full", universe_symbols=tuple(vol196), funding_matrix=fund_full,
            max_open=200, rebalance_hours=tuple(range(24)),
            cooldown_hours=24, reentry_pullback_pct=0.05
        )),

        # --- 市值系列 (对比) ---
        ("市值196: 无冷却 (Baseline)", dict(
            cache_prefix="_full", universe_symbols=tuple(mcap196), funding_matrix=fund_full,
            max_open=200, rebalance_hours=(8,)
        )),
        ("市值196: 冷却 48小时", dict(
            cache_prefix="_full", universe_symbols=tuple(mcap196), funding_matrix=fund_full,
            max_open=200, rebalance_hours=(8,),
            cooldown_hours=48
        )),
    ]

    results = []
    print(f"\n{'策略配置':<30}{'收益率':>10}{'最大回撤':>10}{'Calmar':>9}{'开仓数':>8}{'复开数':>8}")
    print("-" * 80)
    for name, kw in experiments:
        cfg = T.StrategyConfig(**kw)
        res = T.run_strategy(cfg)
        ret = res["total_return"]
        mdd = res["max_drawdown"]
        calmar = res["calmar"]
        opens = res["tier1_opens"]
        reentries = len(res["reentry_events"])
        print(f"{name:<30}{ret:>9.1f}%{mdd:>9.1f}%{calmar:>9.2f}{opens:>8d}{reentries:>8d}")
        results.append({
            "name": name,
            "return": ret,
            "maxdd": mdd,
            "calmar": calmar,
            "opens": opens,
            "reentries": reentries,
            "history": res["history"]
        })

    # Save results
    (RESULTS / "reentry_cooldown_results.json").write_text(json.dumps([
        {k: v for k, v in r.items() if k != "history"} for r in results
    ], indent=2, ensure_ascii=False))

    # Plot Equity Curves
    plt.figure(figsize=(14, 8))
    for r in results:
        if "Baseline" in r["name"] or "冷却 24小时" in r["name"] or "冷却24h + 回调>=5%" in r["name"]:
            plt.plot(r["history"], label=f"{r['name']} ({r['return']:+.1f}%, DD {r['maxdd']:.1f}%)")
    plt.title("Cooldown Period & Pullback Gate Effect on Equity Curves", fontsize=14, fontweight="bold")
    plt.xlabel("Time (Hours)", fontsize=11)
    plt.ylabel("Portfolio Equity (USDT)", fontsize=11)
    plt.grid(True, alpha=0.3)
    plt.legend(loc="upper left", framealpha=0.9)
    plt.tight_layout()
    plt.savefig(RESULTS / "comparison_cooldown.png", dpi=150)
    plt.close()
    print("\nSaved plot to data/results/comparison_cooldown.png")

if __name__ == "__main__":
    run_suite()
