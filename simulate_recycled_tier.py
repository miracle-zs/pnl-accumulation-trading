import json
import time
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

DATA_DIR = Path("data")
CACHE_BARS = DATA_DIR / "bars_cache.npy"
CACHE_INFO = DATA_DIR / "bars_info.json"
RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

info = json.loads(CACHE_INFO.read_text())
bars = np.load(CACHE_BARS)
hours = np.array(info["hours"], dtype=np.int32)
symbols = info["symbols"]
num_symbols = len(symbols)
num_bars = len(hours)

def run_recycled_tier_simulation(recycle_mode: str = "unified_avg"):
    """
    recycle_mode:
      'none': Base scheme (Tier 3 capped forever)
      'unified_avg': When Tier 3 MV <= 10U, and drops to <= 5U, buy 20U. If drops 50% more, buy 60U.
                     TP based on unified average price.
      'fresh_tp': When Tier 3 MV <= 10U and drops to <= 5U, buy 20U.
                  TP calculated on the new addition (+20% from new local avg price) to escape quickly.
    """
    initial_balance = 1000.0
    initial_notional = 10.0
    leverage = 10.0
    taker_fee = 0.0005
    maker_fee = 0.0002
    mmr = 0.01
    
    cash_balance = initial_balance
    is_open = np.zeros(num_symbols, dtype=bool)
    tier = np.zeros(num_symbols, dtype=np.int32)
    qty = np.zeros(num_symbols, dtype=np.float64)
    size_notional = np.zeros(num_symbols, dtype=np.float64)
    avg_price = np.zeros(num_symbols, dtype=np.float64)
    entry_p0 = np.zeros(num_symbols, dtype=np.float64)
    
    # Recycling states
    # cycle 0: normal Tier 1->2->3
    # cycle 1: recycled Tier 2 (buy 20U) -> Tier 3 (buy 60U)
    cycle = np.zeros(num_symbols, dtype=np.int32)
    armed_10u = np.zeros(num_symbols, dtype=bool)
    recycled_entry_p = np.zeros(num_symbols, dtype=np.float64)
    
    limit_buy_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_notional = np.zeros(num_symbols, dtype=np.float64)
    
    max_slots = 6
    sell_active = np.zeros((num_symbols, max_slots), dtype=bool)
    sell_price = np.zeros((num_symbols, max_slots), dtype=np.float64)
    sell_qty = np.zeros((num_symbols, max_slots), dtype=np.float64)
    sell_notional = np.zeros((num_symbols, max_slots), dtype=np.float64)
    
    lowest_equity = initial_balance
    peak_equity = initial_balance
    max_drawdown = 0.0
    total_closed_trades = 0
    total_realized_pnl = 0.0
    is_liquidated = False
    liquidation_time = None
    
    last_p = np.zeros(num_symbols, dtype=np.float64)
    history = []
    
    recycled_events = []
    
    # Bar 0 open
    for s in range(num_symbols):
        p = bars[0, s, 0]
        if not np.isnan(p):
            last_p[s] = p
            fee = initial_notional * taker_fee
            cash_balance -= fee
            q = initial_notional / p
            is_open[s] = True
            tier[s] = 1
            qty[s] = q
            size_notional[s] = initial_notional
            avg_price[s] = p
            entry_p0[s] = p
            limit_buy_price[s] = p * 0.50
            limit_buy_notional[s] = 20.0
            sell_active[s, 0] = True
            sell_price[s, 0] = p * 1.20
            sell_qty[s, 0] = q
            sell_notional[s, 0] = initial_notional
            
    for b_idx in range(num_bars):
        c_bar = bars[b_idx, :, 3]
        valid_c = ~np.isnan(c_bar)
        last_p[valid_c] = c_bar[valid_c]
        o_bar = bars[b_idx, :, 0]
        h_bar = bars[b_idx, :, 1]
        l_bar = bars[b_idx, :, 2]
        ts = pd.Timestamp("2026-01-01") + pd.Timedelta(hours=b_idx)
        
        open_indices = np.where(is_open)[0]
        for s in open_indices:
            o = o_bar[s]
            if np.isnan(o): continue
            h = h_bar[s]
            l = l_bar[s]
            c = c_bar[s]
            
            # 1. Check recycling trigger if in Tier 3
            if recycle_mode != "none" and tier[s] == 3:
                # Current market value
                mv_high = h * qty[s]
                mv_low = l * qty[s]
                
                # Check if market value dropped below 10U
                if not armed_10u[s] and mv_low <= 10.0:
                    armed_10u[s] = True
                    # Set trigger price for 5U
                    p_5u = 5.0 / qty[s]
                    limit_buy_price[s] = p_5u
                    limit_buy_notional[s] = 20.0  # Enter Tier 2 DCA: add 20U
                    
            sub_extremes = [l, h] if c >= o else [h, l]
            for pe in sub_extremes:
                # DCA Buy check
                lb_p = limit_buy_price[s]
                if lb_p > 0 and l <= lb_p:
                    lb_n = limit_buy_notional[s]
                    margin_needed = lb_n / leverage
                    
                    op_mask = is_open
                    lp_sub = last_p[op_mask]
                    q_sub = qty[op_mask]
                    cur_upnl = np.sum((lp_sub - avg_price[op_mask]) * q_sub)
                    cur_notional = np.sum(lp_sub * q_sub)
                    cur_eq = cash_balance + cur_upnl
                    free_m = max(0.0, cur_eq - (cur_notional / leverage))
                    
                    if free_m >= margin_needed:
                        fee = lb_n * maker_fee
                        cash_balance -= fee
                        added_q = lb_n / lb_p
                        new_q = qty[s] + added_q
                        new_n = size_notional[s] + lb_n
                        new_avg = new_n / new_q
                        
                        # Is this a normal tier progression or a recycled cycle?
                        if tier[s] < 3:
                            tier[s] += 1
                            curr_t = tier[s]
                            qty[s] = new_q
                            size_notional[s] = new_n
                            avg_price[s] = new_avg
                            
                            sell_active[s, :] = False
                            if curr_t == 2:
                                hq = new_q * 0.5
                                hn = new_n * 0.5
                                sell_active[s, 0] = True
                                sell_price[s, 0] = new_avg * 1.20
                                sell_qty[s, 0] = hq
                                sell_notional[s, 0] = hn
                                sell_active[s, 1] = True
                                sell_price[s, 1] = new_avg * 1.50
                                sell_qty[s, 1] = hq
                                sell_notional[s, 1] = hn
                                limit_buy_price[s] = lb_p * 0.50
                                limit_buy_notional[s] = new_n * 2.0
                            elif curr_t == 3:
                                tq = new_q / 3.0
                                tn = new_n / 3.0
                                sell_active[s, 0] = True
                                sell_price[s, 0] = new_avg * 1.20
                                sell_qty[s, 0] = tq
                                sell_notional[s, 0] = tn
                                sell_active[s, 1] = True
                                sell_price[s, 1] = new_avg * 1.50
                                sell_qty[s, 1] = tq
                                sell_notional[s, 1] = tn
                                sell_active[s, 2] = True
                                sell_price[s, 2] = new_avg * 2.00
                                sell_qty[s, 2] = tq
                                sell_notional[s, 2] = tn
                                limit_buy_price[s] = 0.0
                                limit_buy_notional[s] = 0.0
                        else:
                            # Recycled tier execution!
                            cycle[s] += 1
                            recycled_events.append({
                                "symbol": symbols[s],
                                "timestamp": str(ts),
                                "cycle": int(cycle[s]),
                                "buy_price": float(lb_p),
                                "added_notional": float(lb_n),
                                "prev_mv": float(lb_p * qty[s]),
                                "new_total_qty": float(new_q)
                            })
                            
                            qty[s] = new_q
                            size_notional[s] = new_n
                            
                            if recycle_mode == "unified_avg":
                                avg_price[s] = new_avg
                                sell_active[s, :] = False
                                # Set TP at +20%, +50% on unified avg
                                hq = new_q * 0.5
                                hn = new_n * 0.5
                                sell_active[s, 0] = True
                                sell_price[s, 0] = new_avg * 1.20
                                sell_qty[s, 0] = hq
                                sell_notional[s, 0] = hn
                                sell_active[s, 1] = True
                                sell_price[s, 1] = new_avg * 1.50
                                sell_qty[s, 1] = hq
                                sell_notional[s, 1] = hn
                            elif recycle_mode == "fresh_tp":
                                # Local bottom average price: (5U MV + 20U cash) / total qty
                                local_effective_p = (5.0 + lb_n) / new_q
                                avg_price[s] = new_avg # for PnL accounting
                                sell_active[s, :] = False
                                # 50% TP at local +20%, 50% TP at local +50%
                                hq = new_q * 0.5
                                hn = new_n * 0.5
                                sell_active[s, 0] = True
                                sell_price[s, 0] = local_effective_p * 1.20
                                sell_qty[s, 0] = hq
                                sell_notional[s, 0] = hn
                                sell_active[s, 1] = True
                                sell_price[s, 1] = local_effective_p * 1.50
                                sell_qty[s, 1] = hq
                                sell_notional[s, 1] = hn
                                
                            # If it drops another 50%, enter recycled Tier 3 (add 60U)!
                            if cycle[s] == 1:
                                limit_buy_price[s] = lb_p * 0.50
                                limit_buy_notional[s] = 60.0
                            else:
                                limit_buy_price[s] = 0.0
                                limit_buy_notional[s] = 0.0
                                armed_10u[s] = False
                    else:
                        pass
                        
                # Limit sells check
                for slot in range(max_slots):
                    if sell_active[s, slot] and h >= sell_price[s, slot]:
                        sp = sell_price[s, slot]
                        sq = sell_qty[s, slot]
                        sn = sell_notional[s, slot]
                        pnl = (sp - avg_price[s]) * sq
                        fee = (sp * sq) * maker_fee
                        cash_balance += (pnl - fee)
                        qty[s] -= sq
                        size_notional[s] -= sn
                        sell_active[s, slot] = False
                        total_closed_trades += 1
                        total_realized_pnl += (pnl - fee)
                        
                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    armed_10u[s] = False
                    cycle[s] = 0
                    break
                    
        op_mask = is_open
        if np.any(op_mask):
            lp_sub = last_p[op_mask]
            q_sub = qty[op_mask]
            tot_upnl = np.sum((lp_sub - avg_price[op_mask]) * q_sub)
            tot_notional = np.sum(lp_sub * q_sub)
        else:
            tot_upnl = 0.0
            tot_notional = 0.0
            
        equity = cash_balance + tot_upnl
        mm = tot_notional * mmr
        
        if equity <= mm:
            is_liquidated = True
            liquidation_time = str(ts)
            print(f"[{recycle_mode}] LIQUIDATED AT {ts}! Equity: ${equity:.2f}, MM: ${mm:.2f}")
            break
            
        if equity > peak_equity:
            peak_equity = equity
        dd = (equity - peak_equity) / peak_equity
        if dd < max_drawdown:
            max_drawdown = dd
        if equity < lowest_equity:
            lowest_equity = equity
            
        history.append(equity)
        
        # Daily 08:00 rebalance
        if hours[b_idx] == 8:
            free_m = max(0.0, equity - (tot_notional / leverage))
            closed_indices = np.where(~is_open)[0]
            for s in closed_indices:
                if free_m < 1.0: break
                p = bars[b_idx, s, 0]
                if not np.isnan(p):
                    cash_balance -= initial_notional * taker_fee
                    free_m -= 1.0
                    q = initial_notional / p
                    is_open[s] = True
                    tier[s] = 1
                    qty[s] = q
                    size_notional[s] = initial_notional
                    avg_price[s] = p
                    entry_p0[s] = p
                    limit_buy_price[s] = p * 0.50
                    limit_buy_notional[s] = 20.0
                    sell_active[s, :] = False
                    sell_active[s, 0] = True
                    sell_price[s, 0] = p * 1.20
                    sell_qty[s, 0] = q
                    sell_notional[s, 0] = initial_notional
                    armed_10u[s] = False
                    cycle[s] = 0
                    
    tot_ret = (equity - initial_balance) / initial_balance * 100 if not is_liquidated else -100.0
    calmar = tot_ret / abs(max_drawdown * 100) if (max_drawdown < 0 and not is_liquidated) else 0.0
    
    return {
        "mode": recycle_mode,
        "final_equity": float(equity) if not is_liquidated else 0.0,
        "total_return": float(tot_ret),
        "lowest_equity": float(lowest_equity),
        "max_drawdown": float(max_drawdown * 100),
        "calmar": float(calmar),
        "is_liquidated": is_liquidated,
        "liquidation_time": liquidation_time,
        "total_trades": total_closed_trades,
        "recycled_events": recycled_events,
        "history": history
    }

def main():
    modes = [
        ("Base (Tier 3 封顶躺平，不循环)", "none"),
        ("循环加仓 (统一全仓均价止盈)", "unified_avg"),
        ("循环加仓 (底部独立脉冲止盈)", "fresh_tp")
    ]
    
    results = []
    print("Running Recycled Tier Experiments...")
    for label, mode in modes:
        t0 = time.time()
        res = run_recycled_tier_simulation(mode)
        res["label"] = label
        results.append(res)
        el = time.time() - t0
        print(f"[{label}] -> Liquidated: {res['is_liquidated']} | Return: {res['total_return']:+.1f}% | Lowest: ${res['lowest_equity']:.1f} | MaxDD: {res['max_drawdown']:.1f}% | Recycled Events: {len(res['recycled_events'])} | Time: {el:.1f}s")
        if res["recycled_events"]:
            print(f"   Sample Recycled Events:")
            for ev in res["recycled_events"][:3]:
                print(f"     {ev['timestamp']} | {ev['symbol']} | Buy: {ev['buy_price']:.6f} | Added: {ev['added_notional']}U | Prev MV: ${ev['prev_mv']:.2f}")

    print("\n" + "="*115)
    print("                 RECYCLED TIER EXPERIMENT COMPARISON (200 SYMBOLS, 1,000U)                 ")
    print("="*115)
    for r in results:
        status = f"💥 爆仓清零 ({r['liquidation_time'][:13]})" if r["is_liquidated"] else f"${r['final_equity']:,.2f} ({r['total_return']:+.1f}%)"
        print(f"{r['label']:<38} | {status:<30} | 最低: ${r['lowest_equity']:<7.1f} | 回撤: {r['max_drawdown']:<6.1f}% | 循环加仓事件: {len(r['recycled_events'])}")
    print("="*115)
    
    # Plot equity curves
    fig, ax = plt.subplots(figsize=(13, 7))
    timestamps = pd.date_range("2026-01-01 00:00:00", periods=num_bars, freq="1h")
    colors = ["#787B86", "#2962FF", "#00C853"]
    linestyles = ["--", "-", "-"]
    
    for i, r in enumerate(results):
        hist = r["history"]
        ts_sub = timestamps[:len(hist)]
        mode_name_en = "Base (No Recycling)" if "Base" in r['label'] else ("Recycled (Unified Avg TP)" if "统一" in r['label'] else "Recycled (Fresh Pulse TP)")
        lbl = f"{mode_name_en} -> Ret: {r['total_return']:+.1f}%, MaxDD: {r['max_drawdown']:.1f}%, Lowest: ${r['lowest_equity']:.0f}"
        ax.plot(ts_sub, hist, label=lbl, color=colors[i], linestyle=linestyles[i], linewidth=2.0)
        
    ax.axhline(1000.0, color="#B0BEC5", linestyle=":", label="Initial Capital ($1,000)")
    ax.set_title("Tier 3 Devaluation & Re-DCA Recycling Mechanism Comparison (200 Symbols, 2026)", fontsize=13, fontweight="bold", pad=12)
    ax.set_ylabel("Account Equity (USD)", fontsize=11)
    ax.legend(loc="upper left", framealpha=0.9, fontsize=9.5)
    ax.grid(True, alpha=0.25)
    
    plt.tight_layout()
    chart_path = RESULTS_DIR / "comparison_recycled_tier.png"
    plt.savefig(chart_path, dpi=200)
    plt.close()
    print("Saved comparison chart to:", chart_path)

if __name__ == "__main__":
    main()
