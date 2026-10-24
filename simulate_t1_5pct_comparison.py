"""
Comprehensive Simulation & Comparison:
Tier 1 Take-Profit Target = +5% under:
1. 隔 1 小时复开 (Next-bar Reopen)
2. 同小时收盘即开 (Same-bar Close Reopen)
3. 盘中平完秒开 (Intra-bar Instant Reopen)

And benchmarked against:
- T1 TP +10% (隔 1 小时 vs 秒开)
- T1 TP +20% (隔 1 小时 vs 秒开)

Under Unlimited Capital across all 821 symbols (Feb - Oct 2026).
"""

import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

DATA = Path("data")
RESULTS = DATA / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

bars_full = np.load(DATA / "bars_cache_full.npy")
info_full = json.loads((DATA / "bars_info_full.json").read_text())
symbols = info_full["symbols"]
fund_full = np.load(DATA / "funding_matrix_full.npy")
num_bars, num_symbols, _ = bars_full.shape

print(f"Loaded {num_bars} bars, {num_symbols} symbols.")

def run_simulation(instant_mode="next_bar", t1_tp_pct=0.05):
    t1_tp_ratio = 1.0 + t1_tp_pct
    initial_notional = 10.0
    leverage = 10.0
    maker_fee = 0.0002
    taker_fee = 0.0005
    t2_buy_drop = 0.50
    t2_notional = 20.0
    t2_tp_batch1 = 1.20
    t2_tp_batch2 = 1.50
    t3_buy_drop = 0.50
    t3_notional = 60.0
    t3_tp_batch1 = 1.20
    t3_tp_batch2 = 1.50
    t3_tp_batch3 = 2.00
    
    mv_arm_threshold = 10.0
    mv_trigger_target = 5.0
    recycled_t2_notional = 20.0
    recycled_t3_notional = 60.0
    max_recycled_cycles = 1

    is_open = np.zeros(num_symbols, dtype=bool)
    tier = np.zeros(num_symbols, dtype=np.int32)
    qty = np.zeros(num_symbols, dtype=np.float64)
    size_notional = np.zeros(num_symbols, dtype=np.float64)
    avg_price = np.zeros(num_symbols, dtype=np.float64)
    cycle = np.zeros(num_symbols, dtype=np.int32)
    armed_10u = np.zeros(num_symbols, dtype=bool)
    limit_buy_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_notional = np.zeros(num_symbols, dtype=np.float64)

    max_slots = 6
    sell_active = np.zeros((num_symbols, max_slots), dtype=bool)
    sell_price = np.zeros((num_symbols, max_slots), dtype=np.float64)
    sell_qty = np.zeros((num_symbols, max_slots), dtype=np.float64)
    sell_notional = np.zeros((num_symbols, max_slots), dtype=np.float64)

    last_exit_bar = np.full(num_symbols, -99999, dtype=np.int64)

    sym_realized_pnl = np.zeros(num_symbols, dtype=np.float64)
    sym_fees = np.zeros(num_symbols, dtype=np.float64)
    sym_funding = np.zeros(num_symbols, dtype=np.float64)
    sym_opens = np.zeros(num_symbols, dtype=np.int64)
    sym_tps = np.zeros(num_symbols, dtype=np.int64)
    sym_dca_t2 = np.zeros(num_symbols, dtype=np.int64)
    sym_dca_t3 = np.zeros(num_symbols, dtype=np.int64)
    sym_recycled = np.zeros(num_symbols, dtype=np.int64)

    same_bar_multi_tp = 0
    total_realized_cash = 0.0
    total_fees_paid = 0.0
    total_funding_paid = 0.0

    history_notional = []
    history_equity = []
    history_realized = []

    last_p = np.zeros(num_symbols, dtype=np.float64)

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
        limit_buy_price[s] = p * (1.0 - t2_buy_drop)
        limit_buy_notional[s] = t2_notional
        sell_active[s, :] = False
        sell_active[s, 0] = True
        sell_price[s, 0] = p * t1_tp_ratio
        sell_qty[s, 0] = q
        sell_notional[s, 0] = initial_notional
        armed_10u[s] = False
        cycle[s] = 0
        sym_opens[s] += 1

    # Bar 0: open all available symbols
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

            # Recycled devaluation trigger check
            if tier[s] == 3 and cycle[s] < max_recycled_cycles:
                mv_low = l * qty[s]
                if not armed_10u[s] and mv_low <= mv_arm_threshold:
                    armed_10u[s] = True
                    limit_buy_price[s] = mv_trigger_target / qty[s]
                    limit_buy_notional[s] = recycled_t2_notional

            # Sub-candle order matching
            sub_extremes = [l, h] if c >= o else [h, l]
            for pe in sub_extremes:
                if not is_open[s]: break

                # 1. Limit buy (DCA) check
                lb_p = limit_buy_price[s]
                if lb_p > 0 and l <= lb_p:
                    lb_n = limit_buy_notional[s]
                    fee = lb_n * maker_fee
                    total_fees_paid += fee
                    sym_fees[s] += fee
                    added_q = lb_n / lb_p
                    new_q = qty[s] + added_q
                    new_n = size_notional[s] + lb_n
                    new_avg = new_n / new_q

                    if tier[s] < 3:
                        tier[s] += 1
                        curr_t = tier[s]
                        qty[s] = new_q
                        size_notional[s] = new_n
                        avg_price[s] = new_avg
                        sell_active[s, :] = False

                        if curr_t == 2:
                            sym_dca_t2[s] += 1
                            hq = new_q * 0.5
                            hn = new_n * 0.5
                            sell_active[s, 0] = True; sell_price[s, 0] = new_avg * t2_tp_batch1; sell_qty[s, 0] = hq; sell_notional[s, 0] = hn
                            sell_active[s, 1] = True; sell_price[s, 1] = new_avg * t2_tp_batch2; sell_qty[s, 1] = hq; sell_notional[s, 1] = hn
                            limit_buy_price[s] = lb_p * (1.0 - t3_buy_drop)
                            limit_buy_notional[s] = t3_notional
                        elif curr_t == 3:
                            sym_dca_t3[s] += 1
                            tq = new_q / 3.0
                            tn = new_n / 3.0
                            sell_active[s, 0] = True; sell_price[s, 0] = new_avg * t3_tp_batch1; sell_qty[s, 0] = tq; sell_notional[s, 0] = tn
                            sell_active[s, 1] = True; sell_price[s, 1] = new_avg * t3_tp_batch2; sell_qty[s, 1] = tq; sell_notional[s, 1] = tn
                            sell_active[s, 2] = True; sell_price[s, 2] = new_avg * t3_tp_batch3; sell_qty[s, 2] = tq; sell_notional[s, 2] = tn
                            limit_buy_price[s] = 0.0
                            limit_buy_notional[s] = 0.0
                    else:
                        cycle[s] += 1
                        sym_recycled[s] += 1
                        qty[s] = new_q
                        size_notional[s] = new_n
                        local_eff_p = (mv_trigger_target + lb_n) / new_q
                        avg_price[s] = new_avg
                        sell_active[s, :] = False
                        hq = new_q * 0.5
                        hn = new_n * 0.5
                        sell_active[s, 0] = True; sell_price[s, 0] = local_eff_p * t2_tp_batch1; sell_qty[s, 0] = hq; sell_notional[s, 0] = hn
                        sell_active[s, 1] = True; sell_price[s, 1] = local_eff_p * t2_tp_batch2; sell_qty[s, 1] = hq; sell_notional[s, 1] = hn
                        if cycle[s] == 1:
                            limit_buy_price[s] = lb_p * 0.50
                            limit_buy_notional[s] = recycled_t3_notional
                        else:
                            limit_buy_price[s] = 0.0
                            limit_buy_notional[s] = 0.0
                            armed_10u[s] = False

                # 2. Limit sell (TP) check
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

                # 3. Check if fully closed
                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    armed_10u[s] = False
                    cycle[s] = 0
                    last_exit_bar[s] = b_idx

                    # --- If INSTANT INTRA-BAR REOPEN is enabled ---
                    if instant_mode == "intra_bar_instant":
                        cur_entry_p = sp
                        open_symbol(s, cur_entry_p, b_idx)
                        while True:
                            next_tp_p = cur_entry_p * t1_tp_ratio
                            if h >= next_tp_p:
                                same_bar_multi_tp += 1
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
                    break

        # Hourly funding calculation
        op_mask = is_open
        if np.any(op_mask):
            cur_p = last_p[op_mask]
            cur_q = qty[op_mask]
            cur_notional = cur_p * cur_q
            tot_notional = float(np.sum(cur_notional))
            tot_upnl = float(np.sum((cur_p - avg_price[op_mask]) * cur_q))
            fund_rates = fund_full[b_idx, op_mask]
            charge_vec = fund_rates * cur_notional
            charge = float(np.sum(charge_vec))
            total_funding_paid += charge
            sym_funding[op_mask] += charge_vec
        else:
            tot_notional = 0.0
            tot_upnl = 0.0

        net_realized = total_realized_cash - total_funding_paid
        total_pnl = net_realized + tot_upnl

        history_notional.append(tot_notional)
        history_equity.append(total_pnl)
        history_realized.append(net_realized)

        # End of bar scheduled reopen for idle symbols
        idle = np.where(~is_open)[0]
        for s in idle:
            can_reopen = False
            if sym_opens[s] == 0:
                can_reopen = True
            elif instant_mode == "next_bar" and (b_idx - last_exit_bar[s] >= 1):
                can_reopen = True
            elif instant_mode == "same_bar_close":
                can_reopen = True

            if can_reopen:
                p_now = bars_full[b_idx, s, 3]
                if not np.isnan(p_now) and p_now > 0:
                    open_symbol(s, p_now, b_idx)

    # End of simulation summary
    op_mask = is_open
    cur_p = last_p[op_mask]
    cur_q = qty[op_mask]
    final_floating_pnl = float(np.sum((cur_p - avg_price[op_mask]) * cur_q)) if np.any(op_mask) else 0.0
    final_open_notional = float(np.sum(cur_p * cur_q)) if np.any(op_mask) else 0.0

    peak_notional = float(np.max(history_notional))
    peak_margin = peak_notional / leverage
    net_realized_final = total_realized_cash - total_funding_paid
    total_net_profit = net_realized_final + final_floating_pnl

    eq = np.array(history_equity)
    running_max = np.maximum.accumulate(eq)
    dd_dollar = eq - running_max
    max_dd_dollar = float(np.min(dd_dollar))

    sym_floating = np.zeros(num_symbols)
    sym_floating[op_mask] = (cur_p - avg_price[op_mask]) * cur_q
    sym_net_total = sym_realized_pnl - sym_funding + sym_floating
    winners = int((sym_net_total > 0).sum())
    losers = int((sym_net_total < 0).sum())

    return {
        "mode": instant_mode,
        "t1_pct": t1_tp_pct,
        "total_net_profit": total_net_profit,
        "total_realized_cash": total_realized_cash,
        "final_floating_pnl": final_floating_pnl,
        "total_funding_paid": total_funding_paid,
        "total_fees_paid": total_fees_paid,
        "peak_margin": peak_margin,
        "peak_notional": peak_notional,
        "max_dd_dollar": max_dd_dollar,
        "total_opens": int(np.sum(sym_opens)),
        "total_tps": int(np.sum(sym_tps)),
        "same_bar_multi_tp": same_bar_multi_tp,
        "dca_t2": int(np.sum(sym_dca_t2)),
        "dca_t3": int(np.sum(sym_dca_t3)),
        "winners": winners,
        "losers": losers,
        "win_rate": winners / max(1, winners + losers) * 100,
        "history_equity": history_equity,
        "sym_net_total": sym_net_total
    }

if __name__ == "__main__":
    print("Testing 5% TP vs other targets with Instant vs Delay...")
    experiments = [
        ("Tier 1 TP +5% (平完秒开) [本次重点]", "intra_bar_instant", 0.05),
        ("Tier 1 TP +5% (隔1小时复开)", "next_bar", 0.05),
        ("Tier 1 TP +5% (同小时收盘即开)", "same_bar_close", 0.05),
        ("Tier 1 TP +10% (平完秒开)", "intra_bar_instant", 0.10),
        ("Tier 1 TP +10% (隔1小时复开)", "next_bar", 0.10),
        ("Tier 1 TP +20% (平完秒开)", "intra_bar_instant", 0.20),
        ("Tier 1 TP +20% (隔1小时复开) [基准]", "next_bar", 0.20),
    ]

    results = []
    for label, m, pct in experiments:
        print(f"\n--- Running {label} ---")
        r = run_simulation(m, pct)
        print(f"  总净利润 (Net Profit):       ${r['total_net_profit']:+,.2f}")
        print(f"  已实现纯现金 (Realized Cash):  ${r['total_realized_cash']:+,.2f}")
        print(f"  期末浮动盈亏 (Floating PnL):   ${r['final_floating_pnl']:+,.2f}")
        print(f"  总手续费 (Total Fees):        ${r['total_fees_paid']:,.2f}")
        print(f"  峰值所需保证金 (Peak Margin):  ${r['peak_margin']:,.2f}")
        print(f"  最大回撤金额 (Max DD Dollar): ${r['max_dd_dollar']:,.2f}")
        print(f"  开仓次数: {r['total_opens']} | 止盈次数: {r['total_tps']} (同K连环止盈: {r['same_bar_multi_tp']} 次)")
        print(f"  标的胜率: {r['winners']}/{r['losers']} ({r['win_rate']:.1f}%)")
        results.append((label, r))

    summary_out = []
    for label, r in results:
        summary_out.append({
            "label": label,
            "mode": r["mode"],
            "t1_pct": r["t1_pct"],
            "net_profit": r["total_net_profit"],
            "realized_cash": r["total_realized_cash"],
            "floating_pnl": r["final_floating_pnl"],
            "fees_paid": r["total_fees_paid"],
            "funding_paid": r["total_funding_paid"],
            "peak_margin": r["peak_margin"],
            "max_dd_dollar": r["max_dd_dollar"],
            "total_opens": r["total_opens"],
            "total_tps": r["total_tps"],
            "same_bar_multi_tp": r["same_bar_multi_tp"],
            "win_rate": r["win_rate"]
        })

    (RESULTS / "t1_5pct_comparison_summary.json").write_text(json.dumps(summary_out, indent=2, ensure_ascii=False))

    # Plotting
    plt.figure(figsize=(15, 9))
    styles = {
        "Tier 1 TP +5% (平完秒开) [本次重点]": ("#d62728", "-", 2.5),
        "Tier 1 TP +5% (隔1小时复开)": ("#ff7f0e", "--", 2.0),
        "Tier 1 TP +5% (同小时收盘即开)": ("#ffbb78", ":", 1.8),
        "Tier 1 TP +10% (平完秒开)": ("#2ca02c", "-", 2.0),
        "Tier 1 TP +10% (隔1小时复开)": ("#98df8a", "--", 2.0),
        "Tier 1 TP +20% (平完秒开)": ("#1f77b4", "-", 1.8),
        "Tier 1 TP +20% (隔1小时复开) [基准]": ("#aec7e8", "--", 1.8),
    }

    for label, r in results:
        c, ls, lw = styles.get(label, ("black", "-", 1.5))
        plt.plot(r["history_equity"], label=f"{label}: ${r['total_net_profit']:+,.0f}", color=c, linestyle=ls, linewidth=lw)

    plt.title("Tier 1 Take-Profit Target +5% (Instant vs Delayed Reopen) Across 821 Symbols", fontsize=14, fontweight="bold")
    plt.xlabel("Hours (9-month Backtest: Feb - Oct 2026)", fontsize=11)
    plt.ylabel("Net Cumulative PnL (USDT)", fontsize=11)
    plt.grid(True, alpha=0.3)
    plt.legend(loc="upper left", framealpha=0.9, fontsize=10)
    plt.tight_layout()
    plt.savefig(RESULTS / "comparison_t1_5pct.png", dpi=150)
    plt.close()
    print("\nSaved plot to data/results/comparison_t1_5pct.png")
