"""
Plot the four candidate production configurations against each other.

All four trade the full 2026-01-01..2026-09-30 period with REAL funding rates.

  Current design      frozen universe_200, daily 08:00 slot refill
  Hourly refill only  same frozen universe, slots refilled every hour
  Lowest-mcap + hourly  196 slots chosen as the smallest market caps
  Vol-196 + hourly      196 slots chosen PURELY by ex-ante volatility

The volatility screen does NOT consider market cap at all: it ranks all 565
contracts with a usable 2025-12 window by realised volatility and takes the top
196 -- no market-cap filter, no liquidity filter. The resulting set happens to
have a much larger median market cap than the mcap rule's set, but that is an
outcome, not a criterion.

Output: data/results/comparison_final_config.png
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

import tier_engine as T
import simulate_final_config as F
from build_full_cache import cmc_lookup

DATA = Path("data")
RESULTS = DATA / "results"
SLOTS = 196


def equity_curve(cfg_kwargs):
    r = T.run_strategy(T.StrategyConfig(**cfg_kwargs))
    h = np.asarray(r["history"], dtype=float)
    peak = np.maximum.accumulate(h)
    return dict(res=r, eq=h, dd=(h - peak) / peak * 100.0)


def main():
    base = T.run_strategy(T.StrategyConfig())
    assert abs(base["final_equity"] - 3867.8487458025065) < 1e-6

    symbols, screen, cmc, fund_full, fund_frozen = F.load()
    eligible = [s for s in symbols if s in screen]
    vol196 = sorted(eligible, key=lambda s: -screen[s])[:SLOTS]          # volatility only
    mcap_map = {s: cmc_lookup(s, cmc) for s in eligible}
    mcap196 = sorted([s for s in eligible if mcap_map[s]],
                     key=lambda s: mcap_map[s]["mcap"])[:SLOTS]

    arms = [
        ("Current design  (frozen u200, daily refill)",
         dict(funding_matrix=fund_frozen), "#7f7f7f"),
        ("Hourly refill only  (frozen u200)",
         dict(funding_matrix=fund_frozen, rebalance_hours=F.HOURLY), "#1f77b4"),
        ("Lowest-mcap-196 + hourly",
         dict(cache_prefix="_full", universe_symbols=tuple(mcap196),
              funding_matrix=fund_full, rebalance_hours=F.HOURLY), "#2ca02c"),
        ("Vol-196 + hourly  (no market-cap filter)",
         dict(cache_prefix="_full", universe_symbols=tuple(vol196),
              funding_matrix=fund_full, rebalance_hours=F.HOURLY), "#d62728"),
    ]

    series = []
    print(f"{'configuration':<44}{'return':>10}{'maxDD':>9}{'ret/|DD|':>10}{'lowest':>10}")
    for label, kw, color in arms:
        s = equity_curve(kw)
        r = s["res"]
        series.append((label, s, color))
        print(f"{label:<44}{r['total_return']:>9.1f}%{r['max_drawdown']:>8.1f}%"
              f"{r['total_return'] / abs(r['max_drawdown']):>10.2f}{r['lowest_equity']:>10.1f}")

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9.5), sharex=True,
                                   gridspec_kw={"height_ratios": [2.4, 1]})
    last = pd.date_range("2026-01-01", periods=len(series[0][1]["eq"]), freq="h")[-1]
    for label, s, color in series:
        idx = pd.date_range("2026-01-01", periods=len(s["eq"]), freq="h")
        r = s["res"]
        ax1.plot(idx, s["eq"], color=color, linewidth=1.9,
                 label=f"{label}   {r['total_return']:+.0f}% / DD {r['max_drawdown']:.0f}%")
        ax2.plot(idx, s["dd"], color=color, linewidth=1.5)

    for ax in (ax1, ax2):
        ax.set_xlim(pd.Timestamp("2025-12-24"), last + pd.Timedelta(days=16))
    ax2.set_ylim(-62, 4)
    ax1.axhline(1000.0, color="black", linewidth=0.8, alpha=0.4)
    ax1.set_ylabel("Equity (USDT)")
    ax1.set_title("Production candidate configurations — Binance USDT-M, full 821-contract pool\n"
                  "2026-01-01 to 2026-09-30, real historical funding; universe screened on 2025-12 klines",
                  fontsize=12)
    ax1.grid(alpha=0.3)
    ax1.legend(loc="upper left", fontsize=9.5, framealpha=0.95)
    ax2.axhline(0, color="black", linewidth=0.8, alpha=0.4)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_xlabel("Date")
    ax2.grid(alpha=0.3)

    crash = pd.Timestamp("2026-02-05 23:00")
    ax1.axvline(crash, color="crimson", linewidth=1.0, alpha=0.45)
    ax1.annotate("2026-02-05 crash", xy=(crash, 900), xytext=(pd.Timestamp("2026-03-10"), 640),
                 fontsize=8.5, color="crimson",
                 arrowprops=dict(arrowstyle="->", color="crimson", lw=1.0))

    # the risk trade-off that separates the two screens
    ax2.text(0.012, 0.05,
             "Feb-2026 crash depth (max drawdown)\n"
             "grey -53%    blue -49%    green -27%    red -45%",
             transform=ax2.transAxes, fontsize=9, va="bottom", linespacing=1.5,
             bbox=dict(boxstyle="round,pad=0.45", fc="white", ec="0.7", alpha=0.92))

    fig.tight_layout()
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "comparison_final_config.png"
    fig.savefig(out, dpi=140)
    print(f"\nSaved: {out}")

    (RESULTS / "comparison_final_config_curves.json").write_text(json.dumps({
        "configurations": [{"label": l, "return": s["res"]["total_return"],
                            "max_drawdown": s["res"]["max_drawdown"],
                            "lowest_equity": s["res"]["lowest_equity"],
                            "funding_paid": s["res"]["funding_paid"]} for l, s, _ in series],
        "vol_screen": {"criterion": "ex-ante volatility only",
                       "market_cap_filter": False, "liquidity_filter": False,
                       "pool": len(eligible), "slots": SLOTS,
                       "median_mcap_of_selected": float(np.median(
                           [mcap_map[s]["mcap"] for s in vol196 if mcap_map[s]]))},
        "mcap_screen": {"criterion": "smallest market cap",
                        "median_mcap_of_selected": float(np.median(
                            [mcap_map[s]["mcap"] for s in mcap196]))},
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
