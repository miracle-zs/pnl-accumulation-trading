"""
Build a bars cache spanning EVERY Binance USDT-M perpetual with 2026 klines.

The frozen bars_cache.npy only holds the 197 columns of universe_200_20260101.json
(the 200 smallest market caps on 2026-01-01). That pool is smaller than the
number of slots the strategy actually uses, so "which symbols should occupy the
slots" was never a real decision -- there was no scarcity to allocate.

This builds data/bars_cache_full.npy over the whole tradeable pool on the SAME
hourly grid as the frozen cache (2026-01-01 00:00 UTC + 6552 hours), so every
earlier result stays directly comparable.

Outputs
  data/bars_cache_full.npy   float64 (6552, N, 4) = open, high, low, close
  data/bars_info_full.json   {"hours": [...], "symbols": [...]}
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
KLINES = DATA / "klines"
ORIGIN = pd.Timestamp("2026-01-01")
NUM_BARS = 6552          # 2026-01-01 00:00 -> 2026-09-30 23:00, matches bars_info.json


def load_cmc():
    p = DATA / "cmc_20260101.json"
    return json.loads(p.read_text()) if p.exists() else {}


def cmc_lookup(symbol, cmc):
    """Map a Binance contract to its CMC entry, handling 1000x / 1000000x prefixes."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    cands = []
    if base.startswith("1000000"):
        cands.append(base[7:])
    if base.startswith("1000"):
        cands.append(base[4:])
    cands.append(base)
    for c in cands:
        if c and c in cmc:
            return cmc[c]
    return None


def main():
    files = sorted(KLINES.glob("*_1h_2026.pkl"))
    print(f"kline files available: {len(files)}")
    cmc = load_cmc()

    symbols, cols = [], []
    for f in files:
        sym = f.name.replace("_1h_2026.pkl", "")
        try:
            df = pd.read_pickle(f)
        except Exception:
            continue
        if df.empty or "open_time" not in df.columns:
            continue
        symbols.append(sym)
        cols.append(df)

    n = len(symbols)
    bars = np.full((NUM_BARS, n, 4), np.nan, dtype=np.float64)
    kept = []
    for j, (sym, df) in enumerate(zip(symbols, cols)):
        idx = ((df["open_time"] - ORIGIN).dt.total_seconds() // 3600).round().astype("int64").to_numpy()
        ok = (idx >= 0) & (idx < NUM_BARS)
        if ok.sum() < 24:
            continue
        for k, col in enumerate(["open", "high", "low", "close"]):
            bars[idx[ok], j, k] = pd.to_numeric(df[col], errors="coerce").to_numpy()[ok]
        kept.append(j)

    bars = bars[:, kept, :]
    symbols = [symbols[j] for j in kept]
    hours = [(ORIGIN + pd.Timedelta(hours=i)).hour for i in range(NUM_BARS)]

    np.save(DATA / "bars_cache_full.npy", bars)
    (DATA / "bars_info_full.json").write_text(
        json.dumps({"hours": hours, "symbols": symbols}), encoding="utf-8")

    matched = sum(1 for s in symbols if cmc_lookup(s, cmc))
    print(f"bars_cache_full.npy {bars.shape}")
    print(f"symbols {len(symbols)}   with CMC market cap {matched}   without {len(symbols) - matched}")
    print(f"median bars present per symbol: {int(np.median(np.isfinite(bars[:, :, 3]).sum(axis=0)))}")
    print(f"symbols with full 6552 bars: {int((np.isfinite(bars[:, :, 3]).sum(axis=0) == NUM_BARS).sum())}")


if __name__ == "__main__":
    main()
