"""
Simulation & Comparison: Periodic PnL Review Strategies vs Continuous Limit TP

User Request:
1. 每小时进行一次收益统计，如果收益大于10%就平掉，然后再开 (Hourly Close PnL Review >= 10%)
2. 北京时间每天早上8点进行一次收益统计，如果收益大于10%就平掉，然后再开 (Daily 08:00 Beijing Time PnL Review >= 10%)

We compare:
A. 纯定时统计平仓 (Periodic Discrete Close Review - No Intraday Limit TP):
   1. [新测] 每小时收盘统计：浮盈>=10%全平，收盘即开 (Hourly Review Close >= 10%)
   2. [新测] 每天早8点统计：浮盈>=10%全平，早8点即开 (Daily 08:00 Review Close >= 10%)

B. 盘中限价单挂单止盈 (Intraday Limit Order TP):
   3. [基准] T1 +10% 盘中限价止盈 (同小时收盘即开)
   4. [基准] T1 +10% 盘中限价止盈 (隔1小时复开)
   5. [基准] T1 +10% 盘中限价止盈 (平完秒开)
   6. [历史冠军] T1 +5% 盘中限价止盈 (同小时收盘即开)
   7. [原初基准] T1 +20% 盘中限价止盈 (隔1小时复开)

Environment:
- 821 Binance USDT-M symbols, 6,552 hours (Feb - Oct 2026).
- Unlimited Capital.
- Real funding rates, Maker fee 0.02%, Taker fee 0.05%.
- Tier 2 (-50%, 20U), Tier 3 (-50%, 60U), Recycled Devaluation included.
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

def run_simulation(strategy_mode="hourly_close_review", tp_threshold=0.10):
    """
    strategy_mode:
      - 'hourly_close_review': No intraday limit TP. Every bar close, if (c - avg)/avg >= tp_threshold: close & reopen.
      - 'daily_0800_review': No intraday limit TP. Every Beijing 08:00 (hours[b]==0), if (c - avg)/avg >= tp_threshold: close & reopen.
      - 'limit_same_bar_close': Intraday limit TP at avg*(1+tp_threshold). Same-bar close reopen.
      - 'limit_next_bar': Intraday limit TP at avg*(1+tp_threshold). 1h delay reopen.
      - 'limit_instant': Intraday limit TP at avg*(1+tp_threshold). Intra-bar instant reopen.
    """
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
    same_bar_multi_tp = 0

    total_realized_cash = 0.0
    total_fees_paid = 0.0
    total_funding_paid = 0.0

    history_notional = []
    history_equity = []

    last_p = np.zeros(num_symbols, dtype=np.float64)

    is_periodic_review = strategy_mode in ("hourly_close_review", "daily_0800_review")
    has_limit_tp = not is_periodic_review

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
        
        if has_limit_tp:
            sell_active[s, 0] = True
            sell_price[s, 0] = p * (1.0 + tp_threshold)
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

                        if has_limit_tp:
                            if curr_t == 2:
                                sym_dca_t2[s] += 1
                                hq = new_q * 0.5
                                hn = new_n * 0.5
                                sell_active[s, 0] = True; sell_price[s, 0] = new_avg * t2_tp_batch1; sell_qty[s, 0] = hq; sell_notional[s, 0] = hn
                                sell_active[s, 1] = True; sell_price[s, 1] = new_avg * t2_tp_batch2; sell_qty[s, 1] = hq; sell_notional[s, 1] = hn
                            elif curr_t == 3:
                                sym_dca_t3[s] += 1
                                tq = new_q / 3.0
                                tn = new_n / 3.0
                                sell_active[s, 0] = True; sell_price[s, 0] = new_avg * t3_tp_batch1; sell_qty[s, 0] = tq; sell_notional[s, 0] = tn
                                sell_active[s, 1] = True; sell_price[s, 1] = new_avg * t3_tp_batch2; sell_qty[s, 1] = tq; sell_notional[s, 1] = tn
                                sell_active[s, 2] = True; sell_price[s, 2] = new_avg * t3_tp_batch3; sell_qty[s, 2] = tq; sell_notional[s, 2] = tn

                        if curr_t == 2:
                            limit_buy_price[s] = lb_p * (1.0 - t3_buy_drop)
                            limit_buy_notional[s] = t3_notional
                        elif curr_t == 3:
                            limit_buy_price[s] = 0.0
                            limit_buy_notional[s] = 0.0
                    else:
                        cycle[s] += 1
                        qty[s] = new_q
                        size_notional[s] = new_n
                        local_eff_p = (mv_trigger_target + lb_n) / new_q
                        avg_price[s] = new_avg
                        sell_active[s, :] = False
                        if has_limit_tp:
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

                # 2. Limit sell (TP) check (only for modes with intraday limit TP)
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

                    # Check if fully closed
                    if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                        is_open[s] = False
                        tier[s] = 0
                        qty[s] = 0.0
                        limit_buy_price[s] = 0.0
                        armed_10u[s] = False
                        cycle[s] = 0
                        last_exit_bar[s] = b_idx

                        if strategy_mode == "limit_instant":
                            cur_entry_p = sp
                            open_symbol(s, cur_entry_p, b_idx)
                            while True:
                                next_tp_p = cur_entry_p * (1.0 + tp_threshold)
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

        # --- PERIODIC CLOSE-BASED PnL REVIEW ---
        # 1. Hourly Review: check at every bar close
        # 2. Daily 08:00 Review: check only when hours[b_idx] == 0 (Beijing 08:00)
        eval_pnl_now = False
        if strategy_mode == "hourly_close_review":
            eval_pnl_now = True
        elif strategy_mode == "daily_0800_review" and hours[b_idx] == 0:
            eval_pnl_now = True

        if eval_pnl_now:
            op_syms = np.where(is_open)[0]
            for s in op_syms:
                cp = c_bar[s]
                if np.isnan(cp) or cp <= 0: continue
                # Calculate return on average price
                ret = (cp - avg_price[s]) / avg_price[s]
                if ret >= tp_threshold:
                    # Profit > 10%! Market close entire position at Close price!
                    pnl = (cp - avg_price[s]) * qty[s]
                    fee = (cp * qty[s]) * taker_fee
                    total_realized_cash += (pnl - fee)
                    total_fees_paid += fee
                    sym_realized_pnl[s] += (pnl - fee)
                    sym_fees[s] += fee
                    sym_tps[s] += 1

                    # Reset position
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    armed_10u[s] = False
                    cycle[s] = 0
                    last_exit_bar[s] = b_idx

                    # Immediately reopen Tier 1 at this Close price!
                    open_symbol(s, cp, b_idx)

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

        # End of bar scheduled reopen for idle symbols
        idle = np.where(~is_open)[0]
        for s in idle:
            can_reopen = False
            if sym_opens[s] == 0:
                can_reopen = True
            elif strategy_mode == "hourly_close_review":
                can_reopen = True
            elif strategy_mode == "daily_0800_review" and hours[b_idx] == 0:
                can_reopen = True
            elif strategy_mode == "limit_same_bar_close":
                can_reopen = True
            elif strategy_mode == "limit_next_bar" and (b_idx - last_exit_bar[s] >= 1):
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
        "strategy": strategy_mode,
        "tp_threshold": tp_threshold,
        "net_profit": total_net_profit,
        "realized_cash": total_realized_cash,
        "floating_pnl": final_floating_pnl,
        "funding_paid": total_funding_paid,
        "fees_paid": total_fees_paid,
        "peak_margin": peak_margin,
        "max_dd_dollar": max_dd_dollar,
        "total_opens": int(np.sum(sym_opens)),
        "total_tps": int(np.sum(sym_tps)),
        "same_bar_multi_tp": same_bar_multi_tp,
        "winners": winners,
        "losers": losers,
        "win_rate": winners / max(1, winners + losers) * 100,
        "history_equity": history_equity
    }

if __name__ == "__main__":
    experiments = [
        ("1. [用户新增] 每小时收益统计>=10%即平且开 (Hourly Review 10%)", "hourly_close_review", 0.10),
        ("2. [用户新增] 每天早8点收益统计>=10%即平且开 (Daily 08:00 Review 10%)", "daily_0800_review", 0.10),
        ("3. [对比基准] T1 +10% 盘中限价止盈 (同小时收盘即开)", "limit_same_bar_close", 0.10),
        ("4. [对比基准] T1 +10% 盘中限价止盈 (隔1小时复开)", "limit_next_bar", 0.10),
        ("5. [对比基准] T1 +10% 盘中限价止盈 (平完秒开)", "limit_instant", 0.10),
        ("6. [历史冠军] T1 +5% 盘中限价止盈 (同小时收盘即开)", "limit_same_bar_close", 0.05),
        ("7. [原初基准] T1 +20% 盘中限价止盈 (隔1小时复开)", "limit_next_bar", 0.20),
    ]

    results = []
    for label, mode, tp in experiments:
        print(f"\n--- Running {label} ---")
        r = run_simulation(mode, tp)
        print(f"  总净利润 (Net Profit):       ${r['net_profit']:+,.2f}")
        print(f"  已实现纯现金 (Realized Cash):  ${r['realized_cash']:+,.2f}")
        print(f"  期末浮动盈亏 (Floating PnL):   ${r['floating_pnl']:+,.2f}")
        print(f"  总手续费 (Total Fees):        ${r['fees_paid']:,.2f}")
        print(f"  峰值所需保证金 (Peak Margin):  ${r['peak_margin']:,.2f}")
        print(f"  最大回撤金额 (Max DD Dollar): ${r['max_dd_dollar']:,.2f}")
        print(f"  开仓次数: {r['total_opens']} | 止盈/平仓次数: {r['total_tps']}")
        print(f"  标的胜率: {r['winners']}/{r['losers']} ({r['win_rate']:.1f}%)")
        results.append((label, r))

    summary_out = []
    for label, r in results:
        summary_out.append({
            "label": label,
            "strategy": r["strategy"],
            "tp_threshold": r["tp_threshold"],
            "net_profit": r["net_profit"],
            "realized_cash": r["realized_cash"],
            "floating_pnl": r["floating_pnl"],
            "funding_paid": r["funding_paid"],
            "fees_paid": r["fees_paid"],
            "peak_margin": r["peak_margin"],
            "max_dd_dollar": r["max_dd_dollar"],
            "total_opens": r["total_opens"],
            "total_tps": r["total_tps"],
            "win_rate": r["win_rate"]
        })

    (RESULTS / "periodic_pnl_review_summary.json").write_text(json.dumps(summary_out, indent=2, ensure_ascii=False))

    # Plot
    plt.figure(figsize=(15, 9))
    colors = ["#d62728", "#9467bd", "#2ca02c", "#1f77b4", "#ff7f0e", "#17becf", "#7f7f7f"]
    linestyles = ["-", "-", "--", "--", ":", "-", "--"]
    linewidths = [2.5, 2.5, 2.0, 2.0, 1.8, 2.2, 1.8]

    for i, (label, r) in enumerate(results):
        plt.plot(r["history_equity"], label=f"{label} (Net: ${r['net_profit']:+,.0f})", 
                 color=colors[i], linestyle=linestyles[i], linewidth=linewidths[i])

    plt.title("Periodic PnL Review (Hourly & Daily 08:00) vs Intraday Limit TP: 821 Symbols", fontsize=14, fontweight="bold")
    plt.xlabel("Hours (9-month Backtest: Feb - Oct 2026)", fontsize=11)
    plt.ylabel("Cumulative Net PnL (USDT)", fontsize=11)
    plt.grid(True, alpha=0.3)
    plt.legend(loc="upper left", framealpha=0.9, fontsize=9.5)
    plt.tight_layout()
    plt.savefig(RESULTS / "comparison_periodic_review.png", dpi=150)
    plt.close()
    print("\nSaved plot to data/results/comparison_periodic_review.png")
