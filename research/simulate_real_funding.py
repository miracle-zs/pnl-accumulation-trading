"""
Re-run the surviving strategy improvements under REAL historical funding rates.

Uses data/funding_matrix.npy (fetched by fetch_funding.py from
data.binance.vision monthly fundingRate archives) and the shared engine
tier_engine.py.

The control from the previous round was a constant funding assumption, which is
directionally wrong for this universe: these beaten-down micro-caps traded at a
perpetual DISCount through 2026-01..09, so a long position collected funding
rather than paying it (mean -0.0051%/8h, -5.6% annualised).

Improvements re-tested here:
  * hourly slot refill instead of daily 08:00
  * volatility screen instead of market-cap screen
and both together, on the original axis and across rotated time axes.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

import tier_engine as T
import simulate_entry_selection as ES

DATA = Path("data")
RESULTS = DATA / "results"
BASELINE_EQUITY = 3867.8487458025065
FM = np.load(DATA / "funding_matrix.npy")
HOURLY = dict(rebalance_hours=tuple(range(24)))
N_SELECT = 100
SELECT_WINDOW = 720


def run(idx=None, real_funding=True, **kw):
    if idx is not None:
        kw["universe_symbols"] = tuple(idx)
    if real_funding:
        kw["funding_matrix"] = FM
    r = T.run_strategy(T.StrategyConfig(**kw))
    return dict(ret=r["total_return"], mdd=r["max_drawdown"],
                eff=r["total_return"] / abs(r["max_drawdown"]), low=r["lowest_equity"],
                funding=r["funding_paid"], opens=r["tier1_opens"], liq=r["is_liquidated"])


def part_a_funding_impact():
    print("\n" + "=" * 92)
    print(" A — what real funding does to the frozen baseline")
    print("=" * 92)
    print(f"  {'scenario':<34}{'return':>10}{'maxDD':>9}{'lowest':>10}{'net funding':>14}")
    for label, kw in [("no funding (frozen assumption)", dict(real_funding=False)),
                      ("REAL funding", dict(real_funding=True)),
                      ("constant +0.01%/8h (what I tested before)", dict(real_funding=False,
                                                                         funding_hourly=0.0001 / 8))]:
        r = run(**kw)
        print(f"  {label:<34}{r['ret']:>9.1f}%{r['mdd']:>8.1f}%{r['low']:>10.1f}{r['funding']:>+13.2f}U")


def part_b_selection_under_funding():
    print("\n" + "=" * 92)
    print(" B — selection under real funding (screen 30d out-of-sample, fixed N=100)")
    print("=" * 92)
    bars, symbols = ES.load_bars()
    closes = {}
    for i, s in enumerate(symbols):
        closes.setdefault(s, bars[:, i, 3])
    uni = {u["symbol"]: u for u in json.loads((DATA / "universe_200_20260101.json").read_text())}
    W = 1440                                    # trade the last ~7 months
    F = ES.features(closes, W)
    mcap_idx = sorted(F.index, key=lambda s: uni.get(s, {}).get("mcap", np.inf))[:N_SELECT]
    vol_idx = list(F.nlargest(N_SELECT).index)
    print(f"  trading from bar {W}; universe pool {len(F)} symbols")
    print(f"  {'rule':<28}{'return':>10}{'maxDD':>9}{'ret/|DD|':>10}{'net funding':>14}")
    for label, idx in [("lowest mcap (current)", mcap_idx), ("highest ex-ante vol", vol_idx)]:
        r = run(idx, start_bar=W)
        print(f"  {label:<28}{r['ret']:>9.1f}%{r['mdd']:>8.1f}%{r['eff']:>10.2f}{r['funding']:>+13.2f}U")


def part_c_combination():
    print("\n" + "=" * 92)
    print(" C — combined improvements, full period, real funding")
    print("=" * 92)
    print(f"  {'configuration':<36}{'return':>10}{'maxDD':>9}{'ret/|DD|':>10}{'net funding':>14}")
    combos = [
        ("daily refill (current)", {}),
        ("hourly refill", HOURLY),
        ("daily + no funding (ref)", dict(real_funding=False)),
        ("hourly + no funding (ref)", dict(real_funding=False, **HOURLY)),
    ]
    for label, kw in combos:
        r = run(**kw)
        print(f"  {label:<36}{r['ret']:>9.1f}%{r['mdd']:>8.1f}%{r['eff']:>10.2f}{r['funding']:>+13.2f}U")


def part_d_robustness(shifts=(0, 1092, 2184, 3276, 4368, 5460)):
    print("\n" + "=" * 92)
    print(" D — path robustness under real funding (rotated time axis)")
    print("=" * 92)
    variants = {"baseline": {}, "hourly refill": HOURLY}
    res = {k: [] for k in variants}
    for sh in shifts:
        for k, kw in variants.items():
            res[k].append(run(bar_shift=sh, **kw))
    base = np.array([x["eff"] for x in res["baseline"]])
    print(f"  {'variant':<26}{'mean ret':>10}{'mean DD':>9}{'mean eff':>10}{'net funding':>14}{'eff wins':>10}")
    for k, v in res.items():
        a = np.array([[x["ret"], x["mdd"], x["eff"]] for x in v])
        f = np.mean([x["funding"] for x in v])
        wins = int((a[:, 2] > base).sum())
        print(f"  {k:<26}{a[:, 0].mean():>9.1f}%{a[:, 1].mean():>8.1f}%{a[:, 2].mean():>10.2f}"
              f"{f:>+13.2f}U{f'{wins}/{len(shifts)}':>10}")
    return res


def main():
    base = T.run_strategy(T.StrategyConfig())
    assert abs(base["final_equity"] - BASELINE_EQUITY) < 1e-6, base["final_equity"]
    print(f"[engine check] baseline still reproduces strategy_final.py: "
          f"${base['final_equity']:,.2f} ({base['total_return']:+.2f}%)")

    part_a_funding_impact()
    part_b_selection_under_funding()
    part_c_combination()
    robust = part_d_robustness()

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "comparison_real_funding.json").write_text(json.dumps(
        {k: [{"ret": x["ret"], "mdd": x["mdd"], "eff": x["eff"], "funding": x["funding"]} for x in v]
         for k, v in robust.items()}, indent=2), encoding="utf-8")
    print(f"\nSaved: {RESULTS / 'comparison_real_funding.json'}")


if __name__ == "__main__":
    main()
