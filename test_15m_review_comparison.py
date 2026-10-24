"""
Empirical Comparison: 15-Minute Periodic PnL Review vs 1-Hour Review vs Limit Order TP
Across 20 Major Active Binance USDT-M Symbols (Jan - Sep 2026).
"""

import io
import json
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd
import requests

DATA_DIR = Path("data")
CACHE_15M = DATA_DIR / "cache_15m_test.npz"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "NEARUSDT", 
    "AVAXUSDT", "LINKUSDT", "BNBUSDT", "ADAUSDT", "XRPUSDT", 
    "SUIUSDT", "APTUSDT", "FETUSDT", "RENDERUSDT", "INJUSDT", 
    "ARBUSDT", "OPUSDT", "TIAUSDT", "SEIUSDT", "STXUSDT"
]
MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]

def download_symbol_month(sym, month):
    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/15m/{sym}-15m-{month}.zip"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(res.content))
            df = pd.read_csv(z.open(z.namelist()[0]))
            df = df.iloc[:, :6]
            df.columns = ["open_time", "open", "high", "low", "close", "volume"]
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            return sym, month, df
    except Exception:
        pass
    return sym, month, None

def get_15m_data():
    if CACHE_15M.exists():
        print("Loading cached 15m test data...")
        data = np.load(CACHE_15M)
        return data["bars_15m"], list(data["symbols"])

    print(f"Downloading 15m klines for {len(SYMBOLS)} symbols across {len(MONTHS)} months...")
    tasks = [(s, m) for s in SYMBOLS for m in MONTHS]
    results = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(download_symbol_month, s, m) for s, m in tasks]
        for f in futures:
            s, m, df = f.result()
            if df is not None and not df.empty:
                results.setdefault(s, []).append(df)

    # Sort and align
    all_dfs = {}
    common_idx = None
    for s, dfs in results.items():
        comb = pd.concat(dfs).drop_duplicates("open_time").sort_values("open_time").set_index("open_time")
        all_dfs[s] = comb
        if common_idx is None:
            common_idx = comb.index
        else:
            common_idx = common_idx.union(comb.index)

    common_idx = common_idx.sort_values()
    valid_symbols = [s for s in SYMBOLS if s in all_dfs]
    num_bars = len(common_idx)
    bars_15m = np.full((num_bars, len(valid_symbols), 4), np.nan, dtype=np.float64)

    for i, s in enumerate(valid_symbols):
        df_re = all_dfs[s].reindex(common_idx)
        bars_15m[:, i, 0] = df_re["open"].values
        bars_15m[:, i, 1] = df_re["high"].values
        bars_15m[:, i, 2] = df_re["low"].values
        bars_15m[:, i, 3] = df_re["close"].values

    np.savez_compressed(CACHE_15M, bars_15m=bars_15m, symbols=valid_symbols)
    print(f"Cached {num_bars} 15m bars for {len(valid_symbols)} symbols.")
    return bars_15m, valid_symbols

def run_test_sim(bars, mode="15m_review", tp_threshold=0.10):
    """
    mode:
      - '15m_review': every 15m bar close, if (c - avg)/avg >= tp_threshold: market close & reopen.
      - '1h_review': every 4th 15m bar close (every 1 hour), if (c - avg)/avg >= tp_threshold: close & reopen.
      - 'limit_tp': intra-bar limit TP at avg*(1+tp_threshold).
    """
    num_bars, num_symbols, _ = bars.shape
    initial_notional = 10.0
    maker_fee = 0.0002
    taker_fee = 0.0005
    t2_buy_drop = 0.50
    t2_notional = 20.0
    t3_buy_drop = 0.50
    t3_notional = 60.0

    is_open = np.zeros(num_symbols, dtype=bool)
    tier = np.zeros(num_symbols, dtype=np.int32)
    qty = np.zeros(num_symbols, dtype=np.float64)
    size_notional = np.zeros(num_symbols, dtype=np.float64)
    avg_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_notional = np.zeros(num_symbols, dtype=np.float64)

    sell_price = np.zeros(num_symbols, dtype=np.float64)
    sell_active = np.zeros(num_symbols, dtype=bool)

    total_realized_cash = 0.0
    total_fees_paid = 0.0
    total_tps = 0
    total_opens = 0

    has_limit_tp = (mode == "limit_tp")

    def open_sym(s, p):
        nonlocal total_fees_paid, total_opens
        fee = initial_notional * taker_fee
        total_fees_paid += fee
        q = initial_notional / p
        is_open[s] = True
        tier[s] = 1
        qty[s] = q
        size_notional[s] = initial_notional
        avg_price[s] = p
        limit_buy_price[s] = p * (1.0 - t2_buy_drop)
        limit_buy_notional[s] = t2_notional
        sell_active[s] = False
        if has_limit_tp:
            sell_active[s] = True
            sell_price[s] = p * (1.0 + tp_threshold)
        total_opens += 1

    for s in range(num_symbols):
        p0 = bars[0, s, 0]
        if not np.isnan(p0) and p0 > 0:
            open_sym(s, p0)

    last_p = np.zeros(num_symbols)

    for b in range(num_bars):
        c_bar = bars[b, :, 3]
        valid_c = ~np.isnan(c_bar) & (c_bar > 0)
        last_p[valid_c] = c_bar[valid_c]

        o_bar = bars[b, :, 0]
        h_bar = bars[b, :, 1]
        l_bar = bars[b, :, 2]

        for s in np.where(is_open)[0]:
            h, l, c = h_bar[s], l_bar[s], c_bar[s]
            if np.isnan(h) or h <= 0: continue

            # Limit DCA check
            if limit_buy_price[s] > 0 and l <= limit_buy_price[s]:
                lb_p = limit_buy_price[s]
                lb_n = limit_buy_notional[s]
                fee = lb_n * maker_fee
                total_fees_paid += fee
                add_q = lb_n / lb_p
                qty[s] += add_q
                size_notional[s] += lb_n
                avg_price[s] = size_notional[s] / qty[s]
                tier[s] += 1
                if tier[s] == 2:
                    limit_buy_price[s] = lb_p * (1.0 - t3_buy_drop)
                    limit_buy_notional[s] = t3_notional
                else:
                    limit_buy_price[s] = 0.0
                    limit_buy_notional[s] = 0.0
                if has_limit_tp:
                    sell_active[s] = True
                    sell_price[s] = avg_price[s] * (1.0 + tp_threshold)

            # Intraday Limit Sell check
            if has_limit_tp and sell_active[s] and h >= sell_price[s]:
                sp = sell_price[s]
                pnl = (sp - avg_price[s]) * qty[s]
                fee = (sp * qty[s]) * maker_fee
                total_realized_cash += (pnl - fee)
                total_fees_paid += fee
                total_tps += 1
                is_open[s] = False
                sell_active[s] = False
                # Reopen at close of this 15m bar
                open_sym(s, c)

        # Periodic Review Check
        eval_now = False
        if mode == "15m_review":
            eval_now = True
        elif mode == "1h_review" and (b % 4 == 3):  # every 4 bars = 1 hour
            eval_now = True

        if eval_now:
            for s in np.where(is_open)[0]:
                c = c_bar[s]
                if np.isnan(c) or c <= 0: continue
                ret = (c - avg_price[s]) / avg_price[s]
                if ret >= tp_threshold:
                    pnl = (c - avg_price[s]) * qty[s]
                    fee = (c * qty[s]) * taker_fee
                    total_realized_cash += (pnl - fee)
                    total_fees_paid += fee
                    total_tps += 1
                    is_open[s] = False
                    open_sym(s, c)

    # End summary
    floating = 0.0
    for s in np.where(is_open)[0]:
        floating += (last_p[s] - avg_price[s]) * qty[s]

    net_profit = total_realized_cash + floating

    return {
        "mode": mode,
        "net_profit": net_profit,
        "realized_cash": total_realized_cash,
        "floating_pnl": floating,
        "fees_paid": total_fees_paid,
        "total_tps": total_tps,
        "total_opens": total_opens
    }

if __name__ == "__main__":
    bars_15m, val_symbols = get_15m_data()
    print(f"\nRunning 15m test on {len(val_symbols)} symbols across {bars_15m.shape[0]} 15-minute bars...")

    res_15m = run_test_sim(bars_15m, mode="15m_review", tp_threshold=0.10)
    res_1h = run_test_sim(bars_15m, mode="1h_review", tp_threshold=0.10)
    res_limit = run_test_sim(bars_15m, mode="limit_tp", tp_threshold=0.10)
    res_limit_5 = run_test_sim(bars_15m, mode="limit_tp", tp_threshold=0.05)

    print("\n--- RESULTS OVER 20 MAJOR SYMBOLS (15M DATA) ---")
    for r in [res_15m, res_1h, res_limit, res_limit_5]:
        print(f"Mode: {r['mode']:<12} | Net: ${r['net_profit']:>8.2f} | Cash: ${r['realized_cash']:>8.2f} | Float: ${r['floating_pnl']:>8.2f} | TPs: {r['total_tps']:>5} | Fees: ${r['fees_paid']:>6.2f}")
