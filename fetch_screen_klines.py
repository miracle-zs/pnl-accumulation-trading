"""
Build a genuinely PRE-PERIOD volatility screen from 2025-12 hourly klines.

The in-period screens used earlier had to sacrifice the first 30 days of 2026 as
a burn-in, and the "5 windows" were nested suffixes of one path. Using December
2025 instead means:
  * the screen data strictly precedes every traded bar (no burn-in, no look-ahead)
  * the whole 2026-01-01 .. 2026-09-30 period is traded, so the result is directly
    comparable to the frozen +286.78% baseline
  * the selected universe is fixed ex-ante, so a rotation test varies only the
    2026 outcome ordering, not the selection

Output: data/screen_vol_202512.json  {SYMBOL: std of hourly log returns}
"""

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA = Path(__file__).parent / "data"
MONTH = "2025-12"
WINDOW = 720          # hours in December


def fetch(sym):
    url = (f"https://data.binance.vision/data/futures/um/monthly/klines/"
           f"{sym}/1h/{sym}-1h-{MONTH}.zip")
    try:
        r = requests.get(url, timeout=25)
        if r.status_code != 200:
            return sym, None
        z = zipfile.ZipFile(io.BytesIO(r.content))
        df = pd.read_csv(z.open(z.namelist()[0]))
        df = df.iloc[:, :5]
        df.columns = ["open_time", "open", "high", "low", "close"]
        c = pd.to_numeric(df["close"], errors="coerce").dropna().to_numpy()
        if len(c) < WINDOW * 0.9:
            return sym, None
        r_ = np.diff(np.log(c))
        return sym, float(np.std(r_))
    except Exception:
        return sym, None


def main():
    info = json.loads((DATA / "bars_info_full.json").read_text())
    symbols = list(dict.fromkeys(info["symbols"]))
    print(f"fetching {MONTH} hourly klines for {len(symbols)} contracts")
    out = {}
    with ThreadPoolExecutor(max_workers=14) as ex:
        futs = [ex.submit(fetch, s) for s in symbols]
        for i, f in enumerate(as_completed(futs), 1):
            s, v = f.result()
            if v is not None:
                out[s] = v
            if i % 200 == 0:
                print(f"  {i}/{len(symbols)}")
    (DATA / "screen_vol_202512.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    ann = np.array(list(out.values())) * np.sqrt(24 * 365) * 100
    print(f"screen_vol_202512.json: {len(out)}/{len(symbols)} contracts with a usable {MONTH} window")
    print(f"annualised vol in {MONTH}: median {np.median(ann):.0f}%  "
          f"p10 {np.percentile(ann,10):.0f}%  p90 {np.percentile(ann,90):.0f}%")


if __name__ == "__main__":
    main()
