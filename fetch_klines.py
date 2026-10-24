import io
import json
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
KLINES_DIR = DATA_DIR / "klines"
KLINES_DIR.mkdir(exist_ok=True, parents=True)

MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]

def download_symbol_month(symbol, month):
    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1h/{symbol}-1h-{month}.zip"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(res.content))
            csv_name = z.namelist()[0]
            df = pd.read_csv(z.open(csv_name))
            # Format dataframe
            # Columns: open_time, open, high, low, close, volume, close_time, quote_volume, count, taker_buy_vol, taker_buy_quote_vol, ignore
            df = df.iloc[:, :6]
            df.columns = ["open_time", "open", "high", "low", "close", "volume"]
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            return symbol, month, df
        elif res.status_code == 404:
            # Not listed yet or delisted
            return symbol, month, None
        else:
            return symbol, month, None
    except Exception as e:
        return symbol, month, None

def download_all_klines(symbols):
    print(f"Downloading 1h klines for {len(symbols)} symbols across {len(MONTHS)} months...")
    tasks = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        for sym in symbols:
            # Check if combined parquet already exists
            combined_file = KLINES_DIR / f"{sym}_1h_2026.pkl"
            if combined_file.exists():
                continue
            for month in MONTHS:
                tasks.append(executor.submit(download_symbol_month, sym, month))
                
        symbol_dfs = {sym: [] for sym in symbols}
        completed = 0
        total = len(tasks)
        if total == 0:
            print("All klines already downloaded and cached!")
            return

        for future in as_completed(tasks):
            sym, month, df = future.result()
            if df is not None and not df.empty:
                symbol_dfs[sym].append(df)
            completed += 1
            if completed % 100 == 0 or completed == total:
                print(f"Progress: {completed}/{total} files processed ({(completed/total)*100:.1f}%)")

        # Combine and save per symbol
        saved = 0
        for sym, dfs in symbol_dfs.items():
            if dfs:
                combined = pd.concat(dfs, ignore_index=True)
                combined.drop_duplicates(subset=["open_time"], inplace=True)
                combined.sort_values(by="open_time", inplace=True)
                combined.reset_index(drop=True, inplace=True)
                combined.to_pickle(KLINES_DIR / f"{sym}_1h_2026.pkl")
                saved += 1
        print(f"Successfully saved combined klines for {saved} symbols.")

if __name__ == "__main__":
    universe_file = DATA_DIR / "initial_universe_20260101.json"
    if not universe_file.exists():
        print("Please run fetch_universe.py first!")
    else:
        symbols = [x["symbol"] for x in json.loads(universe_file.read_text())]
        download_all_klines(symbols)
