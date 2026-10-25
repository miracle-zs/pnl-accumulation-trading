"""
Compounding (equity-proportional position sizing) experiments.

Runs on the shared parameterized engine (tier_engine.py), which reproduces the
frozen baseline in strategy_final.py exactly.

Question under test: can compounding raise returns without breaking the
Tier-3 cap / devaluation-recycling risk architecture?

The control group matters: `compound_min == compound_cap` forces a CONSTANT
scale factor, which yields "just trade bigger" with an identical grid geometry.
That separates the effect of compounding (a time-varying scale) from the effect
of simply sizing up.

Verdict (see data/results/comparison_compounding.json): compounding beats fixed
sizing only on the original time axis, where the -54% crash landed on day 35
while the compounding scale was still 1.0x. Rotating the time axis so the same
crash lands later flips the ranking in 6/6 cases -- the advantage is a bet on
crash-before-growth sequencing, not a robust edge.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

import tier_engine as T

RESULTS = Path("data/results")
BASELINE_EQUITY = 3867.8487458025065

MODES = [
    ("static 1x (frozen base)", dict(compound=False)),
    ("static 1.5x fixed",       dict(compound=True, compound_cap=1.5, compound_min=1.5)),
    ("static 2x fixed",         dict(compound=True, compound_cap=2.0, compound_min=2.0)),
    ("static 3x fixed",         dict(compound=True, compound_cap=3.0, compound_min=3.0)),
    ("compound -> 2x",          dict(compound=True, compound_cap=2.0)),
    ("compound -> 3x",          dict(compound=True, compound_cap=3.0)),
    ("compound uncapped",       dict(compound=True, compound_cap=float("inf"))),
]

FUNDING = [("0.00%/8h (frozen assumption)", 0.0),
           ("0.03%/8h", 0.0003 / 8),
           ("0.05%/8h", 0.0005 / 8)]


def main():
    base = T.run_strategy(T.StrategyConfig())
    assert abs(base["final_equity"] - BASELINE_EQUITY) < 1e-6, base["final_equity"]
    print(f"[engine check] baseline reproduces strategy_final.py: "
          f"${base['final_equity']:,.2f} ({base['total_return']:+.2f}%), maxDD {base['max_drawdown']:.2f}%")

    rows, curves = [], {}
    hdr = (f"{'config':<24}{'final eq':>12}{'return':>9}{'maxDD':>9}{'ret/|DD|':>10}"
           f"{'lowest':>11}{'liq':>5}{'DCArej':>8}{'peakNotl':>10}{'scale':>8}{'MDD date':>18}")
    for fname, fr in FUNDING:
        print(f"\n{'=' * len(hdr)}\n funding {fname}\n{'=' * len(hdr)}")
        print(hdr)
        for label, kw in MODES:
            r = T.run_strategy(T.StrategyConfig(funding_hourly=fr, **kw))
            eff = r["total_return"] / abs(r["max_drawdown"]) if r["max_drawdown"] < 0 else 0.0
            mdd_date = pd.Timestamp("2026-01-01") + pd.Timedelta(hours=r["mdd_bar"])
            rows.append(dict(mode=label, funding=fname, equity=r["final_equity"], ret=r["total_return"],
                             mdd=r["max_drawdown"], eff=eff, low=r["lowest_equity"], liq=r["is_liquidated"],
                             dca_rejected=r["dca_rejected"], peak_notional=r["peak_notional"],
                             max_scale=r["max_scale_seen"], recycled=len(r["recycled_events"]),
                             open_at_end=r["open_at_end"], mdd_bar=r["mdd_bar"]))
            print(f"{label:<24}{r['final_equity']:>12,.2f}{r['total_return']:>8.2f}%{r['max_drawdown']:>8.2f}%"
                  f"{eff:>10.2f}{r['lowest_equity']:>11,.2f}{'YES' if r['is_liquidated'] else 'no':>5}"
                  f"{r['dca_rejected']:>8d}{r['peak_notional']:>10,.0f}{r['max_scale_seen']:>7.2f}x"
                  f"{str(mdd_date)[:16]:>18}")
            if fr == 0.0:
                curves[label] = r["history"]

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "comparison_compounding.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    fig, ax = plt.subplots(figsize=(13, 7))
    for label, h in curves.items():
        ax.plot(pd.date_range("2026-01-01", periods=len(h), freq="h"), h, linewidth=1.4, label=label)
    ax.axhline(1000.0, color="gray", linestyle=":", linewidth=1)
    ax.set_title("Compounding vs fixed notional — equity, funding excluded (2026-01 -> 2026-09)")
    ax.set_ylabel("Equity (USDT)")
    ax.set_xlabel("Date")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(RESULTS / "comparison_compounding.png", dpi=130)
    print(f"\nSaved: {RESULTS / 'comparison_compounding.json'}")
    print(f"Saved: {RESULTS / 'comparison_compounding.png'}")


if __name__ == "__main__":
    main()
