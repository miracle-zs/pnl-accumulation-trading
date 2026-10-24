import os
import io
import zipfile
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd

DATA_DIR = Path("data")
OUT_FILE = DATA_DIR / "cache_15m_40symbols.npz"

SYMBOLS = [
    # 20 Majors
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "NEARUSDT", 
    "AVAXUSDT", "LINKUSDT", "BNBUSDT", "ADAUSDT", "XRPUSDT", 
    "SUIUSDT", "APTUSDT", "FETUSDT", "RENDERUSDT", "INJUSDT", 
    "ARBUSDT", "OPUSDT", "TIAUSDT", "SEIUSDT", "STXUSDT",
    # 20 Volatile Altcoins
    "1000PEPEUSDT", "1000FLOKIUSDT", "1000BONKUSDT", "1000SHIBUSDT", 
    "BOMEUSDT", "PEOPLEUSDT", "TRBUSDT", "ORDIUSDT", "BLURUSDT", 
    "GASUSDT", "JTOUSDT", "MEMEUSDT", "WLDUSDT", "ARKMUSDT", 
    "PIXELUSDT", "PENDLEUSDT", "DYDXUSDT", "GALAUSDT", "SANDUSDT", "CRVUSDT"
]
MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]

def dl(sym, month):
    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/15m/{sym}-15m-{month}.zip"
    try:
        r = requests.get(url, timeout=12)
        if r.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            df = pd.read_csv(z.open(z.namelist()[0])).iloc[:, :6]
            df.columns = ["open_time", "open", "high", "low", "close", "volume"]
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            for c in ["open", "high", "low", "close"]: 
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return sym, df
    except Exception:
        pass
    return sym, None

def main():
    if OUT_FILE.exists():
        print(f"File {OUT_FILE} already exists!")
        return

    print(f"Downloading 15m data for {len(SYMBOLS)} symbols across {len(MONTHS)} months...")
    tasks = [(s, m) for s in SYMBOLS for m in MONTHS]
    res = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        for s, df in ex.map(lambda x: dl(*x), tasks):
            if df is not None and not df.empty:
                res.setdefault(s, []).append(df)

    all_dfs = {s: pd.concat(dfs).drop_duplicates("open_time").sort_values("open_time").set_index("open_time") for s, dfs in res.items() if dfs}
    common_idx = sorted(list(set.union(*[set(df.index) for df in all_dfs.values()])))
    val_syms = [s for s in SYMBOLS if s in all_dfs]
    num_bars = len(common_idx)
    bars = np.full((num_bars, len(val_syms), 4), np.nan, dtype=np.float64)
    idx_map = {t: i for i, t in enumerate(common_idx)}

    for si, s in enumerate(val_syms):
        df = all_dfs[s]
        rows = [idx_map[t] for t in df.index]
        bars[rows, si, 0] = df["open"].values
        bars[rows, si, 1] = df["high"].values
        bars[rows, si, 2] = df["low"].values
        bars[rows, si, 3] = df["close"].values

    # forward fill nans if any
    for si in range(len(val_syms)):
        col = bars[:, si, :]
        mask = np.isnan(col[:, 3])
        if np.any(mask):
            # forward fill close
            idx = np.where(~mask)[0]
            if len(idx) > 0:
                first = idx[0]
                bars[:first, si, :] = np.nan
                for b in range(first + 1, num_bars):
                    if np.isnan(bars[b, si, 3]):
                        bars[b, si, :] = bars[b - 1, si, 3]

    timestamps = [str(t) for t in common_idx]
    np.savez_compressed(OUT_FILE, bars=bars, symbols=val_syms, timestamps=timestamps)
    print(f"Saved {num_bars} bars for {len(val_syms)} symbols to {OUT_FILE}")

if __name__ == "__main__":
    main()
