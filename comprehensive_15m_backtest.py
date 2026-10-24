import numpy as np
import pandas as pd
from pathlib import Path
import json

DATA_DIR = Path("data")
FILE_15M = DATA_DIR / "cache_15m_40symbols.npz"

data = np.load(FILE_15M)
bars = data["bars"]
symbols = list(data["symbols"])
timestamps = list(data["timestamps"])
num_bars, num_symbols, _ = bars.shape

print(f"Loaded 15m dataset: {num_bars} bars, {num_symbols} symbols.")

# Parse timestamps to determine 1h and daily 08:00 (UTC 00:00)
# Timestamps format is 'YYYY-MM-DD HH:MM:SS'
ts_series = pd.to_datetime(timestamps)
is_1h = (ts_series.minute == 45)  # 15m bar ending at :00 (bar open at :45)
is_daily_0800 = (ts_series.hour == 0) & (ts_series.minute == 0) # UTC 00:00 is Beijing 08:00

def run_15m_sim(mode="limit_instant", is_pure_t1=False, tp_aligned=True, tp_threshold=0.10):
    """
    mode:
      - 'limit_instant': Intraday limit TP + instant reopen
      - 'limit_bar_close': Intraday limit TP + reopen at end of 15m bar
      - '15m_review': Periodic check every 15m bar close
      - '1h_review': Periodic check every 1 hour (bar minute == 45)
      - 'daily_0800_review': Periodic check every day at Beijing 08:00
    """
    initial_notional = 10.0
    maker_fee = 0.0002
    taker_fee = 0.0005
    t2_buy_drop = 0.50
    t2_notional = 20.0
    t3_buy_drop = 0.50
    t3_notional = 60.0

    # Grid TP batches
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

            # Limit DCA check
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
                else:
                    limit_buy_price[s] = 0.0
                    limit_buy_notional[s] = 0.0

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

            # Intraday Limit Sell check
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
                        # Instant intra-bar reopen & loop
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
                    # Old / Simple mode: if ret >= tp_threshold: close all and reopen T1
                    if ret >= tp_threshold:
                        pnl = (c - avg_price[s]) * qty[s]
                        fee = (c * qty[s]) * taker_fee
                        total_realized_cash += (pnl - fee)
                        total_fees_paid += fee
                        total_tps += 1
                        is_open[s] = False
                        open_sym(s, c)
                else:
                    # Aligned mode: T1 >= 10%, T2 >= 20% / 50%, T3 >= 20% / 50% / 100%
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
                        # Batch 1 (+20%), Batch 2 (+50%)
                        if ret >= 0.50:
                            # Both batches filled
                            pnl = (c - avg_price[s]) * qty[s]
                            fee = (c * qty[s]) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 2
                            is_open[s] = False
                            open_sym(s, c)
                        elif ret >= 0.20 and qty[s] > (size_notional[s] * 0.5 / avg_price[s] + 1e-6):
                            # Partial batch 1 close
                            hq = qty[s] * 0.5
                            hn = size_notional[s] * 0.5
                            pnl = (c - avg_price[s]) * hq
                            fee = (c * hq) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 1
                            qty[s] -= hq
                            size_notional[s] -= hn
                    elif curr_t == 3:
                        if ret >= 1.00:
                            pnl = (c - avg_price[s]) * qty[s]
                            fee = (c * qty[s]) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 3
                            is_open[s] = False
                            open_sym(s, c)
                        elif ret >= 0.50 and qty[s] > (size_notional[s] * (1/3) / avg_price[s] + 1e-6):
                            close_q = qty[s] * 0.5
                            close_n = size_notional[s] * 0.5
                            pnl = (c - avg_price[s]) * close_q
                            fee = (c * close_q) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 2
                            qty[s] -= close_q
                            size_notional[s] -= close_n
                        elif ret >= 0.20 and qty[s] > (size_notional[s] * (2/3) / avg_price[s] + 1e-6):
                            close_q = qty[s] / 3.0
                            close_n = size_notional[s] / 3.0
                            pnl = (c - avg_price[s]) * close_q
                            fee = (c * close_q) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            total_tps += 1
                            qty[s] -= close_q
                            size_notional[s] -= close_n

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
    print("\n" + "="*80)
    print("PART 1: 40 SYMBOLS (20 MAJORS + 20 VOLATILE ALTCOINS) - PURE T1 (NO DCA)")
    print("="*80)
    res_p_lim_inst = run_15m_sim("limit_instant", is_pure_t1=True)
    res_p_lim_close = run_15m_sim("limit_bar_close", is_pure_t1=True)
    res_p_15m = run_15m_sim("15m_review", is_pure_t1=True)
    res_p_1h = run_15m_sim("1h_review", is_pure_t1=True)
    res_p_daily = run_15m_sim("daily_0800_review", is_pure_t1=True)

    pure_list = [
        ("限价单 (盘中秒开 10%)", res_p_lim_inst),
        ("限价单 (15m收盘开 10%)", res_p_lim_close),
        ("15分钟收益统计 (>=10% 即平且开)", res_p_15m),
        ("1小时收益统计 (>=10% 即平且开)", res_p_1h),
        ("每天早8点统计 (>=10% 即平且开)", res_p_daily),
    ]
    print(f"{'策略配置':<32} | {'总净利润 (Net)':>12} | {'已实现现金 (Cash)':>15} | {'未结浮亏':>10} | {'止盈次数':>8} | {'手续费':>8}")
    print("-" * 95)
    for name, r in pure_list:
        print(f"{name:<32} | ${r['net_profit']:>11.2f} | ${r['realized_cash']:>14.2f} | ${r['floating_pnl']:>9.2f} | {r['total_tps']:>8} | ${r['fees_paid']:>7.2f}")

    print("\n" + "="*80)
    print("PART 2: 40 SYMBOLS (20 MAJORS + 20 VOLATILE ALTCOINS) - FULL 3-TIER GRID (WITH DCA)")
    print("="*80)
    res_g_lim_inst = run_15m_sim("limit_instant", is_pure_t1=False, tp_aligned=True)
    res_g_lim_close = run_15m_sim("limit_bar_close", is_pure_t1=False, tp_aligned=True)
    res_g_15m_aligned = run_15m_sim("15m_review", is_pure_t1=False, tp_aligned=True)
    res_g_1h_aligned = run_15m_sim("1h_review", is_pure_t1=False, tp_aligned=True)
    res_g_daily_aligned = run_15m_sim("daily_0800_review", is_pure_t1=False, tp_aligned=True)
    res_g_15m_old = run_15m_sim("15m_review", is_pure_t1=False, tp_aligned=False)
    res_g_1h_old = run_15m_sim("1h_review", is_pure_t1=False, tp_aligned=False)

    grid_list = [
        ("🏆 盘中限价单 (平完秒开)", res_g_lim_inst),
        ("⭐ 盘中限价单 (15m收盘开)", res_g_lim_close),
        ("✨ 15m统计 [规则公平对齐]", res_g_15m_aligned),
        ("📊 1h统计 [规则公平对齐]", res_g_1h_aligned),
        ("📅 每天早8点统计 [规则公平对齐]", res_g_daily_aligned),
        ("⚠️ 15m统计 [旧版一刀切10%]", res_g_15m_old),
        ("⚠️ 1h统计 [旧版一刀切10%]", res_g_1h_old),
    ]
    print(f"{'策略配置':<32} | {'总净利润 (Net)':>12} | {'已实现现金 (Cash)':>15} | {'未结浮亏':>10} | {'止盈次数':>8} | {'手续费':>8}")
    print("-" * 95)
    for name, r in grid_list:
        print(f"{name:<32} | ${r['net_profit']:>11.2f} | ${r['realized_cash']:>14.2f} | ${r['floating_pnl']:>9.2f} | {r['total_tps']:>8} | ${r['fees_paid']:>7.2f}")
