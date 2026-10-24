"""
Volatility screen — parameter quantification and sensitivity.

The previous round used two different, never-tuned parameterisations:
  * static screen : std of hourly log returns over 720h (30d), measured once
  * dynamic rank  : std of hourly log returns over 168h (7d), rolling

This script pins down what the metric actually is, what values it takes in this
universe, and how sensitive the result is to each choice:

  part 1  magnitudes: what does the metric equal, in annualised terms?
  part 2  lookback window: 24h / 72h / 168h(7d) / 336h(14d) / 720h(30d)
  part 3  metric definition: std / MAD / high-low range / downside deviation
  part 4  monotonicity: volatility quartile bands at fixed universe size
  part 5  absolute threshold: cumulative percentile cut-offs (production usable)

Every screen is measured strictly out-of-sample: the metric uses only data
before `start_bar`, and trading begins at `start_bar`.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

import tier_engine as T

DATA = Path("data")
RESULTS = DATA / "results"
FM = np.load(DATA / "funding_matrix.npy")
N_SELECT = 100
HOURS_PER_YEAR = 24 * 365


def load():
    bars = np.load(DATA / "bars_cache.npy")
    symbols = json.loads((DATA / "bars_info.json").read_text())["symbols"]
    cols = {}
    for i, s in enumerate(symbols):
        cols.setdefault(s, i)          # dedupe the duplicated ACTUSDT column
    return bars, cols


def metric(bars, cols, end, window, kind="std"):
    """Ex-ante volatility metric from [end-window, end). No look-ahead."""
    lo = max(0, end - window)
    out = {}
    for s, i in cols.items():
        c = bars[lo:end, i, 3]
        h = bars[lo:end, i, 1]
        l = bars[lo:end, i, 2]
        m = np.isfinite(c)
        if m.sum() < max(12, window * 0.8):
            continue
        if kind == "range":
            mm = m & np.isfinite(h) & np.isfinite(l)
            if mm.sum() < 12:
                continue
            out[s] = float(np.mean((h[mm] - l[mm]) / c[mm]))
            continue
        r = np.diff(np.log(c[m]))
        if len(r) < 12:
            continue
        if kind == "std":
            v = float(np.std(r))
        elif kind == "mad":
            v = float(np.mean(np.abs(r)))
        elif kind == "downside":
            neg = r[r < 0]
            v = float(np.sqrt(np.mean(neg ** 2))) if len(neg) > 1 else np.nan
        else:
            raise ValueError(kind)
        if np.isfinite(v):
            out[s] = v
    return pd.Series(out)


def run(idx, W, random_seeds=0):
    if random_seeds:
        import random
        effs = []
        for sd in range(random_seeds):
            pick = random.Random(W + sd).sample(list(idx), N_SELECT)
            r = T.run_strategy(T.StrategyConfig(universe_symbols=tuple(pick),
                                                start_bar=W, funding_matrix=FM))
            effs.append(r["total_return"] / abs(r["max_drawdown"]))
        return float(np.mean(effs)), None
    r = T.run_strategy(T.StrategyConfig(universe_symbols=tuple(idx),
                                        start_bar=W, funding_matrix=FM))
    return (r["total_return"] / abs(r["max_drawdown"]),
            dict(ret=r["total_return"], mdd=r["max_drawdown"],
                 low=r["lowest_equity"], opens=r["tier1_opens"]))


def part1_magnitudes(bars, cols):
    print("\n" + "=" * 96)
    print(" PART 1 — what the metric actually equals (first 30 days, 196 symbols)")
    print("=" * 96)
    v = metric(bars, cols, 720, 720, "std")
    ann = v * np.sqrt(HOURS_PER_YEAR) * 100
    print(f"  metric = std of hourly log returns over 720h  (n={len(v)} symbols)")
    print(f"  {'percentile':<14}{'hourly sigma':>16}{'annualised sigma':>20}")
    for p in [5, 25, 50, 75, 90, 95, 100]:
        q = np.percentile(v, p)
        print(f"  p{p:<13}{q:>15.5f}{q * np.sqrt(HOURS_PER_YEAR) * 100:>19.1f}%")
    print(f"\n  -> \"high volatility\" in this universe means roughly "
          f"{np.percentile(ann, 75):.0f}%-{np.percentile(ann, 100):.0f}% annualised")
    print(f"  -> the 100 selected by highest vol have mean annualised sigma "
          f"{ann.nlargest(100).mean():.0f}% vs {ann.nsmallest(100).mean():.0f}% for the lowest 100")
    return v, ann


def part2_window(bars, cols, windows=(720, 2160, 3600)):
    print("\n" + "=" * 96)
    print(" PART 2 — lookback window sensitivity (top-100 by std of hourly log returns)")
    print("=" * 96)
    lags = [24, 72, 168, 336, 720]
    print(f"  {'trade from':<12}{'random':>9}" + "".join(f"{str(w) + 'h':>10}" for w in lags))
    out = {}
    for W in windows:
        rnd, _ = run(cols, W, random_seeds=4)
        row = []
        for w in lags:
            v = metric(bars, cols, W, w, "std")
            eff, _ = run(list(v.nlargest(N_SELECT).index), W)
            row.append(eff)
        out[W] = dict(random=rnd, **{f"{w}h": e for w, e in zip(lags, row)})
        print(f"  {W:<12}{rnd:>9.2f}" + "".join(f"{e:>10.2f}" for e in row))
    print("\n  (all windows are comparable; the metric window is the only thing changing)")


def part3_definition(bars, cols, windows=(720, 2160, 3600)):
    print("\n" + "=" * 96)
    print(" PART 3 — metric definition sensitivity (window = 720h, top-100)")
    print("=" * 96)
    kinds = [("std", "std of hourly log ret"),
             ("mad", "mean |hourly log ret|"),
             ("range", "mean (high-low)/close"),
             ("downside", "downside deviation")]
    print(f"  {'trade from':<12}" + "".join(f"{k:>22}" for k, _ in kinds))
    for W in windows:
        row = []
        for k, _ in kinds:
            v = metric(bars, cols, W, 720, k)
            eff, _ = run(list(v.nlargest(N_SELECT).index), W)
            row.append(eff)
        print(f"  {W:<12}" + "".join(f"{e:>22.2f}" for e in row))
    print("\n  definitions: " + " | ".join(f"{k}={d}" for k, d in kinds))


def part4_bands(bars, cols, windows=(720, 2160, 3600)):
    print("\n" + "=" * 96)
    print(" PART 4 — monotonicity: volatility quartile bands at fixed size (49 symbols)")
    print("=" * 96)
    print(f"  {'trade from':<12}{'Q1 lowest':>12}{'Q2':>10}{'Q3':>10}{'Q4 highest':>12}{'random49':>10}")
    for W in windows:
        v = metric(bars, cols, W, 720, "std").sort_values()
        k = len(v) // 4
        bands = [v.iloc[i * k:(i + 1) * k].index for i in range(4)]
        effs = []
        for b in bands:
            r = T.run_strategy(T.StrategyConfig(universe_symbols=tuple(b), start_bar=W,
                                                funding_matrix=FM))
            effs.append(r["total_return"] / abs(r["max_drawdown"]))
        rnd_unused = None
        import random
        rr = []
        for sd in range(4):
            pick = random.Random(W + sd).sample(list(v.index), k)
            r = T.run_strategy(T.StrategyConfig(universe_symbols=tuple(pick), start_bar=W,
                                                funding_matrix=FM))
            rr.append(r["total_return"] / abs(r["max_drawdown"]))
        print(f"  {W:<12}" + "".join(f"{e:>12.2f}" for e in effs) + f"{np.mean(rr):>10.2f}")


def part5_threshold(bars, cols, windows=(720, 2160, 3600)):
    print("\n" + "=" * 96)
    print(" PART 5 — absolute threshold (include symbol if ann. vol >= p-th percentile)")
    print("=" * 96)
    print(f"  {'trade from':<12}{'p0 (all 196)':>14}{'p25':>10}{'p50':>10}{'p75':>10}{'p90':>10}")
    for W in windows:
        v = metric(bars, cols, W, 720, "std")
        row, counts = [], []
        for p in [0, 25, 50, 75, 90]:
            cut = np.percentile(v, p)
            idx = list(v[v >= cut].index)
            eff, _ = run(idx, W)
            row.append(eff)
            counts.append(len(idx))
        print(f"  {W:<12}" + "".join(f"{e:>10.2f}" for e in row))
        print(f"  {'  n symbols':<12}" + "".join(f"{c:>10d}" for c in counts))


def main():
    base = T.run_strategy(T.StrategyConfig())
    assert abs(base["final_equity"] - 3867.8487458025065) < 1e-6
    print(f"[engine check] baseline reproduces strategy_final.py: ${base['final_equity']:,.2f}")
    print("all screens measured out-of-sample; real funding on; metric = ret/|maxDD|")

    bars, cols = load()
    v, ann = part1_magnitudes(bars, cols)
    part2_window(bars, cols)
    part3_definition(bars, cols)
    part4_bands(bars, cols)
    part5_threshold(bars, cols)

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "vol_screen_params.json").write_text(json.dumps({
        "metric": "std of hourly log returns",
        "annualisation": "sigma_hourly * sqrt(24*365)",
        "cross_section_annualised_pct": {f"p{p}": float(np.percentile(ann, p))
                                         for p in [5, 25, 50, 75, 90, 95, 100]},
        "selected_top100_mean_ann_pct": float(ann.nlargest(100).mean()),
        "rejected_bottom100_mean_ann_pct": float(ann.nsmallest(100).mean()),
    }, indent=2), encoding="utf-8")
    print(f"\nSaved: {RESULTS / 'vol_screen_params.json'}")


if __name__ == "__main__":
    main()
