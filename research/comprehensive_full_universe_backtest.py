import json
from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path("data")
RESULTS = DATA / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

bars_full = np.load(DATA / "bars_cache_full.npy")
info_full = json.loads((DATA / "bars_info_full.json").read_text())
symbols = info_full["symbols"]
fund_full = np.load(DATA / "funding_matrix_full.npy")
hours = np.array(info_full["hours"], dtype=np.int32)
num_bars, num_symbols, _ = bars_full.shape

print(f"Loaded {num_bars} bars, {num_symbols} symbols.")

def run_simulation(strategy_mode="hourly_close_review", tp_threshold=0.10, is_pure_t1=False, tp_aligned=True):
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

    sym_realized_pnl = np.zeros(num_symbols, dtype=np.float64)
    sym_fees = np.zeros(num_symbols, dtype=np.float64)
    sym_funding = np.zeros(num_symbols, dtype=np.float64)
    sym_opens = np.zeros(num_symbols, dtype=np.int64)
    sym_tps = np.zeros(num_symbols, dtype=np.int64)

    total_realized_cash = 0.0
    total_fees_paid = 0.0
    total_funding_paid = 0.0
    last_p = np.zeros(num_symbols, dtype=np.float64)

    has_limit_tp = strategy_mode.startswith("limit_")

    def open_symbol(s, p, b_idx):
        nonlocal total_fees_paid
        fee = initial_notional * taker_fee
        total_fees_paid += fee
        sym_fees[s] += fee
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
        sym_opens[s] += 1

    for s in range(num_symbols):
        p0 = bars_full[0, s, 0]
        if not np.isnan(p0) and p0 > 0:
            last_p[s] = p0
            open_symbol(s, p0, 0)

    for b_idx in range(num_bars):
        c_bar = bars_full[b_idx, :, 3]
        valid_c = ~np.isnan(c_bar)
        last_p[valid_c] = c_bar[valid_c]
        o_bar = bars_full[b_idx, :, 0]
        h_bar = bars_full[b_idx, :, 1]
        l_bar = bars_full[b_idx, :, 2]

        open_indices = np.where(is_open)[0]
        for s in open_indices:
            o = o_bar[s]
            if np.isnan(o) or o <= 0: continue
            h, l, c = h_bar[s], l_bar[s], c_bar[s]

            # 1. Limit buy (DCA) check
            if not is_pure_t1 and limit_buy_price[s] > 0 and l <= limit_buy_price[s]:
                lb_p = limit_buy_price[s]
                lb_n = limit_buy_notional[s]
                fee = lb_n * maker_fee
                total_fees_paid += fee
                sym_fees[s] += fee
                added_q = lb_n / lb_p
                new_q = qty[s] + added_q
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

            # 2. Limit sell (TP) check
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
                        sym_realized_pnl[s] += (pnl - fee)
                        sym_fees[s] += fee
                        sym_tps[s] += 1
                        qty[s] -= sq
                        size_notional[s] -= sn
                        sell_active[s, slot] = False

                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0

                    if strategy_mode == "limit_instant":
                        cur_entry_p = sp
                        open_symbol(s, cur_entry_p, b_idx)
                        while True:
                            next_tp_p = cur_entry_p * (1.0 + tp_threshold)
                            if h >= next_tp_p:
                                pnl = (next_tp_p - cur_entry_p) * (initial_notional / cur_entry_p)
                                fee = initial_notional * maker_fee
                                total_realized_cash += (pnl - fee)
                                total_fees_paid += fee
                                sym_realized_pnl[s] += (pnl - fee)
                                sym_fees[s] += fee
                                sym_tps[s] += 1
                                cur_entry_p = next_tp_p
                                open_symbol(s, cur_entry_p, b_idx)
                            else:
                                break
                    elif strategy_mode == "limit_same_bar_close":
                        open_symbol(s, c, b_idx)

        # Periodic Review Check
        eval_now = False
        if strategy_mode == "hourly_close_review":
            eval_now = True
        elif strategy_mode == "daily_0800_review" and hours[b_idx] == 0:
            eval_now = True

        if eval_now:
            for s in np.where(is_open)[0]:
                cp = c_bar[s]
                if np.isnan(cp) or cp <= 0: continue
                ret = (cp - avg_price[s]) / avg_price[s]
                
                if is_pure_t1 or not tp_aligned:
                    if ret >= tp_threshold:
                        pnl = (cp - avg_price[s]) * qty[s]
                        fee = (cp * qty[s]) * taker_fee
                        total_realized_cash += (pnl - fee)
                        total_fees_paid += fee
                        sym_realized_pnl[s] += (pnl - fee)
                        sym_fees[s] += fee
                        sym_tps[s] += 1
                        is_open[s] = False
                        open_symbol(s, cp, b_idx)
                else:
                    curr_t = tier[s]
                    if curr_t == 1:
                        if ret >= tp_threshold:
                            pnl = (cp - avg_price[s]) * qty[s]
                            fee = (cp * qty[s]) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            sym_realized_pnl[s] += (pnl - fee)
                            sym_fees[s] += fee
                            sym_tps[s] += 1
                            is_open[s] = False
                            open_symbol(s, cp, b_idx)
                    elif curr_t == 2:
                        if ret >= 0.50:
                            pnl = (cp - avg_price[s]) * qty[s]
                            fee = (cp * qty[s]) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            sym_realized_pnl[s] += (pnl - fee)
                            sym_fees[s] += fee
                            sym_tps[s] += 2
                            is_open[s] = False
                            open_symbol(s, cp, b_idx)
                        elif ret >= 0.20 and qty[s] > (size_notional[s] * 0.5 / avg_price[s] + 1e-6):
                            hq = qty[s] * 0.5
                            hn = size_notional[s] * 0.5
                            pnl = (cp - avg_price[s]) * hq
                            fee = (cp * hq) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            sym_realized_pnl[s] += (pnl - fee)
                            sym_fees[s] += fee
                            sym_tps[s] += 1
                            qty[s] -= hq
                            size_notional[s] -= hn
                    elif curr_t == 3:
                        if ret >= 1.00:
                            pnl = (cp - avg_price[s]) * qty[s]
                            fee = (cp * qty[s]) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            sym_realized_pnl[s] += (pnl - fee)
                            sym_fees[s] += fee
                            sym_tps[s] += 3
                            is_open[s] = False
                            open_symbol(s, cp, b_idx)
                        elif ret >= 0.50 and qty[s] > (size_notional[s] * (1/3) / avg_price[s] + 1e-6):
                            close_q = qty[s] * 0.5
                            close_n = size_notional[s] * 0.5
                            pnl = (cp - avg_price[s]) * close_q
                            fee = (cp * close_q) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            sym_realized_pnl[s] += (pnl - fee)
                            sym_fees[s] += fee
                            sym_tps[s] += 2
                            qty[s] -= close_q
                            size_notional[s] -= close_n
                        elif ret >= 0.20 and qty[s] > (size_notional[s] * (2/3) / avg_price[s] + 1e-6):
                            close_q = qty[s] / 3.0
                            close_n = size_notional[s] / 3.0
                            pnl = (cp - avg_price[s]) * close_q
                            fee = (cp * close_q) * taker_fee
                            total_realized_cash += (pnl - fee)
                            total_fees_paid += fee
                            sym_realized_pnl[s] += (pnl - fee)
                            sym_fees[s] += fee
                            sym_tps[s] += 1
                            qty[s] -= close_q
                            size_notional[s] -= close_n

        # Funding
        op_mask = is_open
        if np.any(op_mask):
            cur_p = last_p[op_mask]
            cur_q = qty[op_mask]
            cur_notional = cur_p * cur_q
            fund_rates = fund_full[b_idx, op_mask]
            charge_vec = fund_rates * cur_notional
            charge = float(np.sum(charge_vec))
            total_funding_paid += charge
            sym_funding[op_mask] += charge_vec

    # Final summary
    op_mask = is_open
    cur_p = last_p[op_mask]
    cur_q = qty[op_mask]
    final_floating_pnl = float(np.sum((cur_p - avg_price[op_mask]) * cur_q))
    net_realized = total_realized_cash - total_funding_paid
    total_net_profit = net_realized + final_floating_pnl

    return {
        "strategy": strategy_mode,
        "is_pure_t1": is_pure_t1,
        "tp_aligned": tp_aligned,
        "net_profit": total_net_profit,
        "realized_cash": total_realized_cash,
        "floating_pnl": final_floating_pnl,
        "funding_paid": total_funding_paid,
        "fees_paid": total_fees_paid,
        "total_opens": int(np.sum(sym_opens)),
        "total_tps": int(np.sum(sym_tps)),
    }

if __name__ == "__main__":
    print("\n" + "="*80)
    print("FULL UNIVERSE (821 SYMBOLS) - PURE T1 (NO DCA)")
    print("="*80)
    p_lim_inst = run_simulation("limit_instant", is_pure_t1=True)
    p_lim_close = run_simulation("limit_same_bar_close", is_pure_t1=True)
    p_hourly = run_simulation("hourly_close_review", is_pure_t1=True)
    p_daily = run_simulation("daily_0800_review", is_pure_t1=True)

    pure_list = [
        ("限价单 (盘中秒开 10%)", p_lim_inst),
        ("限价单 (收盘即开 10%)", p_lim_close),
        ("每小时统计 (>=10% 即平且开)", p_hourly),
        ("每天早8点统计 (>=10% 即平且开)", p_daily),
    ]
    print(f"{'策略配置':<32} | {'总净利润 (Net)':>12} | {'已实现现金 (Cash)':>15} | {'未结浮亏':>10} | {'止盈次数':>8} | {'手续费':>8}")
    print("-" * 95)
    for name, r in pure_list:
        print(f"{name:<32} | ${r['net_profit']:>11.2f} | ${r['realized_cash']:>14.2f} | ${r['floating_pnl']:>9.2f} | {r['total_tps']:>8} | ${r['fees_paid']:>7.2f}")

    print("\n" + "="*80)
    print("FULL UNIVERSE (821 SYMBOLS) - FULL 3-TIER GRID (WITH DCA)")
    print("="*80)
    g_lim_inst = run_simulation("limit_instant", is_pure_t1=False, tp_aligned=True)
    g_lim_close = run_simulation("limit_same_bar_close", is_pure_t1=False, tp_aligned=True)
    g_hourly_aligned = run_simulation("hourly_close_review", is_pure_t1=False, tp_aligned=True)
    g_daily_aligned = run_simulation("daily_0800_review", is_pure_t1=False, tp_aligned=True)
    g_hourly_old = run_simulation("hourly_close_review", is_pure_t1=False, tp_aligned=False)
    g_daily_old = run_simulation("daily_0800_review", is_pure_t1=False, tp_aligned=False)

    grid_list = [
        ("🏆 盘中限价单 (平完秒开)", g_lim_inst),
        ("⭐ 盘中限价单 (收盘即开)", g_lim_close),
        ("✨ 每小时统计 [规则公平对齐]", g_hourly_aligned),
        ("📅 每天早8点统计 [规则公平对齐]", g_daily_aligned),
        ("⚠️ 每小时统计 [旧版一刀切10%]", g_hourly_old),
        ("⚠️ 每天早8点统计 [旧版一刀切10%]", g_daily_old),
    ]
    print(f"{'策略配置':<32} | {'总净利润 (Net)':>12} | {'已实现现金 (Cash)':>15} | {'未结浮亏':>10} | {'止盈次数':>8} | {'手续费':>8}")
    print("-" * 95)
    for name, r in grid_list:
        print(f"{name:<32} | ${r['net_profit']:>11.2f} | ${r['realized_cash']:>14.2f} | ${r['floating_pnl']:>9.2f} | {r['total_tps']:>8} | ${r['fees_paid']:>7.2f}")
