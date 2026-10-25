import os
import io
import json
import zipfile
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd
import requests

DATA_DIR = Path("data")
KLINES_15M_DIR = DATA_DIR / "klines_15m"
KLINES_15M_DIR.mkdir(parents=True, exist_ok=True)

OUT_FILE = DATA_DIR / "cache_15m_all_crypto.npz"

# Load all 630 pure crypto active symbols
SYMBOLS = json.loads((DATA_DIR / "pure_crypto_active_symbols.json").read_text())
MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]

print(f"Targeting {len(SYMBOLS)} pure native crypto symbols across {len(MONTHS)} months (total {len(SYMBOLS)*len(MONTHS)} monthly files)...")

def process_symbol_month(task):
    sym, month = task
    local_path = KLINES_15M_DIR / f"{sym}-{month}.npz"
    if local_path.exists():
        try:
            d = np.load(local_path)
            return sym, d["times"], d["ohlc"]
        except Exception:
            pass

    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/15m/{sym}-15m-{month}.zip"
    try:
        r = requests.get(url, timeout=12)
        if r.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            df = pd.read_csv(z.open(z.namelist()[0])).iloc[:, :6]
            times = df.iloc[:, 0].astype(np.int64).values
            ohlc = df.iloc[:, 1:5].astype(np.float64).values
            np.savez_compressed(local_path, times=times, ohlc=ohlc)
            return sym, times, ohlc
    except Exception:
        pass
    return sym, None, None

def get_full_15m_matrix():
    if OUT_FILE.exists():
        print(f"Loading cached full dataset from {OUT_FILE}...")
        d = np.load(OUT_FILE)
        return d["bars"], list(d["symbols"]), list(d["timestamps"])

    tasks = [(s, m) for s in SYMBOLS for m in MONTHS]
    print(f"Downloading/loading {len(tasks)} monthly 15m chunks with 48 threads...")
    t0 = time.time()
    
    # Pre-populate from existing 100-symbol cache if possible
    # We download in parallel
    results = {}
    done_count = 0
    with ThreadPoolExecutor(max_workers=48) as ex:
        for sym, times, ohlc in ex.map(process_symbol_month, tasks):
            done_count += 1
            if done_count % 500 == 0:
                print(f"  Progress: {done_count}/{len(tasks)} tasks processed ({time.time()-t0:.1f}s)...")
            if times is not None and len(times) > 0:
                results.setdefault(sym, []).append((times, ohlc))

    print(f"Downloaded/cached in {time.time()-t0:.1f}s. Valid symbols with data: {len(results)}")

    # Build master timestamp index
    # We know standard 2026-01-01 00:00:00 to 2026-09-30 23:45:00 has exactly 26,208 15m bars
    # Let's derive timestamps from BTCUSDT or global union
    symbol_dfs = {}
    all_time_sets = []
    for sym, chunks in results.items():
        all_t = np.concatenate([c[0] for c in chunks])
        all_o = np.concatenate([c[1] for c in chunks], axis=0)
        # Deduplicate
        sort_idx = np.argsort(all_t)
        all_t = all_t[sort_idx]
        all_o = all_o[sort_idx]
        _, uniq_idx = np.unique(all_t, return_index=True)
        all_t = all_t[uniq_idx]
        all_o = all_o[uniq_idx]
        symbol_dfs[sym] = (all_t, all_o)
        if sym in ("BTCUSDT", "ETHUSDT"):
            all_time_sets.append(set(all_t))

    if all_time_sets:
        common_times = sorted(list(set.intersection(*all_time_sets)))
    else:
        common_times = sorted(list(set().union(*[set(v[0]) for v in symbol_dfs.values()])))

    num_bars = len(common_times)
    val_syms = sorted([s for s in SYMBOLS if s in symbol_dfs])
    print(f"Assembling 3D numpy matrix: {num_bars} bars x {len(val_syms)} symbols...")

    time_to_row = {t: i for i, t in enumerate(common_times)}
    bars = np.full((num_bars, len(val_syms), 4), np.nan, dtype=np.float64)

    for si, sym in enumerate(val_syms):
        s_times, s_ohlc = symbol_dfs[sym]
        row_indices = []
        data_indices = []
        for di, t in enumerate(s_times):
            if t in time_to_row:
                row_indices.append(time_to_row[t])
                data_indices.append(di)
        if row_indices:
            bars[row_indices, si, :] = s_ohlc[data_indices, :]

    # Forward fill NaNs for symbols that started trading earlier or had short maintenance
    for si in range(len(val_syms)):
        col = bars[:, si, 3]
        valid_idx = np.where(~np.isnan(col))[0]
        if len(valid_idx) > 0:
            first = valid_idx[0]
            for b in range(first + 1, num_bars):
                if np.isnan(bars[b, si, 3]):
                    bars[b, si, :] = bars[b - 1, si, 3]

    timestamps = [str(pd.to_datetime(t, unit="ms")) for t in common_times]
    np.savez_compressed(OUT_FILE, bars=bars, symbols=val_syms, timestamps=timestamps)
    print(f"Saved master matrix ({bars.nbytes / (1024*1024):.1f} MB) to {OUT_FILE}!")
    return bars, val_syms, timestamps

def run_simulation(bars, timestamps, mode="limit_instant", is_pure_t1=False, tp_aligned=True, tp_threshold=0.10):
    num_bars, num_symbols, _ = bars.shape
    ts_series = pd.to_datetime(timestamps)
    is_1h = (ts_series.minute == 45)
    is_daily_0800 = (ts_series.hour == 0) & (ts_series.minute == 0)

    initial_notional = 10.0
    maker_fee = 0.0002
    taker_fee = 0.0005
    t2_buy_drop = 0.50
    t2_notional = 20.0
    t3_buy_drop = 0.50
    t3_notional = 60.0

    t2_tp_batch1 = 1.20
    t2_tp_batch2 = 1.50
    t3_tp_batch1 = 1.20
    t3_tp_batch2 = 1.50
    t3_tp_batch3 = 2.00

    is_open = np.zeros(num_symbols, dtype=bool)
    tier = np.zeros(num_symbols, dtype=np.int32)
    qty = np.zeros(num_symbols, dtype=np.float64)
    size_notional = np.zeros(num_symbols, dtype=np.float64)
    avg_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_notional = np.zeros(num_symbols, dtype=np.float64)

    max_slots = 3
    sell_active = np.zeros((num_symbols, max_slots), dtype=bool)
    sell_price = np.zeros((num_symbols, max_slots), dtype=np.float64)
    sell_qty = np.zeros((num_symbols, max_slots), dtype=np.float64)
    sell_notional = np.zeros((num_symbols, max_slots), dtype=np.float64)

    # State tracking for batches in periodic review
    t2_batch1_done = np.zeros(num_symbols, dtype=bool)
    t3_batches_done = np.zeros(num_symbols, dtype=np.int32)
    t2_init_qty = np.zeros(num_symbols, dtype=np.float64)
    t3_init_qty = np.zeros(num_symbols, dtype=np.float64)

    total_realized_cash = 0.0
    total_fees_paid = 0.0
    total_tps = 0
    total_opens = 0

    has_limit_tp = mode.startswith("limit_")

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
        if not is_pure_t1:
            limit_buy_price[s] = p * (1.0 - t2_buy_drop)
            limit_buy_notional[s] = t2_notional
        else:
            limit_buy_price[s] = 0.0
            limit_buy_notional[s] = 0.0

        t2_batch1_done[s] = False
        t3_batches_done[s] = 0

        sell_active[s, :] = False
        if has_limit_tp:
            sell_active[s, 0] = True
            sell_price[s, 0] = p * (1.0 + tp_threshold)
            sell_qty[s, 0] = q
            sell_notional[s, 0] = initial_notional
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

            # DCA check
            if not is_pure_t1 and limit_buy_price[s] > 0 and l <= limit_buy_price[s]:
                lb_p = limit_buy_price[s]
                lb_n = limit_buy_notional[s]
                fee = lb_n * maker_fee
                total_fees_paid += fee
                add_q = lb_n / lb_p
                new_q = qty[s] + add_q
                new_n = size_notional[s] + lb_n
                new_avg = new_n / new_q
                tier[s] += 1
                curr_t = tier[s]
                qty[s] = new_q
                size_notional[s] = new_n
                avg_price[s] = new_avg

                if curr_t == 2:
                    limit_buy_price[s] = lb_p * (1.0 - t3_buy_drop)
                    limit_buy_notional[s] = t3_notional
                    t2_batch1_done[s] = False
                    t2_init_qty[s] = new_q
                else:
                    limit_buy_price[s] = 0.0
                    limit_buy_notional[s] = 0.0
                    t3_batches_done[s] = 0
                    t3_init_qty[s] = new_q

                sell_active[s, :] = False
                if has_limit_tp:
                    if curr_t == 2:
                        hq = new_q * 0.5
                        hn = new_n * 0.5
                        sell_active[s, 0] = True; sell_price[s, 0] = new_avg * t2_tp_batch1; sell_qty[s, 0] = hq; sell_notional[s, 0] = hn
                        sell_active[s, 1] = True; sell_price[s, 1] = new_avg * t2_tp_batch2; sell_qty[s, 1] = hq; sell_notional[s, 1] = hn
                    elif curr_t == 3:
                        tq = new_q / 3.0
                        tn = new_n / 3.0
                        sell_active[s, 0] = True; sell_price[s, 0] = new_avg * t3_tp_batch1; sell_qty[s, 0] = tq; sell_notional[s, 0] = tn
                        sell_active[s, 1] = True; sell_price[s, 1] = new_avg * t3_tp_batch2; sell_qty[s, 1] = tq; sell_notional[s, 1] = tn
                        sell_active[s, 2] = True; sell_price[s, 2] = new_avg * t3_tp_batch3; sell_qty[s, 2] = tq; sell_notional[s, 2] = tn

            # Limit TP check
            if has_limit_tp:
                for slot in range(max_slots):
                    if sell_active[s, slot] and h >= sell_price[s, slot]:
                        sp = sell_price[s, slot]
                        sq = sell_qty[s, slot]
                        sn = sell_notional[s, slot]
                        pnl = (sp - avg_price[s]) * sq
                        fee = (sp * sq) * maker_fee
                        total_realized_cash += (pnl - fee)
                        total_fees_paid += fee
                        total_tps += 1
                        qty[s] -= sq
                        size_notional[s] -= sn
                        sell_active[s, slot] = False

                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    if mode == "limit_instant":
                        cur_entry_p = sp
                        open_sym(s, cur_entry_p)
                        while True:
                            next_tp_p = cur_entry_p * (1.0 + tp_threshold)
                            if h >= next_tp_p:
                                pnl = (next_tp_p - cur_entry_p) * (initial_notional / cur_entry_p)
                                fee = initial_notional * maker_fee
                                total_realized_cash += (pnl - fee)
                                total_fees_paid += fee
                                total_tps += 1
                                cur_entry_p = next_tp_p
                                open_sym(s, cur_entry_p)
                            else:
                                break
                    elif mode == "limit_bar_close":
                        open_sym(s, c)

        # Periodic Review Check
        eval_now = False
        if mode == "15m_review":
            eval_now = True
        elif mode == "1h_review" and is_1h[b]:
            eval_now = True
        elif mode == "daily_0800_review" and is_daily_0800[b]:
            eval_now = True

        if eval_now:
            for s in np.where(is_open)[0]:
                c = c_bar[s]
                if np.isnan(c) or c <= 0: continue
                ret = (c - avg_price[s]) / avg_price[s]

                if is_pure_t1 or not tp_aligned:
                    if ret >= tp_threshold:
                        pnl = (c - avg_price[s]) * qty[s]
                        fee = (c * qty[s]) * taker_fee
                        total_realized_cash += (pnl - fee)
                        total_fees_paid += fee
                        total_tps += 1
                        is_open[s] = False
                        open_sym(s, c)
                else:
                    curr_t = tier[s]
                    if curr_t == 1:
                        if ret >= tp_threshold:
                            pnl = (c - avg_price[s]) * qty[s]
                            fee = (c * qty[s]) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 1
                            is_open[s] = False
                            open_sym(s, c)
                    elif curr_t == 2:
                        if ret >= 0.50:
                            pnl = (c - avg_price[s]) * qty[s]
                            fee = (c * qty[s]) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 1
                            is_open[s] = False
                            open_sym(s, c)
                        elif ret >= 0.20 and not t2_batch1_done[s]:
                            hq = t2_init_qty[s] * 0.5
                            pnl = (c - avg_price[s]) * hq
                            fee = (c * hq) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 1
                            qty[s] -= hq
                            t2_batch1_done[s] = True
                    elif curr_t == 3:
                        if ret >= 1.00:
                            pnl = (c - avg_price[s]) * qty[s]
                            fee = (c * qty[s]) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 1
                            is_open[s] = False
                            open_sym(s, c)
                        elif ret >= 0.50 and t3_batches_done[s] < 2:
                            needed = 2 - t3_batches_done[s]
                            close_q = (t3_init_qty[s] / 3.0) * needed
                            pnl = (c - avg_price[s]) * close_q
                            fee = (c * close_q) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += needed
                            qty[s] -= close_q
                            t3_batches_done[s] = 2
                        elif ret >= 0.20 and t3_batches_done[s] == 0:
                            close_q = t3_init_qty[s] / 3.0
                            pnl = (c - avg_price[s]) * close_q
                            fee = (c * close_q) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 1
                            qty[s] -= close_q
                            t3_batches_done[s] = 1

    floating = 0.0
    for s in np.where(is_open)[0]:
        floating += (last_p[s] - avg_price[s]) * qty[s]

    net_profit = total_realized_cash + floating

    return {
        "mode": mode,
        "is_pure_t1": is_pure_t1,
        "tp_aligned": tp_aligned,
        "net_profit": net_profit,
        "realized_cash": total_realized_cash,
        "floating_pnl": floating,
        "fees_paid": total_fees_paid,
        "total_tps": total_tps,
        "total_opens": total_opens
    }

if __name__ == "__main__":
    bars, symbols, timestamps = get_full_15m_matrix()
    print(f"\nRunning ALL CRYPTO ({len(symbols)} symbols) backtest over {bars.shape[0]} 15m bars...")

    print("\n" + "="*80)
    print(f"ALL NATIVE CRYPTO ({len(symbols)} SYMBOLS) - PURE T1 (NO DCA)")
    print("="*80)
    p_lim_inst = run_simulation(bars, timestamps, "limit_instant", is_pure_t1=True)
    p_lim_close = run_simulation(bars, timestamps, "limit_bar_close", is_pure_t1=True)
    p_15m = run_simulation(bars, timestamps, "15m_review", is_pure_t1=True)
    p_1h = run_simulation(bars, timestamps, "1h_review", is_pure_t1=True)
    p_daily = run_simulation(bars, timestamps, "daily_0800_review", is_pure_t1=True)

    pure_list = [
        ("限价单 (盘中秒开 10%)", p_lim_inst),
        ("限价单 (15m收盘开 10%)", p_lim_close),
        ("✨ 15分钟收益统计 (>=10% 即平且开)", p_15m),
        ("1小时收益统计 (>=10% 即平且开)", p_1h),
        ("每天早8点统计 (>=10% 即平且开)", p_daily),
    ]
    print(f"{'策略配置':<34} | {'总净利润 (Net)':>12} | {'已实现现金 (Cash)':>15} | {'未结浮亏':>10} | {'止盈次数':>8} | {'手续费':>8}")
    print("-" * 98)
    for name, r in pure_list:
        print(f"{name:<34} | ${r['net_profit']:>11.2f} | ${r['realized_cash']:>14.2f} | ${r['floating_pnl']:>9.2f} | {r['total_tps']:>8} | ${r['fees_paid']:>7.2f}")

    print("\n" + "="*80)
    print(f"ALL NATIVE CRYPTO ({len(symbols)} SYMBOLS) - FULL 3-TIER GRID (WITH DCA)")
    print("="*80)
    g_lim_inst = run_simulation(bars, timestamps, "limit_instant", is_pure_t1=False, tp_aligned=True)
    g_lim_close = run_simulation(bars, timestamps, "limit_bar_close", is_pure_t1=False, tp_aligned=True)
    g_15m_aligned = run_simulation(bars, timestamps, "15m_review", is_pure_t1=False, tp_aligned=True)
    g_1h_aligned = run_simulation(bars, timestamps, "1h_review", is_pure_t1=False, tp_aligned=True)
    g_daily_aligned = run_simulation(bars, timestamps, "daily_0800_review", is_pure_t1=False, tp_aligned=True)
    g_15m_old = run_simulation(bars, timestamps, "15m_review", is_pure_t1=False, tp_aligned=False)
    g_1h_old = run_simulation(bars, timestamps, "1h_review", is_pure_t1=False, tp_aligned=False)

    grid_list = [
        ("🏆 盘中限价单 (平完秒开)", g_lim_inst),
        ("⭐ 盘中限价单 (15m收盘开)", g_lim_close),
        ("✨ 15m统计 [规则公平对齐]", g_15m_aligned),
        ("📊 1h统计 [规则公平对齐]", g_1h_aligned),
        ("📅 每天早8点统计 [规则公平对齐]", g_daily_aligned),
        ("⚠️ 15m统计 [旧版一刀切10%]", g_15m_old),
        ("⚠️ 1h统计 [旧版一刀切10%]", g_1h_old),
    ]
    print(f"{'策略配置':<34} | {'总净利润 (Net)':>12} | {'已实现现金 (Cash)':>15} | {'未结浮亏':>10} | {'止盈次数':>8} | {'手续费':>8}")
    print("-" * 98)
    for name, r in grid_list:
        print(f"{name:<34} | ${r['net_profit']:>11.2f} | ${r['realized_cash']:>14.2f} | ${r['floating_pnl']:>9.2f} | {r['total_tps']:>8} | ${r['fees_paid']:>7.2f}")
