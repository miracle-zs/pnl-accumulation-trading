"""
Fetch real historical funding rates for Binance USDT-M perpetuals.

Source: https://data.binance.vision/data/futures/um/monthly/fundingRate/
(the REST /fapi/v1/fundingRate endpoint is geo-blocked from this host, and the
vision bucket is the same source fetch_klines.py already uses for klines.)

Outputs
  data/funding/<SYMBOL>_funding_2026.pkl   per-symbol (calc_time, rate)
  data/funding_matrix.npy                  float32 (num_bars, num_symbols)
                                           hourly rate aligned to bars_info.json,
                                           non-zero only at 00/08/16 UTC

Sign convention: positive rate => longs pay shorts. The engine costs a long
position `rate * notional` at that bar.
"""

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
FUNDING_DIR = DATA_DIR / "funding"
FUNDING_DIR.mkdir(exist_ok=True, parents=True)

MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]
ORIGIN_MS = 1767225600000          # 2026-01-01 00:00:00 UTC
HOUR_MS = 3600000


def download_symbol_month(symbol, month):
    url = (f"https://data.binance.vision/data/futures/um/monthly/fundingRate/"
           f"{symbol}/{symbol}-fundingRate-{month}.zip")
    try:
        res = requests.get(url, timeout=20)
        if res.status_code != 200:
            return symbol, month, None
        z = zipfile.ZipFile(io.BytesIO(res.content))
        df = pd.read_csv(z.open(z.namelist()[0]))
        df = df[["calc_time", "last_funding_rate"]].copy()
        df["calc_time"] = pd.to_numeric(df["calc_time"], errors="coerce")
        df["last_funding_rate"] = pd.to_numeric(df["last_funding_rate"], errors="coerce")
        return symbol, month, df.dropna()
    except Exception:
        return symbol, month, None


def download_all(symbols):
    todo = [s for s in symbols if not (FUNDING_DIR / f"{s}_funding_2026.pkl").exists()]
    print(f"funding rates: {len(symbols)} symbols, {len(todo)} to fetch")
    tasks = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for s in todo:
            for m in MONTHS:
                tasks.append(ex.submit(download_symbol_month, s, m))
        per = {s: [] for s in todo}
        done = 0
        for fut in as_completed(tasks):
            s, m, df = fut.result()
            if df is not None and not df.empty:
                per[s].append(df)
            done += 1
            if done % 250 == 0:
                print(f"  {done}/{len(tasks)}")
    saved = 0
    for s, dfs in per.items():
        if dfs:
            c = pd.concat(dfs, ignore_index=True)
            c = c.drop_duplicates(subset=["calc_time"]).sort_values("calc_time")
            c.to_pickle(FUNDING_DIR / f"{s}_funding_2026.pkl")
            saved += 1
    print(f"  saved {saved}/{len(todo)} symbols")
    return saved


def build_matrix():
    info = json.loads((DATA_DIR / "bars_info.json").read_text())
    symbols, num_bars = info["symbols"], len(info["hours"])
    mat = np.zeros((num_bars, len(symbols)), dtype=np.float32)
    covered = 0
    for j, s in enumerate(symbols):
        p = FUNDING_DIR / f"{s}_funding_2026.pkl"
        if not p.exists():
            continue
        df = pd.read_pickle(p)
        idx = np.round((df["calc_time"].to_numpy() - ORIGIN_MS) / HOUR_MS).astype(np.int64)
        ok = (idx >= 0) & (idx < num_bars)
        mat[idx[ok], j] = df["last_funding_rate"].to_numpy()[ok]
        covered += 1
    np.save(DATA_DIR / "funding_matrix.npy", mat)
    print(f"funding_matrix.npy {mat.shape}  symbols covered {covered}/{len(symbols)}")
    return mat


if __name__ == "__main__":
    info = json.loads((DATA_DIR / "bars_info.json").read_text())
    uniq = list(dict.fromkeys(info["symbols"]))
    download_all(uniq)
    build_matrix()
