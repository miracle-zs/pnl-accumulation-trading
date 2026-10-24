"""
Simulate the strategy across ALL 821 Binance USDT-M symbols simultaneously,
under the assumption of UNLIMITED CAPITAL (资金无限):
- No margin constraints / 0 DCA rejections
- No slot limits / all 821 symbols trade in parallel
- Full historical simulation (6,552 hours, 2026-01-01 to 2026-09-30)
- Real funding rates and maker/taker trading fees included

Compares 3 Re-open Modes:
1. Mode A: Hourly Reopen (每小时平仓即开: 极致动量追高收割)
2. Mode B: Daily 08:00 Reopen (每日定点调仓: 每日早8点平仓补开)
3. Mode C: Single Pass (单次遍历不复开: 每只币只跑一个完整生命周期)
"""

import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

DATA = Path("data")
RESULTS = DATA / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

# Load data
bars_full = np.load(DATA / "bars_cache_full.npy")
info_full = json.loads((DATA / "bars_info_full.json").read_text())
symbols = info_full["symbols"]
fund_full = np.load(DATA / "funding_matrix_full.npy")
hours = np.array(info_full["hours"], dtype=np.int32)
num_bars, num_symbols, _ = bars_full.shape

print(f"Loaded {num_bars} bars, {num_symbols} symbols.")

def run_unlimited(rebalance_mode="hourly"):
    """
    rebalance_mode:
      'hourly': whenever symbol closes, next hour it can re-open (cooldown 1h)
      'daily': rebalance only at UTC 08:00 (hours[b_idx] == 8)
      'single': never re-open once closed
    """
    initial_notional = 10.0
    leverage = 10.0
    maker_fee = 0.0002
    taker_fee = 0.0005
    t1_tp_ratio = 1.20
    t2_buy_drop = 0.50
    t2_notional = 20.0
    t2_tp_batch1 = 1.20
    t2_tp_batch2 = 1.50
    t3_buy_drop = 0.50
    t3_notional = 60.0
    t3_tp_batch1 = 1.20
    t3_tp_batch2 = 1.50
    t3_tp_batch3 = 2.00
    
    # Recycled devaluation
    mv_arm_threshold = 10.0
    mv_trigger_target = 5.0
    recycled_t2_notional = 20.0
    recycled_t3_notional = 60.0
    max_recycled_cycles = 1

    # State variables
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

    # Tracking metrics
    sym_realized_pnl = np.zeros(num_symbols, dtype=np.float64)
    sym_fees = np.zeros(num_symbols, dtype=np.float64)
    sym_funding = np.zeros(num_symbols, dtype=np.float64)
    sym_opens = np.zeros(num_symbols, dtype=np.int64)
    sym_tps = np.zeros(num_symbols, dtype=np.int64)
    sym_dca_t2 = np.zeros(num_symbols, dtype=np.int64)
    sym_dca_t3 = np.zeros(num_symbols, dtype=np.int64)
    sym_recycled = np.zeros(num_symbols, dtype=np.int64)

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

    # Main loop across all 6552 hours
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
                        # Recycled fill
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

                # Check if fully closed
                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    armed_10u[s] = False
                    cycle[s] = 0
                    last_exit_bar[s] = b_idx
                    break

        # Hourly funding calculation
        op_mask = is_open
        if np.any(op_mask):
            cur_p = last_p[op_mask]
            cur_q = qty[op_mask]
            cur_notional = cur_p * cur_q
            tot_notional = float(np.sum(cur_notional))
            tot_upnl = float(np.sum((cur_p - avg_price[op_mask]) * cur_q))
            # Funding charge
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

        # Scheduled Rebalance (Opening closed or newly listed symbols)
        if rebalance_mode != "single":
            trigger_rebalance = False
            if rebalance_mode == "hourly":
                trigger_rebalance = True
            elif rebalance_mode == "daily" and hours[b_idx] == 8:
                trigger_rebalance = True

            if trigger_rebalance:
                # Open idle symbols that have valid data at this bar
                # Avoid reopening within the same bar (cooldown >= 1 hour to prevent lookahead)
                idle = np.where(~is_open)[0]
                for s in idle:
                    if b_idx - last_exit_bar[s] >= 1:
                        p_now = bars_full[b_idx, s, 3]  # execute at Close of current bar
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

    # Drawdown of cumulative PnL
    eq = np.array(history_equity)
    running_max = np.maximum.accumulate(eq)
    dd_dollar = eq - running_max
    max_dd_dollar = float(np.min(dd_dollar))

    # Per-symbol PnL
    sym_floating = np.zeros(num_symbols)
    sym_floating[op_mask] = (cur_p - avg_price[op_mask]) * cur_q
    sym_net_total = sym_realized_pnl - sym_funding + sym_floating
    winners = int((sym_net_total > 0).sum())
    losers = int((sym_net_total < 0).sum())
    neutrals = int((sym_net_total == 0).sum())

    return {
        "mode": rebalance_mode,
        "total_net_profit": total_net_profit,
        "total_realized_cash": total_realized_cash,
        "total_fees_paid": total_fees_paid,
        "total_funding_paid": total_funding_paid,
        "final_floating_pnl": final_floating_pnl,
        "peak_notional": peak_notional,
        "peak_margin": peak_margin,
        "max_dd_dollar": max_dd_dollar,
        "final_open_notional": final_open_notional,
        "total_opens": int(np.sum(sym_opens)),
        "total_tps": int(np.sum(sym_tps)),
        "total_dca_t2": int(np.sum(sym_dca_t2)),
        "total_dca_t3": int(np.sum(sym_dca_t3)),
        "total_recycled": int(np.sum(sym_recycled)),
        "open_symbols_end": int(np.sum(is_open)),
        "winners": winners,
        "losers": losers,
        "neutrals": neutrals,
        "win_rate": winners / max(1, winners + losers) * 100,
        "sym_net_total": sym_net_total,
        "history_equity": history_equity,
        "history_notional": history_notional,
        "history_realized": history_realized
    }

print("Running 3 modes with Unlimited Capital across all 821 symbols...")

modes = [
    ("Mode A: 每小时平仓即开 (Hourly Reopen - 极致动量追高收割)", "hourly"),
    ("Mode B: 每日定点调仓 (Daily 08:00 - 每日早8点平仓补开)", "daily"),
    ("Mode C: 单次遍历不复开 (Single Pass - 每币仅跑一次生命周期)", "single")
]

all_res = []
for label, m in modes:
    print(f"\n--- Running {label} ---")
    r = run_unlimited(m)
    print(f"  总净利润 (Net Profit):       ${r['total_net_profit']:+,.2f}")
    print(f"  已实现纯利润 (Realized Cash):  ${r['total_realized_cash']:+,.2f}")
    print(f"  期末浮动盈亏 (Floating PnL):   ${r['final_floating_pnl']:+,.2f}")
    print(f"  累计支付资金费 (Funding):     ${r['total_funding_paid']:+,.2f}")
    print(f"  累计支付手续费 (Fees):        ${r['total_fees_paid']:+,.2f}")
    print(f"  峰值名义持仓 (Peak Notional):  ${r['peak_notional']:,.2f}")
    print(f"  峰值所需保证金 (Peak Margin):  ${r['peak_margin']:,.2f}")
    print(f"  最大回撤金额 (Max DD Dollar): ${r['max_dd_dollar']:,.2f}")
    print(f"  开仓次数: {r['total_opens']} | 止盈次数: {r['total_tps']} | T2补仓: {r['total_dca_t2']} | T3补仓: {r['total_dca_t3']}")
    print(f"  标的胜率 (赢/亏/平): {r['winners']}/{r['losers']}/{r['neutrals']} ({r['win_rate']:.1f}%)")
    all_res.append((label, r))

# Save summary
summary_data = []
for label, r in all_res:
    summary_data.append({
        "label": label,
        "mode": r["mode"],
        "net_profit": r["total_net_profit"],
        "realized_cash": r["total_realized_cash"],
        "floating_pnl": r["final_floating_pnl"],
        "funding_paid": r["total_funding_paid"],
        "fees_paid": r["total_fees_paid"],
        "peak_margin": r["peak_margin"],
        "peak_notional": r["peak_notional"],
        "max_dd_dollar": r["max_dd_dollar"],
        "total_opens": r["total_opens"],
        "total_tps": r["total_tps"],
        "dca_t2": r["total_dca_t2"],
        "dca_t3": r["total_dca_t3"],
        "recycled": r["total_recycled"],
        "win_rate": r["win_rate"],
        "winners": r["winners"],
        "losers": r["losers"]
    })

(RESULTS / "unlimited_capital_summary.json").write_text(json.dumps(summary_data, indent=2, ensure_ascii=False))

# Plot PnL curves
plt.figure(figsize=(14, 8))
for label, r in all_res:
    plt.plot(r["history_equity"], label=f"{label} (Net Profit: ${r['total_net_profit']:+,.0f})")
plt.title("Unlimited Capital: All 821 Binance Symbols (Full Portfolio PnL in USD)", fontsize=14, fontweight="bold")
plt.xlabel("Time (Hours)", fontsize=11)
plt.ylabel("Cumulative Net PnL (USDT)", fontsize=11)
plt.grid(True, alpha=0.3)
plt.legend(loc="upper left", framealpha=0.9)
plt.tight_layout()
plt.savefig(RESULTS / "comparison_unlimited_capital.png", dpi=150)
plt.close()
print("\nSaved plot to data/results/comparison_unlimited_capital.png")

# Top 10 winners & Top 10 losers in Mode A (Hourly)
r_hourly = all_res[0][1]
sym_pnl = r_hourly["sym_net_total"]
top_winners_idx = np.argsort(sym_pnl)[-10:][::-1]
top_losers_idx = np.argsort(sym_pnl)[:10]

print("\n--- [Mode A 每小时轮动] 盈利最高 Top 10 暴利币 ---")
for idx in top_winners_idx:
    print(f"  {symbols[idx]:<16}: Net Profit ${sym_pnl[idx]:>+9.2f}")

print("\n--- [Mode A 每小时轮动] 亏损最大 Top 10 吞金币 ---")
for idx in top_losers_idx:
    print(f"  {symbols[idx]:<16}: Net Profit ${sym_pnl[idx]:>+9.2f}")
