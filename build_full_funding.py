"""
Fetch funding rates for the FULL perpetual pool and build the aligned matrix.

Reuses the per-symbol funding pickle cache in data/funding/ (fetch_funding.py),
so only symbols not yet fetched hit the network.

Output: data/funding_matrix_full.npy  float32 (6552, N) aligned to
        data/bars_info_full.json
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

import fetch_funding as FF

DATA = Path(__file__).parent / "data"
ORIGIN_MS = 1767225600000
HOUR_MS = 3600000


def main():
    info = json.loads((DATA / "bars_info_full.json").read_text())
    symbols = list(dict.fromkeys(info["symbols"]))
    num_bars = len(info["hours"])

    FF.download_all(symbols)

    mat = np.zeros((num_bars, len(info["symbols"])), dtype=np.float32)
    covered = 0
    for j, s in enumerate(info["symbols"]):
        p = DATA / "funding" / f"{s}_funding_2026.pkl"
        if not p.exists():
            continue
        df = pd.read_pickle(p)
        idx = np.round((df["calc_time"].to_numpy() - ORIGIN_MS) / HOUR_MS).astype(np.int64)
        ok = (idx >= 0) & (idx < num_bars)
        mat[idx[ok], j] = df["last_funding_rate"].to_numpy()[ok]
        covered += 1
    np.save(DATA / "funding_matrix_full.npy", mat)
    print(f"funding_matrix_full.npy {mat.shape}  covered {covered}/{len(info['symbols'])}")


if __name__ == "__main__":
    main()
