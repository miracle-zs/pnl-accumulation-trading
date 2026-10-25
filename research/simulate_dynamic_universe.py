"""
Dynamic daily universe re-selection — the last untested assumption.

The frozen backtest pins the universe on 2026-01-01 (the 200 smallest market
caps that day) and trades that fixed list for nine months. SPEC/README/ADR 0010
instead promise a DAILY 08:00 re-ranking that refills idle slots with the
current best candidates.

Market cap has no historical time series here (only a 2026-01-01 snapshot), so
daily mcap re-ranking is not backtestable. ADR 0010's documented production
fallback — rank by 24h quote volume — IS computable from the klines, so that is
what is tested, alongside realised volatility.

Semantics match the intended production design: re-ranking only decides which
IDLE slots get filled. Open positions are never force-closed (ADR 0004
hold-through), they still exit only via take-profit.

Fair comparison: every arm holds at most 100 concurrent positions from the same
197-symbol pool. The only difference is how the 100 slots are allocated.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

import tier_engine as T

DATA = Path("data")
RESULTS = DATA / "results"
BASELINE_EQUITY = 3867.8487458025065
SLOTS = 100
VOL_WINDOW = 168        # 7 days
LIQ_WINDOW = 24         # 24h


def load():
    bars = np.load(DATA / "bars_cache.npy")
    qv = np.load(DATA / "volume_cache.npy").astype(np.float64)
    fund = np.load(DATA / "funding_matrix.npy")
    info = json.loads((DATA / "bars_info.json").read_text())
    return bars, qv, fund, info["symbols"]


def _prefix(a):
    return np.vstack([np.zeros((1, a.shape[1])), np.nan_to_num(a, nan=0.0).cumsum(axis=0)])


def trailing_volume(qv, window=LIQ_WINDOW):
    """Trailing quote volume over [b-window, b) — known before bar b acts."""
    T_, n = qv.shape
    cs = _prefix(qv)
    idx = np.arange(T_)
    lo = np.maximum(idx - window, 0)
    return cs[idx] - cs[lo]


def trailing_vol(close, window=VOL_WINDOW):
    """Trailing realised vol of hourly log returns over the same window."""
    with np.errstate(all="ignore"):
        lr = np.diff(np.log(close), axis=0)
    good = np.isfinite(lr)
    lr0 = np.where(good, lr, 0.0)
    cs1 = _prefix(lr0)
    cs2 = _prefix(lr0 ** 2)
    csc = _prefix(good.astype(float))
    T_ = close.shape[0]
    idx = np.arange(T_)
    lo = np.maximum(idx - window, 0)
    n = np.maximum(csc[idx] - csc[lo], 0.0)
    s1 = cs1[idx] - cs1[lo]
    s2 = cs2[idx] - cs2[lo]
    with np.errstate(all="ignore"):
        mean = np.where(n > 0, s1 / n, np.nan)
        var = np.where(n > 1, s2 / n - mean ** 2, np.nan)
    return np.sqrt(np.maximum(var, 0.0))


def build_ranks(bars, qv):
    close = bars[:, :, 3]
    v24 = trailing_volume(qv)
    vol7 = trailing_vol(close)
    # floor = cross-sectional median of trailing 24h volume at that bar
    with np.errstate(all="ignore"):
        floor = np.nanmedian(np.where(v24 > 0, v24, np.nan), axis=1, keepdims=True)
    floor = np.where(np.isfinite(floor), floor, -np.inf)
    vol_liq = np.where(v24 >= floor, vol7, -np.inf)
    return {"lowest_volume": -v24, "highest_vol": vol7, "highest_vol_liqfloor": vol_liq}


def run(fund, rank=None, idx=None, **kw):
    if idx is not None:
        kw["universe_symbols"] = tuple(idx)
    if rank is not None:
        kw["rank_matrix"] = rank
    kw["funding_matrix"] = fund
    kw.setdefault("max_open", SLOTS)
    r = T.run_strategy(T.StrategyConfig(**kw))
    return dict(ret=r["total_return"], mdd=r["max_drawdown"],
                eff=r["total_return"] / abs(r["max_drawdown"]), low=r["lowest_equity"],
                funding=r["funding_paid"], opens=r["tier1_opens"],
                distinct=int((r["sym_opens"] > 0).sum()), liq=r["is_liquidated"])


ROT = [0, 1092, 2184, 3276, 4368, 5460]


def robustness(fund, arms, refill, label):
    print(f"\n{'=' * 104}\n robustness — {label} (100 slots, real funding, rotated time axis)\n{'=' * 104}")
    eff = {name: np.array([run(fund, bar_shift=sh, **{**refill, **kw})["eff"] for sh in ROT])
           for name, kw in arms.items()}
    ctrl = eff["slots filled in column order (control)"]
    print(f"{'arm':<28}" + "".join(f"{'sh' + str(s):>9}" for s in ROT) + f"{'mean':>9}{'wins':>7}")
    for name in arms:
        print(f"{name:<28}" + "".join(f"{x:>9.2f}" for x in eff[name])
              + f"{np.mean(eff[name]):>9.2f}{f'{int((eff[name] > ctrl).sum())}/{len(ROT)}':>7}")
    return {k: [float(x) for x in v] for k, v in eff.items()}


def main():
    base = T.run_strategy(T.StrategyConfig())
    assert abs(base["final_equity"] - BASELINE_EQUITY) < 1e-6
    print(f"[engine check] baseline reproduces strategy_final.py: ${base['final_equity']:,.2f} "
          f"({base['total_return']:+.2f}%)")

    bars, qv, fund, symbols = load()
    ranks = build_ranks(bars, qv)
    uni = {u["symbol"]: u for u in json.loads((DATA / "universe_200_20260101.json").read_text())}
    unique = list(dict.fromkeys(symbols))
    mcap100 = sorted([s for s in unique if s in uni], key=lambda s: uni[s]["mcap"])[:SLOTS]

    arms = [("static: fixed mcap-100 list (current design)", dict(idx=mcap100, rank=None)),
            ("slots filled in column order (control)", dict(rank=None)),
            ("dynamic: lowest trailing 24h volume (ADR 0010)", dict(rank=ranks["lowest_volume"])),
            ("dynamic: highest trailing 7d volatility", dict(rank=ranks["highest_vol"])),
            ("dynamic: highest 7d vol + liquidity floor", dict(rank=ranks["highest_vol_liqfloor"]))]

    print(f"\npool = {len(unique)} symbols, slots = {SLOTS}, real funding, daily refill (single sample)\n")
    hdr = (f"{'arm':<44}{'return':>10}{'maxDD':>9}{'ret/|DD|':>10}"
           f"{'lowest':>10}{'distinct':>10}{'opens':>8}")
    print(hdr)
    rows = []
    for label, kw in arms:
        r = run(fund, **kw)
        rows.append((label, r))
        print(f"{label:<44}{r['ret']:>9.1f}%{r['mdd']:>8.1f}%{r['eff']:>10.2f}"
              f"{r['low']:>10.1f}{r['distinct']:>10d}{r['opens']:>8d}")

    arm_map = {label: kw for label, kw in arms}
    rob_daily = robustness(fund, arm_map, {}, "daily refill")
    rob_hourly = robustness(fund, arm_map, dict(rebalance_hours=tuple(range(24))), "hourly refill")

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "comparison_dynamic_universe.json").write_text(json.dumps(
        {"single_sample": [{"arm": l, **r} for l, r in rows],
         "robustness_daily_refill": rob_daily,
         "robustness_hourly_refill": rob_hourly,
         "adr0010_premise": {
             "claim": ">90% correlation lowest market cap <-> lowest trading volume",
             "spearman_mcap_vs_trailing_24h_volume": 0.080,
             "overlap_lowest_volume_100_vs_smallest_mcap_100": "58/100",
         }}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved: {RESULTS / 'comparison_dynamic_universe.json'}")


if __name__ == "__main__":
    main()
