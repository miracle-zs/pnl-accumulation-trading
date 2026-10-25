"""
Plot the best validated configuration against the base design.

Base : all 196 symbols, daily 08:00 slot refill, REAL funding   (current design)
Best : all 196 symbols, hourly slot refill,     REAL funding   (the one change
       that survived every robustness test: 6/6 rotated time axes, and best in
       all six funding regimes)

The volatility screen is deliberately NOT stacked on top of "best": it only
decides WHICH symbols occupy a *limited* number of slots. With no slot cap the
engine already holds the whole pool, so a screen can only remove positions.

Output: data/results/comparison_best_vs_base.png
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

import tier_engine as T

DATA = Path("data")
RESULTS = DATA / "results"
BASELINE_EQUITY = 3867.8487458025065


def run(**kw):
    r = T.run_strategy(T.StrategyConfig(**kw))
    h = np.asarray(r["history"], dtype=float)
    peak = np.maximum.accumulate(h)
    return dict(res=r, equity=h, dd=(h - peak) / peak * 100.0)


def main():
    frozen = run()                                                        # README figure
    assert abs(frozen["res"]["final_equity"] - BASELINE_EQUITY) < 1e-6

    fm = np.load(DATA / "funding_matrix.npy")
    base = run(funding_matrix=fm)
    best = run(funding_matrix=fm, rebalance_hours=tuple(range(24)))

    series = [("Frozen baseline (daily refill, NO funding)", frozen, "#9aa0a6", ":"),
              ("Base (daily refill, real funding)", base, "#1f77b4", "-"),
              ("Best (hourly refill, real funding)", best, "#2ca02c", "-")]

    print(f"{'config':<42}{'final eq':>12}{'return':>10}{'maxDD':>9}{'lowest':>11}{'ret/|DD|':>10}")
    for label, s, _, _ in series:
        r = s["res"]
        print(f"{label:<42}{r['final_equity']:>12,.2f}{r['total_return']:>9.2f}%"
              f"{r['max_drawdown']:>8.2f}%{r['lowest_equity']:>11,.2f}"
              f"{r['total_return'] / abs(r['max_drawdown']):>10.2f}")

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13.5, 9), sharex=True,
                                   gridspec_kw={"height_ratios": [2.4, 1]})
    for label, s, color, style in series:
        idx = pd.date_range("2026-01-01", periods=len(s["equity"]), freq="h")
        ax1.plot(idx, s["equity"], color=color, linestyle=style, linewidth=1.9, label=label)
        ax2.plot(idx, s["dd"], color=color, linestyle=style, linewidth=1.4)

    ax1.axhline(1000.0, color="black", linewidth=0.8, alpha=0.4)
    last_date = pd.date_range("2026-01-01", periods=len(base["equity"]), freq="h")[-1]
    for ax in (ax1, ax2):
        ax.set_xlim(pd.Timestamp("2025-12-24"), last_date + pd.Timedelta(days=22))
    ax2.set_ylim(-62, 4)
    ax1.set_ylabel("Equity (USDT)")
    ax1.set_title("Best validated change vs base — Binance USDT-M, 196 symbols, 2026-01 -> 2026-09\n"
                  "real historical funding rates (data.binance.vision)", fontsize=12)
    ax1.grid(alpha=0.3)
    ax1.legend(loc="upper left", fontsize=9, framealpha=0.95)

    # annotate the February crash that sets every max-drawdown figure in this repo
    crash = pd.Timestamp("2026-02-05 23:00")
    ax1.axvline(crash, color="crimson", linewidth=1.0, alpha=0.5)
    ax1.annotate("2026-02-05 crash\nsets the max drawdown",
                 xy=(crash, base["equity"][840]), xytext=(pd.Timestamp("2026-03-05"), 1500),
                 fontsize=8.5, color="crimson",
                 arrowprops=dict(arrowstyle="->", color="crimson", lw=1.0))

    tb = base["res"]
    tt = best["res"]
    ax1.annotate(f"base  ${tb['final_equity']:,.0f}  ({tb['total_return']:+.1f}%)",
                 xy=(pd.Timestamp("2026-09-25"), tb["final_equity"]), xytext=(6, 6),
                 textcoords="offset points", fontsize=9, color="#1f77b4", fontweight="bold")
    ax1.annotate(f"best  ${tt['final_equity']:,.0f}  ({tt['total_return']:+.1f}%)",
                 xy=(pd.Timestamp("2026-09-25"), tt["final_equity"]), xytext=(6, 6),
                 textcoords="offset points", fontsize=9, color="#2ca02c", fontweight="bold")

    ax2.axhline(0, color="black", linewidth=0.8, alpha=0.4)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_xlabel("Date")
    ax2.grid(alpha=0.3)
    ax2.annotate(f"max DD {tb['max_drawdown']:.1f}%", xy=(crash, tb["max_drawdown"]),
                 xytext=(10, -3), textcoords="offset points", fontsize=9,
                 color="#1f77b4", fontweight="bold")
    ax2.annotate(f"max DD {tt['max_drawdown']:.1f}%", xy=(crash, tt["max_drawdown"]),
                 xytext=(10, 6), textcoords="offset points", fontsize=9,
                 color="#2ca02c", fontweight="bold")

    fig.tight_layout()
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "comparison_best_vs_base.png"
    fig.savefig(out, dpi=140)
    print(f"\nSaved: {out}")

    (RESULTS / "comparison_best_vs_base.json").write_text(json.dumps({
        label: {"final_equity": s["res"]["final_equity"], "total_return": s["res"]["total_return"],
                "max_drawdown": s["res"]["max_drawdown"], "lowest_equity": s["res"]["lowest_equity"],
                "funding_paid": s["res"]["funding_paid"],
                "eff": s["res"]["total_return"] / abs(s["res"]["max_drawdown"])}
        for label, s, _, _ in series}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
