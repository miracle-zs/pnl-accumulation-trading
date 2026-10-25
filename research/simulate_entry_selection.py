"""
Universe selection and entry-cadence experiments.

Runs on the shared parameterized engine (tier_engine.py), which reproduces the
frozen baseline in strategy_final.py exactly.

Questions under test:
  1. Does the current universe screen (lowest market cap) actually add edge?
  2. Is there a better ex-ante screen?
  3. Which entry cadence / timing rule is best, and is it path-robust?

Method notes:
  - Selection is always made on a 30-day window that ends where trading starts
    (`start_bar`), so the screen never sees the period it is traded on.
  - Every "winner" is re-tested across rotated time axes (`bar_shift`), because
    this strategy's headline metrics are dominated by a single crash event.
"""

import json
import random
from pathlib import Path

import numpy as np
import pandas as pd

import tier_engine as T

DATA = Path("data")
RESULTS = DATA / "results"
N_SELECT = 100
SELECT_WINDOW = 720          # 30 days
RANDOM_SEEDS = 4

BASELINE_EQUITY = 3867.8487458025065


def load_bars():
    bars = np.load(DATA / "bars_cache.npy")
    info = json.loads((DATA / "bars_info.json").read_text())
    return bars, info["symbols"]


def features(closes, end_bar, window=SELECT_WINDOW):
    """Ex-ante realised volatility per symbol, using ONLY data before end_bar."""
    out = {}
    for s, c in closes.items():
        v = c[end_bar - window:end_bar]
        v = v[~np.isnan(v)]
        if len(v) < window * 0.9:
            continue
        out[s] = float(np.std(np.diff(np.log(v))))
    return pd.Series(out)


def run(idx=None, **kw):
    if idx is not None:
        kw["universe_symbols"] = tuple(idx)
    r = T.run_strategy(T.StrategyConfig(**kw))
    return dict(ret=r["total_return"], mdd=r["max_drawdown"],
                eff=r["total_return"] / abs(r["max_drawdown"]),
                low=r["lowest_equity"], opens=r["tier1_opens"], liq=r["is_liquidated"], raw=r)


def part1_attribution(bars, symbols):
    """What per-symbol property predicts profitability?"""
    print("\n" + "=" * 96)
    print(" PART 1 — per-symbol attribution (full period, diagnostic only: uses look-ahead)")
    print("=" * 96)
    r = T.run_strategy(T.StrategyConfig())
    uni = {u["symbol"]: u for u in json.loads((DATA / "universe_200_20260101.json").read_text())}
    rows = []
    for i, s in enumerate(r["symbols"]):
        c = bars[:, i, 3]
        v = c[~np.isnan(c)]
        if len(v) < 100 or r["sym_opens"][i] == 0:
            continue
        try:
            df = pd.read_pickle(DATA / "klines" / f"{s}_1h_2026.pkl")
            qv = float((df["volume"] * df["close"]).mean())
        except Exception:
            qv = np.nan
        rows.append(dict(symbol=s, opens=int(r["sym_opens"][i]), realized=r["sym_realized"][i],
                         pnl_per_open=r["sym_realized"][i] / max(1, r["sym_opens"][i]),
                         mcap=uni.get(s, {}).get("mcap", np.nan),
                         vol=float(np.std(np.diff(np.log(v)))), qvol=qv))
    d = pd.DataFrame(rows)
    print(f"  symbols: {len(d)}   total realised: {d.realized.sum():,.0f}U   "
          f"symbols with NEGATIVE realised PnL: {(d.realized < 0).sum()}")
    print(f"  top-20 symbols = {d.nlargest(20, 'realized').realized.sum() / d.realized.sum() * 100:.0f}% of all realised PnL")
    print(f"\n  {'feature':<14}{'rho vs PnL/open':>18}{'rho vs raw PnL':>18}")
    for f in ["mcap", "vol", "qvol", "opens"]:
        print(f"  {f:<14}{d['pnl_per_open'].corr(d[f], method='spearman'):>18.3f}"
              f"{d.realized.corr(d[f], method='spearman'):>18.3f}")
    d.to_csv(RESULTS / "symbol_attribution.csv", index=False)
    return d


def part2_selection(closes, symbols, windows=(720, 1440, 2160, 2880, 3600)):
    """mcap screen vs volatility screen vs random, all out-of-sample."""
    print("\n" + "=" * 96)
    print(" PART 2 — universe selection, screened out-of-sample (30d window, fixed N=100)")
    print("=" * 96)
    uni = {u["symbol"]: u for u in json.loads((DATA / "universe_200_20260101.json").read_text())}
    agg = {"mcap": [], "vol": [], "random": []}
    for W in windows:
        F = features(closes, W)
        mcap_idx = sorted(F.index, key=lambda s: uni.get(s, {}).get("mcap", np.inf))[:N_SELECT]
        vol_idx = list(F.nlargest(N_SELECT).index)
        rnd = [run(random.Random(sd).sample(list(F.index), N_SELECT), start_bar=W)
               for sd in range(RANDOM_SEEDS)]
        m, v = run(mcap_idx, start_bar=W), run(vol_idx, start_bar=W)
        re_ = float(np.mean([x["eff"] for x in rnd]))
        agg["mcap"].append(m["eff"]); agg["vol"].append(v["eff"]); agg["random"].append(re_)
        print(f"  trade from bar {W:>5}: lowest-mcap {m['ret']:>7.1f}% eff {m['eff']:>5.2f} | "
              f"highest-vol {v['ret']:>7.1f}% eff {v['eff']:>5.2f} | random eff {re_:>5.2f}")
    print(f"\n  mean eff over {len(windows)} windows:  lowest-mcap {np.mean(agg['mcap']):.2f}   "
          f"highest-vol {np.mean(agg['vol']):.2f}   random {np.mean(agg['random']):.2f}")
    print(f"  highest-vol beats lowest-mcap in {sum(a > b for a, b in zip(agg['vol'], agg['mcap']))}/{len(windows)} windows")
    return agg


def part3_cadence():
    print("\n" + "=" * 96)
    print(" PART 3 — entry cadence / timing (single sample)")
    print("=" * 96)
    trials = [
        ("daily refill (current)", {}),
        ("every 2 days", dict(rebalance_every_n_days=2)),
        ("every 7 days", dict(rebalance_every_n_days=7)),
        ("refill 3x/day", dict(rebalance_hours=(0, 8, 16))),
        ("refill hourly", dict(rebalance_hours=tuple(range(24)))),
        ("phase-in 10 days", dict(phase_in_days=10)),
        ("throttle 1 new/day", dict(max_new_per_rebalance=1)),
        ("throttle 3 new/day", dict(max_new_per_rebalance=3)),
        ("throttle 10 new/day", dict(max_new_per_rebalance=10)),
        ("dip-buy >=10% / 24h", dict(entry_drop_lookback_h=24, entry_drop_min=0.10)),
        ("dip-buy >=20% / 72h", dict(entry_drop_lookback_h=72, entry_drop_min=0.20)),
        ("dip-buy >=30% / 168h", dict(entry_drop_lookback_h=168, entry_drop_min=0.30)),
    ]
    print(f"  {'variant':<26}{'return':>9}{'maxDD':>9}{'ret/|DD|':>10}{'opens':>8}")
    out = []
    for label, kw in trials:
        r = run(**kw)
        out.append((label, r))
        print(f"  {label:<26}{r['ret']:>8.1f}%{r['mdd']:>8.1f}%{r['eff']:>10.2f}{r['opens']:>8d}")
    return out


def part4_robustness(shifts=(0, 1092, 2184, 3276, 4368, 5460)):
    """Re-test the cadence winners across rotated time axes."""
    print("\n" + "=" * 96)
    print(" PART 4 — path robustness of the cadence winners (rotated time axis)")
    print("=" * 96)
    variants = {
        "baseline": {},
        "hourly refill": dict(rebalance_hours=tuple(range(24))),
        "throttle 10/day": dict(max_new_per_rebalance=10),
        "hourly + throttle 10": dict(rebalance_hours=tuple(range(24)), max_new_per_rebalance=10),
    }
    res = {k: [] for k in variants}
    for sh in shifts:
        for k, kw in variants.items():
            res[k].append(run(bar_shift=sh, **kw))
    base = np.array([x["eff"] for x in res["baseline"]])
    print(f"  {'variant':<24}{'mean ret':>10}{'mean DD':>9}{'mean eff':>10}{'eff wins vs base':>18}")
    for k, v in res.items():
        a = np.array([[x["ret"], x["mdd"], x["eff"]] for x in v])
        wins = int((a[:, 2] > base).sum())
        print(f"  {k:<24}{a[:, 0].mean():>9.1f}%{a[:, 1].mean():>8.1f}%{a[:, 2].mean():>10.2f}{f'{wins}/{len(shifts)}':>18}")
    return res


def main():
    base = T.run_strategy(T.StrategyConfig())
    assert abs(base["final_equity"] - BASELINE_EQUITY) < 1e-6, base["final_equity"]
    print(f"[engine check] baseline reproduces strategy_final.py: "
          f"${base['final_equity']:,.2f} ({base['total_return']:+.2f}%), maxDD {base['max_drawdown']:.2f}%")

    bars, symbols = load_bars()
    closes = {}
    for i, s in enumerate(symbols):
        closes.setdefault(s, bars[:, i, 3])   # dedupe the duplicated ACTUSDT column

    part1_attribution(bars, symbols)
    agg = part2_selection(closes, symbols)
    cadence = part3_cadence()
    robust = part4_robustness()

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "comparison_entry_selection.json").write_text(json.dumps({
        "selection_mean_eff": {k: float(np.mean(v)) for k, v in agg.items()},
        "cadence": [{"variant": l, "return": r["ret"], "maxdd": r["mdd"], "eff": r["eff"],
                     "opens": r["opens"]} for l, r in cadence],
        "robustness": {k: [{"ret": x["ret"], "mdd": x["mdd"], "eff": x["eff"]} for x in v]
                       for k, v in robust.items()},
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
