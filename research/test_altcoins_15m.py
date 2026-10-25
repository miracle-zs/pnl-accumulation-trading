import io, zipfile, requests, pandas as pd
from concurrent.futures import ThreadPoolExecutor
import numpy as np

SYMBOLS = [
    "1000PEPEUSDT", "1000FLOKIUSDT", "1000BONKUSDT", "1000SHIBUSDT", 
    "BOMEUSDT", "PEOPLEUSDT", "TRBUSDT", "ORDIUSDT", "TIAUSDT", 
    "BLURUSDT", "GASUSDT", "JTOUSDT", "MEMEUSDT", "WLDUSDT", 
    "ARKMUSDT", "PIXELUSDT", "PENDLEUSDT", "DYDXUSDT", "GALAUSDT", "SANDUSDT"
]
MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]

def dl(sym, month):
    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/15m/{sym}-15m-{month}.zip"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            df = pd.read_csv(z.open(z.namelist()[0])).iloc[:, :6]
            df.columns = ["open_time", "open", "high", "low", "close", "volume"]
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            for c in ["open", "high", "low", "close"]: 
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return sym, df
    except: 
        pass
    return sym, None

tasks = [(s, m) for s in SYMBOLS for m in MONTHS]
res = {}
with ThreadPoolExecutor(max_workers=16) as ex:
    for s, df in ex.map(lambda x: dl(*x), tasks):
        if df is not None: 
            res.setdefault(s, []).append(df)

all_dfs = {s: pd.concat(dfs).drop_duplicates("open_time").sort_values("open_time").set_index("open_time") for s, dfs in res.items() if dfs}
common_idx = sorted(list(set.union(*[set(df.index) for df in all_dfs.values()])))
val_syms = list(all_dfs.keys())
bars = np.full((len(common_idx), len(val_syms), 4), np.nan)
idx_map = {t: i for i, t in enumerate(common_idx)}
for si, s in enumerate(val_syms):
    df = all_dfs[s]
    rows = [idx_map[t] for t in df.index]
    bars[rows, si, 0] = df["open"].values
    bars[rows, si, 1] = df["high"].values
    bars[rows, si, 2] = df["low"].values
    bars[rows, si, 3] = df["close"].values

import test_15m_review_comparison as T15
r_15m = T15.run_test_sim(bars, "15m_review", 0.10)
r_1h = T15.run_test_sim(bars, "1h_review", 0.10)
r_lim = T15.run_test_sim(bars, "limit_tp", 0.10)

print(f"\n--- VOLATILE ALTCOINS ({len(val_syms)} symbols) ---")
print(f"15m Review: Net ${r_15m['net_profit']:>8.2f} | Cash ${r_15m['realized_cash']:>8.2f} | Float ${r_15m['floating_pnl']:>8.2f} | TPs {r_15m['total_tps']}")
print(f"1h Review:  Net ${r_1h['net_profit']:>8.2f} | Cash ${r_1h['realized_cash']:>8.2f} | Float ${r_1h['floating_pnl']:>8.2f} | TPs {r_1h['total_tps']}")
print(f"Limit TP:   Net ${r_lim['net_profit']:>8.2f} | Cash ${r_lim['realized_cash']:>8.2f} | Float ${r_lim['floating_pnl']:>8.2f} | TPs {r_lim['total_tps']}")
