"""
End-to-end validation of the FINAL configuration.

  final = select the 196 highest-volatility contracts (screened on 2025-12,
          strictly before the traded period) + refill idle slots HOURLY,
          on the full 821-contract pool, with real funding

Evaluated against, all on the same full 2026-01-01..2026-09-30 period:
  * the current shipped design   (frozen universe_200, daily 08:00 refill)
  * the frozen universe with hourly refill
  * volatility selection with daily refill
so the two independent changes can be seen separately and together.

Rotations vary the 2026 outcome ordering only; the screen is fixed ex-ante.
"""

import json
import random
from pathlib import Path

import numpy as np

import tier_engine as T
from build_full_cache import cmc_lookup

DATA = Path("data")
RESULTS = DATA / "results"
SLOTS = 196
RANDOM_SEEDS = 4
HOURLY = tuple(range(24))
SHIFTS = (0, 1092, 2184, 3276, 4368, 5460)


def load():
    info = json.loads((DATA / "bars_info_full.json").read_text())
    symbols = list(dict.fromkeys(info["symbols"]))
    screen = json.loads((DATA / "screen_vol_202512.json").read_text())
    cmc = json.loads((DATA / "cmc_20260101.json").read_text())
    fund_full = np.load(DATA / "funding_matrix_full.npy")
    fund_frozen = np.load(DATA / "funding_matrix.npy")
    return symbols, screen, cmc, fund_full, fund_frozen


def frozen_universe():
    u = json.loads((DATA / "universe_200_20260101.json").read_text())
    return list(dict.fromkeys(x["symbol"] for x in u))


def run(frozen_universe_mode=False, idx=None, fund_full=None, fund_frozen=None,
        hours=None, shift=0, flat_funding=None):
    kw = {}
    if frozen_universe_mode:
        if flat_funding is None:
            kw["funding_matrix"] = fund_frozen          # frozen cache, no selection
        else:
            kw["funding_hourly"] = flat_funding
    else:
        kw["cache_prefix"] = "_full"
        kw["universe_symbols"] = tuple(idx)
        if flat_funding is None:
            kw["funding_matrix"] = fund_full
        else:
            kw["funding_hourly"] = flat_funding
    if hours:
        kw["rebalance_hours"] = hours
    if shift:
        kw["bar_shift"] = shift
    r = T.run_strategy(T.StrategyConfig(**kw))
    return dict(ret=r["total_return"], mdd=r["max_drawdown"],
                eff=r["total_return"] / abs(r["max_drawdown"]),
                low=r["lowest_equity"], opens=r["tier1_opens"],
                funding=r["funding_paid"], liq=r["is_liquidated"])


def main():
    base = T.run_strategy(T.StrategyConfig())
    assert abs(base["final_equity"] - 3867.8487458025065) < 1e-6
    print(f"[engine check] frozen baseline reproduces strategy_final.py: "
          f"${base['final_equity']:,.2f} ({base['total_return']:+.2f}%)")

    symbols, screen, cmc, fund_full, fund_frozen = load()
    eligible = [s for s in symbols if s in screen]
    vol196 = sorted(eligible, key=lambda s: -screen[s])[:SLOTS]
    # CMC keys are base assets ("ETH"); Binance contracts are "ETHUSDT".
    mcap_map = {s: cmc_lookup(s, cmc) for s in eligible}
    mcap_known = [s for s in eligible if mcap_map[s]]
    mcap196 = sorted(mcap_known, key=lambda s: mcap_map[s]["mcap"])[:SLOTS] if mcap_known else []

    print(f"\npool {len(symbols)} contracts -> {len(eligible)} with a usable 2025-12 screen "
          f"-> choosing {SLOTS}")
    print(f"  vol-196 median ann.vol {np.median([screen[s] for s in vol196])*np.sqrt(8760)*100:.0f}%"
          f"   mcap-196 median ann.vol {np.median([screen[s] for s in mcap196])*np.sqrt(8760)*100:.0f}%")
    print(f"  overlap vol-196 vs mcap-196: {len(set(vol196) & set(mcap196))}/{SLOTS}")

    arms = [
        ("current design: frozen u200, daily", dict(frozen_universe_mode=True)),
        ("frozen u200 + hourly refill", dict(frozen_universe_mode=True, hours=HOURLY)),
        ("vol-196 + daily refill", dict(idx=vol196)),
        ("** FINAL: vol-196 + hourly refill **", dict(idx=vol196, hours=HOURLY)),
        ("lowest-mcap-196 + hourly (control)", dict(idx=mcap196, hours=HOURLY)),
    ]

    print(f"\n{'=' * 104}\n single sample (shift 0), full 2026-01-01..09-30, real funding\n{'=' * 104}")
    print(f"{'arm':<38}{'return':>10}{'maxDD':>9}{'ret/|DD|':>10}{'lowest':>10}"
          f"{'opens':>8}{'funding':>12}")
    single = {}
    for label, kw in arms:
        r = run(fund_full=fund_full, fund_frozen=fund_frozen, **kw)
        single[label] = r
        print(f"{label:<38}{r['ret']:>9.1f}%{r['mdd']:>8.1f}%{r['eff']:>10.2f}"
              f"{r['low']:>10.1f}{r['opens']:>8d}{r['funding']:>+11.2f}U")

    print(f"\n{'=' * 104}\n rotation robustness — 2026 outcome ordering varied, screen fixed ex-ante\n{'=' * 104}")
    eff = {label: [] for label, _ in arms}
    for sh in SHIFTS:
        for label, kw in arms:
            eff[label].append(run(fund_full=fund_full, fund_frozen=fund_frozen, shift=sh, **kw)["eff"])
        pick = random.Random(sh).sample(eligible, SLOTS)
        eff.setdefault("random-196 + hourly", []).append(
            run(idx=pick, fund_full=fund_full, fund_frozen=fund_frozen, hours=HOURLY,
                shift=sh)["eff"])
    ref = np.array(eff["current design: frozen u200, daily"])
    print(f"{'arm':<38}" + "".join(f"{'sh' + str(s):>9}" for s in SHIFTS) + f"{'mean':>9}{'wins':>7}")
    for label in eff:
        a = np.array(eff[label])
        print(f"{label:<38}" + "".join(f"{x:>9.2f}" for x in a)
              + f"{a.mean():>9.2f}{f'{int((a > ref).sum())}/{len(SHIFTS)}':>7}")

    print(f"\n{'=' * 104}\n funding-regime stress on the FINAL config (flat rate, no real matrix)\n{'=' * 104}")
    print(f"{'regime':<26}{'current design':>18}{'FINAL (vol+hourly)':>22}")
    fin_kw = dict(idx=vol196, hours=HOURLY)
    for label, fr in [("0", 0.0), ("+0.01%/8h", 0.0001 / 8), ("+0.03%/8h", 0.0003 / 8),
                      ("+0.05%/8h", 0.0005 / 8), ("+0.10%/8h", 0.0010 / 8)]:
        a = run(fund_full=fund_full, fund_frozen=fund_frozen, frozen_universe_mode=True,
                flat_funding=fr)
        b = run(fund_full=fund_full, fund_frozen=fund_frozen, flat_funding=fr, **fin_kw)
        print(f"{label:<26}{a['ret']:>17.1f}%{b['ret']:>21.1f}%"
              f"{'   LIQ' if a['liq'] else ''}{'   LIQ' if b['liq'] else ''}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "comparison_final_config.json").write_text(json.dumps({
        "single_sample": single,
        "rotation": {k: [float(x) for x in v] for k, v in eff.items()},
        "screen": {"source": "2025-12 hourly klines (pre-period)",
                   "eligible": len(eligible), "slots": SLOTS,
                   "overlap_vol_vs_mcap": len(set(vol196) & set(mcap196))},
    }, indent=2), encoding="utf-8")
    print(f"\nSaved: {RESULTS / 'comparison_final_config.json'}")


if __name__ == "__main__":
    main()
