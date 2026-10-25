"""
Universe selection from the FULL Binance USDT-M perpetual pool.

Previously impossible: the frozen cache held only the 197 columns of
universe_200_20260101.json, so the pool (196 usable) was never larger than the
number of slots the strategy occupies (~196). "Which symbols deserve a slot" was
therefore not a real decision -- there was no scarcity to allocate.

This uses data/bars_cache_full.npy (821 contracts, built by build_full_cache.py)
and asks the question properly: of the whole tradeable pool, which 196 symbols
should hold the slots?

Arms (all screened out-of-sample: metrics use only data before `start_bar`):
  lowest mcap       the current documented rule (smallest market cap first)
  highest vol       ex-ante realised vol, 720h window (same untuned value as before)
  highest vol + liq vol screen restricted to above-median liquidity
  lowest volume     ADR 0010's documented production failover rule
  highest volume
  random            control

Two pools are reported because only 576 of 821 contracts have a CMC market cap:
  pool A (mcap-known)  apples-to-apples for the mcap rule
  pool B (all)         vol/volume/random only
"""

import json
import random
from pathlib import Path

import numpy as np
import pandas as pd

import tier_engine as T
from build_full_cache import cmc_lookup

DATA = Path("data")
RESULTS = DATA / "results"
SLOTS = 196
VOL_WINDOW = 720          # 30 days; unchanged from the earlier, untuned choice
RANDOM_SEEDS = 4
WINDOWS = (720, 2160)


def load():
    bars = np.load(DATA / "bars_cache_full.npy")
    info = json.loads((DATA / "bars_info_full.json").read_text())
    qv = np.load(DATA / "volume_cache_full.npy").astype(np.float64)
    cmc = json.loads((DATA / "cmc_20260101.json").read_text())
    fp = DATA / "funding_matrix_full.npy"
    fund = np.load(fp) if fp.exists() else None
    return bars, info["symbols"], qv, cmc, fund


def screen(bars, qv, cmc, symbols, W, window=VOL_WINDOW):
    """Ex-ante screen metrics from [W-window, W). No look-ahead."""
    lo = max(0, W - window)
    rows = {}
    for j, s in enumerate(symbols):
        c = bars[lo:W, j, 3]
        m = np.isfinite(c)
        if m.sum() < window * 0.9:
            continue
        r = np.diff(np.log(c[m]))
        if len(r) < 24:
            continue
        v = qv[lo:W, j]
        entry = cmc_lookup(s, cmc)
        rows[s] = dict(vol=float(np.std(r)),
                       volume=float(np.nansum(v)),
                       mcap=float(entry["mcap"]) if entry else np.nan)
    return pd.DataFrame(rows).T


def run(idx, W, fund, shift=0):
    kw = dict(universe_symbols=tuple(idx), start_bar=W, cache_prefix="_full")
    if shift:
        kw["bar_shift"] = shift
    if fund is not None:
        kw["funding_matrix"] = fund
    r = T.run_strategy(T.StrategyConfig(**kw))
    return dict(ret=r["total_return"], mdd=r["max_drawdown"],
                eff=r["total_return"] / abs(r["max_drawdown"]),
                low=r["lowest_equity"], opens=r["tier1_opens"], liq=r["is_liquidated"])


def arms_for(F, has_mcap):
    """Selection rules evaluated on the given eligible pool."""
    out = {}
    if has_mcap:
        out["lowest mcap (current rule)"] = list(F["mcap"].nsmallest(SLOTS).index)
    out["highest ex-ante vol"] = list(F["vol"].nlargest(SLOTS).index)
    liq = F[F["volume"] >= F["volume"].median()]
    out["highest vol + liquidity floor"] = list(liq["vol"].nlargest(SLOTS).index)
    out["lowest volume (ADR 0010)"] = list(F["volume"].nsmallest(SLOTS).index)
    out["highest volume"] = list(F["volume"].nlargest(SLOTS).index)
    return out


def part_rotation(bars, symbols, qv, cmc, fund,
                  shifts=(0, 1092, 2184, 3276, 4368, 5460), W=720):
    """Re-screen on each rotated axis, then trade. Rotation is the only honest
    stress test available here: the two `windows` above are nested suffixes of a
    single price path, not independent samples."""
    print("\n" + "=" * 100)
    print(" rotation robustness — full pool, 196 slots, re-screened on each rotated axis")
    print("=" * 100)
    frozen_fund = np.load(DATA / "funding_matrix.npy")
    res = {"frozen universe_200 (current design)": [],
           "lowest mcap (re-screened)": [],
           "highest ex-ante vol (re-screened)": [],
           "random": []}
    for sh in shifts:
        rb, rq = np.roll(bars, sh, axis=0), np.roll(qv, sh, axis=0)
        F = screen(rb, rq, cmc, symbols, W)
        Fm = F[F["mcap"].notna()]
        picks = {"lowest mcap (re-screened)": list(Fm["mcap"].nsmallest(SLOTS).index),
                 "highest ex-ante vol (re-screened)": list(F["vol"].nlargest(SLOTS).index)}
        for name, idx in picks.items():
            r = run(idx, W, fund, shift=sh)
            res[name].append(r["eff"])
        r = T.run_strategy(T.StrategyConfig(start_bar=W, bar_shift=sh, funding_matrix=frozen_fund))
        res["frozen universe_200 (current design)"].append(
            r["total_return"] / abs(r["max_drawdown"]))
        rr = [run(random.Random(sh * 100 + sd).sample(list(F.index), SLOTS), W, fund,
                  shift=sh)["eff"] for sd in range(RANDOM_SEEDS)]
        res["random"].append(float(np.mean(rr)))

    base = np.array(res["frozen universe_200 (current design)"])
    print(f"  {'arm':<36}" + "".join(f"{'sh' + str(s):>9}" for s in shifts) + f"{'mean':>9}{'wins':>7}")
    for k, v in res.items():
        a = np.array(v)
        print(f"  {k:<36}" + "".join(f"{x:>9.2f}" for x in a)
              + f"{a.mean():>9.2f}{f'{int((a > base).sum())}/{len(shifts)}':>7}")
    return {k: [float(x) for x in v] for k, v in res.items()}


def main():
    base = T.run_strategy(T.StrategyConfig())
    assert abs(base["final_equity"] - 3867.8487458025065) < 1e-6
    print(f"[engine check] frozen baseline still reproduces: ${base['final_equity']:,.2f}")

    bars, symbols, qv, cmc, fund = load()
    print(f"full pool: {len(symbols)} contracts, funding matrix: "
          f"{'yes' if fund is not None else 'NOT YET BUILT (running without funding)'}")

    all_rows = []
    for W in WINDOWS:
        F = screen(bars, qv, cmc, symbols, W)
        Fm = F[F["mcap"].notna()]
        print(f"\n{'=' * 100}")
        print(f" trade from bar {W} ({W // 24}d)   eligible {len(F)} symbols   "
              f"with CMC mcap {len(Fm)}   slots {SLOTS}")
        print("=" * 100)
        for pool_name, pool, has_mcap in [("pool A (mcap known)", Fm, True),
                                          ("pool B (all eligible)", F, False)]:
            print(f"\n  {pool_name} — universe size {len(pool)}, choosing {SLOTS}")
            print(f"  {'arm':<34}{'return':>10}{'maxDD':>9}{'ret/|DD|':>10}{'lowest':>10}{'opens':>8}")
            picks = arms_for(pool, has_mcap)
            for label, idx in picks.items():
                r = run(idx, W, fund)
                all_rows.append(dict(window=W, pool=pool_name, arm=label, **r))
                print(f"  {label:<34}{r['ret']:>9.1f}%{r['mdd']:>8.1f}%{r['eff']:>10.2f}"
                      f"{r['low']:>10.1f}{r['opens']:>8d}")
            rr = [run(random.Random(W * 100 + sd).sample(list(pool.index), SLOTS), W, fund)
                  for sd in range(RANDOM_SEEDS)]
            all_rows.append(dict(window=W, pool=pool_name, arm="random (mean of 4)",
                                 ret=float(np.mean([x["ret"] for x in rr])),
                                 mdd=float(np.mean([x["mdd"] for x in rr])),
                                 eff=float(np.mean([x["eff"] for x in rr])),
                                 low=float(np.mean([x["low"] for x in rr])),
                                 opens=float(np.mean([x["opens"] for x in rr])), liq=False))
            print(f"  {'random (mean of 4)':<34}{np.mean([x['ret'] for x in rr]):>9.1f}%"
                  f"{np.mean([x['mdd'] for x in rr]):>8.1f}%{np.mean([x['eff'] for x in rr]):>10.2f}")

        # how different are the candidate lists?
        if len(Fm) >= SLOTS:
            a = set(Fm["mcap"].nsmallest(SLOTS).index)
            b = set(F["vol"].nlargest(SLOTS).index)
            print(f"\n  overlap: lowest-mcap-{SLOTS} vs highest-vol-{SLOTS} = {len(a & b)}/{SLOTS}")
            print(f"  median mcap  selected-by-mcap ${Fm.loc[list(a), 'mcap'].median():,.0f}  "
                  f"selected-by-vol ${F.loc[list(b), 'mcap'].median():,.0f}")

    rot = part_rotation(bars, symbols, qv, cmc, fund)

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "comparison_fulluniverse_selection.json").write_text(
        json.dumps({"single_sample": all_rows, "rotation": rot}, indent=2), encoding="utf-8")
    print(f"\nSaved: {RESULTS / 'comparison_fulluniverse_selection.json'}")


if __name__ == "__main__":
    main()
