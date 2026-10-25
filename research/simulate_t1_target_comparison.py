"""
Simulate and Compare Tier 1 Take-Profit Targets: +10% vs +20% (and +5%, +15%)
Under Unlimited Capital across all 821 symbols.

User question: "那如果tier1的目标从20%改成10%呢"

We test both:
- Hourly Reopen (隔1小时复开)
- Instant Reopen (盘中平完秒开)
Across T1 TP targets: +5%, +10%, +15%, +20%
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
hours = np.array(info_full["hours"], dtype=np.int32)
num_bars, num_symbols, _ = bars_full.shape

print(f"Loaded {num_bars} bars, {num_symbols} symbols.")

def run_t1_experiment(t1_tp_pct=0.10, instant=False):
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

    total_realized_cash = 0.0
    total_fees_paid = 0.0
    total_funding_paid = 0.0

    history_notional = []
    history_equity = []

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

            if tier[s] == 3 and cycle[s] < max_recycled_cycles:
                mv_low = l * qty[s]
                if not armed_10u[s] and mv_low <= mv_arm_threshold:
                    armed_10u[s] = True
                    limit_buy_price[s] = mv_trigger_target / qty[s]
                    limit_buy_notional[s] = recycled_t2_notional

            sub_extremes = [l, h] if c >= o else [h, l]
            for pe in sub_extremes:
                if not is_open[s]: break

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
                    armed_10u[s] = False
                    cycle[s] = 0
                    last_exit_bar[s] = b_idx

                    if instant:
                        cur_entry_p = sp
                        open_symbol(s, cur_entry_p, b_idx)
                        while True:
                            next_tp_p = cur_entry_p * t1_tp_ratio
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
                    break

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

        if not instant:
            idle = np.where(~is_open)[0]
            for s in idle:
                if b_idx - last_exit_bar[s] >= 1:
                    p_now = bars_full[b_idx, s, 3]
                    if not np.isnan(p_now) and p_now > 0:
                        open_symbol(s, p_now, b_idx)

    op_mask = is_open
    cur_p = last_p[op_mask]
    cur_q = qty[op_mask]
    final_floating_pnl = float(np.sum((cur_p - avg_price[op_mask]) * cur_q)) if np.any(op_mask) else 0.0

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
        "t1_pct": t1_tp_pct,
        "instant": instant,
        "net_profit": total_net_profit,
        "realized_cash": total_realized_cash,
        "floating_pnl": final_floating_pnl,
        "total_funding_paid": total_funding_paid,
        "total_fees_paid": total_fees_paid,
        "peak_margin": peak_margin,
        "peak_notional": peak_notional,
        "max_dd_dollar": max_dd_dollar,
        "total_opens": int(np.sum(sym_opens)),
        "total_tps": int(np.sum(sym_tps)),
        "dca_t2": int(np.sum(sym_dca_t2)),
        "dca_t3": int(np.sum(sym_dca_t3)),
        "winners": winners,
        "losers": losers,
        "win_rate": winners / max(1, winners + losers) * 100,
        "history_equity": history_equity
    }

print("Testing Tier 1 TP Targets: 5%, 10%, 15%, 20%...")

targets = [
    ("Tier 1 TP +5% (隔1小时复开)", 0.05, False),
    ("Tier 1 TP +10% (隔1小时复开) [用户提议]", 0.10, False),
    ("Tier 1 TP +15% (隔1小时复开)", 0.15, False),
    ("Tier 1 TP +20% (隔1小时复开) [当前基准]", 0.20, False),
    ("Tier 1 TP +10% (平完秒开) [极致高频]", 0.10, True),
    ("Tier 1 TP +20% (平完秒开) [之前秒开对照]", 0.20, True),
]

results = []
print(f"\n{'策略配置':<35}{'总净利润':>12}{'已实现现金':>12}{'期末浮亏':>10}{'峰值保证金':>10}{'止盈次数':>8}{'T2补仓':>7}{'胜率':>7}")
print("-" * 105)

for label, pct, inst in targets:
    r = run_t1_experiment(t1_tp_pct=pct, instant=inst)
    results.append((label, r))
    print(f"{label:<35}${r['net_profit']:>11,.2f}${r['realized_cash']:>11,.2f}${r['floating_pnl']:>9,.2f}${r['peak_margin']:>9,.2f}{r['total_tps']:>8d}{r['dca_t2']:>7d}{r['win_rate']:>6.1f}%")

# Save summary
summary_out = []
for label, r in results:
    summary_out.append({
        "label": label,
        "t1_pct": r["t1_pct"],
        "instant": r["instant"],
        "net_profit": r["net_profit"],
        "realized_cash": r["realized_cash"],
        "floating_pnl": r["floating_pnl"],
        "funding_paid": r["total_funding_paid"],
        "fees_paid": r["total_fees_paid"],
        "peak_margin": r["peak_margin"],
        "max_dd_dollar": r["max_dd_dollar"],
        "total_opens": r["total_opens"],
        "total_tps": r["total_tps"],
        "dca_t2": r["dca_t2"],
        "dca_t3": r["dca_t3"],
        "win_rate": r["win_rate"]
    })

(RESULTS / "t1_target_comparison_summary.json").write_text(json.dumps(summary_out, indent=2, ensure_ascii=False))

# Plot
plt.figure(figsize=(14, 8))
for label, r in results:
    if "平完秒开" not in label:
        plt.plot(r["history_equity"], label=f"{label} (Net: ${r['net_profit']:+,.0f})")
plt.plot(results[4][1]["history_equity"], linestyle="--", label=f"{results[4][0]} (Net: ${results[4][1]['net_profit']:+,.0f})")
plt.title("Tier 1 Take-Profit Target Comparison: +5%, +10%, +15%, +20%", fontsize=14, fontweight="bold")
plt.xlabel("Hours", fontsize=11)
plt.ylabel("Cumulative Net PnL (USDT)", fontsize=11)
plt.grid(True, alpha=0.3)
plt.legend(loc="upper left", framealpha=0.9)
plt.tight_layout()
plt.savefig(RESULTS / "comparison_t1_targets.png", dpi=150)
plt.close()
print("\nSaved plot to data/results/comparison_t1_targets.png")
