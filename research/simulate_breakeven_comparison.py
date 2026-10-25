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

def run_simulation(recycle_mode: str = "none", breakeven: bool = False):
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
    
    # Break-even tracking
    be_active = np.zeros(num_symbols, dtype=bool)
    be_price = np.zeros(num_symbols, dtype=np.float64)
    be_trigger_count = 0
    
    # Recycling states
    cycle = np.zeros(num_symbols, dtype=np.int32)
    armed_10u = np.zeros(num_symbols, dtype=bool)
    
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
                mv_low = l * qty[s]
                if not armed_10u[s] and mv_low <= 10.0:
                    armed_10u[s] = True
                    p_5u = 5.0 / qty[s]
                    limit_buy_price[s] = p_5u
                    limit_buy_notional[s] = 20.0
                    
            sub_extremes = [l, h] if c >= o else [h, l]
            for pe in sub_extremes:
                if not is_open[s]: break
                
                # Check Break-Even trigger if price is falling (pe == l)
                if breakeven and be_active[s] and pe == l and l <= be_price[s]:
                    be_p = be_price[s]
                    rem_q = qty[s]
                    pnl = (be_p - avg_price[s]) * rem_q
                    fee = (be_p * rem_q) * taker_fee
                    cash_balance += (pnl - fee)
                    total_closed_trades += 1
                    total_realized_pnl += (pnl - fee)
                    be_trigger_count += 1
                    
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    sell_active[s, :] = False
                    be_active[s] = False
                    armed_10u[s] = False
                    cycle[s] = 0
                    break

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
                        be_active[s] = False
                        
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
                                sell_active[s, 0] = True; sell_price[s, 0] = new_avg * 1.20; sell_qty[s, 0] = hq; sell_notional[s, 0] = hn
                                sell_active[s, 1] = True; sell_price[s, 1] = new_avg * 1.50; sell_qty[s, 1] = hq; sell_notional[s, 1] = hn
                                limit_buy_price[s] = lb_p * 0.50
                                limit_buy_notional[s] = new_n * 2.0
                            elif curr_t == 3:
                                tq = new_q / 3.0
                                tn = new_n / 3.0
                                sell_active[s, 0] = True; sell_price[s, 0] = new_avg * 1.20; sell_qty[s, 0] = tq; sell_notional[s, 0] = tn
                                sell_active[s, 1] = True; sell_price[s, 1] = new_avg * 1.50; sell_qty[s, 1] = tq; sell_notional[s, 1] = tn
                                sell_active[s, 2] = True; sell_price[s, 2] = new_avg * 2.00; sell_qty[s, 2] = tq; sell_notional[s, 2] = tn
                                limit_buy_price[s] = 0.0
                                limit_buy_notional[s] = 0.0
                        else:
                            # Recycled tier execution (fresh_tp)
                            cycle[s] += 1
                            qty[s] = new_q
                            size_notional[s] = new_n
                            local_effective_p = (5.0 + lb_n) / new_q
                            avg_price[s] = new_avg
                            sell_active[s, :] = False
                            hq = new_q * 0.5
                            hn = new_n * 0.5
                            sell_active[s, 0] = True; sell_price[s, 0] = local_effective_p * 1.20; sell_qty[s, 0] = hq; sell_notional[s, 0] = hn
                            sell_active[s, 1] = True; sell_price[s, 1] = local_effective_p * 1.50; sell_qty[s, 1] = hq; sell_notional[s, 1] = hn
                            
                            if cycle[s] == 1:
                                limit_buy_price[s] = lb_p * 0.50
                                limit_buy_notional[s] = 60.0
                            else:
                                limit_buy_price[s] = 0.0
                                limit_buy_notional[s] = 0.0
                                armed_10u[s] = False
                                
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
                        
                        # Enable break-even on slot 0 fill in Tier >= 2
                        if breakeven and (tier[s] in [2, 3]) and slot == 0:
                            be_active[s] = True
                            be_price[s] = avg_price[s]
                            
                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    be_active[s] = False
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
            break
            
        if equity > peak_equity: peak_equity = equity
        dd = (equity - peak_equity) / peak_equity
        if dd < max_drawdown: max_drawdown = dd
        if equity < lowest_equity: lowest_equity = equity
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
                    be_active[s] = False
                    armed_10u[s] = False
                    cycle[s] = 0

    tot_ret = (equity - initial_balance) / initial_balance * 100 if not is_liquidated else -100.0
    calmar = tot_ret / abs(max_drawdown * 100) if (max_drawdown < 0 and not is_liquidated) else 0.0
    
    return {
        "final_equity": float(equity) if not is_liquidated else 0.0,
        "total_return": float(tot_ret),
        "lowest_equity": float(lowest_equity),
        "max_drawdown": float(max_drawdown * 100),
        "calmar": float(calmar),
        "is_liquidated": is_liquidated,
        "liquidation_time": liquidation_time,
        "be_triggers": be_trigger_count,
        "history": history
    }

def main():
    configs = [
        ("Base + Option A (No BE)", "none", False),
        ("Base + Option B (Break-Even Stop)", "none", True),
        ("Recycled + Option A (No BE)", "fresh_tp", False),
        ("Recycled + Option B (Break-Even Stop)", "fresh_tp", True)
    ]
    
    results = []
    print("Running 4-way comparison...")
    for label, rec, be in configs:
        t0 = time.time()
        res = run_simulation(rec, be)
        res["label"] = label
        results.append(res)
        el = time.time() - t0
        print(f"[{label}] -> Ret: {res['total_return']:+.1f}% | Lowest: ${res['lowest_equity']:.1f} | MaxDD: {res['max_drawdown']:.1f}% | BE Triggers: {res['be_triggers']} | Time: {el:.1f}s")
        
    print("\n" + "="*115)
    print("                      4 CONFIGURATIONS COMPARISON (200 SYMBOLS, 1,000U)                    ")
    print("="*115)
    for r in results:
        status = f"💥 爆仓清零" if r["is_liquidated"] else f"${r['final_equity']:,.2f} ({r['total_return']:+.1f}%)"
        print(f"{r['label']:<38} | {status:<28} | 最低: ${r['lowest_equity']:<7.1f} | 回撤: {r['max_drawdown']:<6.1f}% | 保本触发: {r['be_triggers']:<4}")
    print("="*115)
    
    # Plot equity curves
    fig, ax = plt.subplots(figsize=(13, 7))
    timestamps = pd.date_range("2026-01-01 00:00:00", periods=num_bars, freq="1h")
    colors = ["#787B86", "#E91E63", "#2962FF", "#00C853"]
    linestyles = ["--", "-", "--", "-"]
    
    for i, r in enumerate(results):
        hist = r["history"]
        ts_sub = timestamps[:len(hist)]
        lbl = f"{r['label']} -> Ret: {r['total_return']:+.1f}%, MaxDD: {r['max_drawdown']:.1f}%, Lowest: ${r['lowest_equity']:.0f}"
        ax.plot(ts_sub, hist, label=lbl, color=colors[i], linestyle=linestyles[i], linewidth=2.0 if "B" in r['label'] else 1.6)
        
    ax.axhline(1000.0, color="#B0BEC5", linestyle=":", label="Initial Capital ($1,000)")
    ax.set_title("Base vs Recycled Tier under Option A (No BE) vs Option B (Break-Even Stop)", fontsize=13, fontweight="bold", pad=12)
    ax.set_ylabel("Account Equity (USD)", fontsize=11)
    ax.legend(loc="upper left", framealpha=0.9, fontsize=9.5)
    ax.grid(True, alpha=0.25)
    
    plt.tight_layout()
    chart_path = RESULTS_DIR / "comparison_4_configs_breakeven.png"
    plt.savefig(chart_path, dpi=200)
    plt.close()
    print("Saved 4-curve comparison chart to:", chart_path)

if __name__ == "__main__":
    main()
