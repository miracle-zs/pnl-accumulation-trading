import json
import time
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

CACHE_BARS = DATA_DIR / "bars_cache.npy"
CACHE_INFO = DATA_DIR / "bars_info.json"

info = json.loads(CACHE_INFO.read_text())
bars = np.load(CACHE_BARS)
hours = np.array(info["hours"], dtype=np.int32)
symbols = info["symbols"]
num_symbols = len(symbols)
num_bars = len(hours)

def run_tier_experiment(max_allowed_tiers: int, mult_type: str = "mult_total"):
    """
    max_allowed_tiers: 3, 4, 5, 10 (unlimited)
    mult_type:
      'mult_total': added = current_total * 2.0 (10, 20, 60, 180, 540...) as in engine.py
      'mult_prev': added = prev_added * 2.0 (10, 20, 40, 80, 160...)
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
    prev_added_notional = np.zeros(num_symbols, dtype=np.float64)
    
    limit_buy_price = np.zeros(num_symbols, dtype=np.float64)
    limit_buy_notional = np.zeros(num_symbols, dtype=np.float64)
    
    # Store up to 10 sell slots
    max_slots = max(3, max_allowed_tiers)
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
    liquidation_bar = -1
    
    last_p = np.zeros(num_symbols, dtype=np.float64)
    
    # Tier reached statistics
    max_tier_reached = 1
    tier_counts = {t: 0 for t in range(1, 10)}
    
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
            prev_added_notional[s] = initial_notional
            
            # Next DCA
            limit_buy_price[s] = p * 0.50
            limit_buy_notional[s] = 20.0
            
            # TP
            sell_active[s, 0] = True
            sell_price[s, 0] = p * 1.20
            sell_qty[s, 0] = q
            sell_notional[s, 0] = initial_notional
            
    history = []
    
    for b_idx in range(num_bars):
        c_bar = bars[b_idx, :, 3]
        valid_c = ~np.isnan(c_bar)
        last_p[valid_c] = c_bar[valid_c]
        
        o_bar = bars[b_idx, :, 0]
        h_bar = bars[b_idx, :, 1]
        l_bar = bars[b_idx, :, 2]
        
        open_indices = np.where(is_open)[0]
        for s in open_indices:
            o = o_bar[s]
            if np.isnan(o):
                continue
            h = h_bar[s]
            l = l_bar[s]
            c = c_bar[s]
            
            sub_extremes = [l, h] if c >= o else [h, l]
            for pe in sub_extremes:
                # DCA Buy
                lb_p = limit_buy_price[s]
                if lb_p > 0 and l <= lb_p:
                    lb_n = limit_buy_notional[s]
                    margin_needed = lb_n / leverage
                    
                    # Quick free margin check
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
                        
                        tier[s] += 1
                        curr_t = tier[s]
                        if curr_t > max_tier_reached:
                            max_tier_reached = curr_t
                        tier_counts[curr_t] = tier_counts.get(curr_t, 0) + 1
                        
                        qty[s] = new_q
                        size_notional[s] = new_n
                        avg_price[s] = new_avg
                        prev_added_notional[s] = lb_n
                        
                        sell_active[s, :] = False
                        
                        # Dynamic multi-batch TP based on tier
                        # Tier 2: 2 batches (+20%, +50%)
                        # Tier 3: 3 batches (+20%, +50%, +100%)
                        # Tier k: 3 batches (+20%, +50%, +100%) or k batches
                        num_batches = min(curr_t, 3)
                        batch_q = new_q / num_batches
                        batch_n = new_n / num_batches
                        
                        tp_targets = [0.20, 0.50, 1.00]
                        for slot in range(num_batches):
                            sell_active[s, slot] = True
                            sell_price[s, slot] = new_avg * (1.0 + tp_targets[slot])
                            sell_qty[s, slot] = batch_q
                            sell_notional[s, slot] = batch_n
                            
                        # Next DCA check
                        if curr_t < max_allowed_tiers:
                            # Drop 50% from previous DCA price
                            limit_buy_price[s] = lb_p * 0.50
                            if mult_type == "mult_total":
                                limit_buy_notional[s] = new_n * 2.0
                            else:
                                limit_buy_notional[s] = lb_n * 2.0
                        else:
                            limit_buy_price[s] = 0.0
                            limit_buy_notional[s] = 0.0
                    else:
                        # Insufficient margin to DCA!
                        pass
                        
                # Limit Sells
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
            liquidation_bar = b_idx
            return {
                "max_allowed_tiers": max_allowed_tiers,
                "mult_type": mult_type,
                "final_equity": 0.0,
                "total_return": -100.0,
                "lowest_equity": equity,
                "max_drawdown": -100.0,
                "calmar": 0.0,
                "is_liquidated": True,
                "liquidation_time": str(pd.Timestamp("2026-01-01") + pd.Timedelta(hours=b_idx)),
                "max_tier_reached": max_tier_reached,
                "tier_counts": tier_counts,
                "total_trades": total_closed_trades,
                "history": history
            }
            
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
            free_margin = max(0.0, equity - (tot_notional / leverage))
            closed_indices = np.where(~is_open)[0]
            for s in closed_indices:
                if free_margin < (initial_notional / leverage):
                    break
                p = bars[b_idx, s, 0]
                if not np.isnan(p):
                    fee = initial_notional * taker_fee
                    cash_balance -= fee
                    free_margin -= (initial_notional / leverage)
                    q = initial_notional / p
                    is_open[s] = True
                    tier[s] = 1
                    qty[s] = q
                    size_notional[s] = initial_notional
                    avg_price[s] = p
                    prev_added_notional[s] = initial_notional
                    limit_buy_price[s] = p * 0.50
                    limit_buy_notional[s] = 20.0
                    sell_active[s, :] = False
                    sell_active[s, 0] = True
                    sell_price[s, 0] = p * 1.20
                    sell_qty[s, 0] = q
                    sell_notional[s, 0] = initial_notional
                    
    tot_ret = (equity - initial_balance) / initial_balance * 100
    calmar = tot_ret / abs(max_drawdown * 100) if max_drawdown < 0 else 0.0
    
    return {
        "max_allowed_tiers": max_allowed_tiers,
        "mult_type": mult_type,
        "final_equity": float(equity),
        "total_return": float(tot_ret),
        "lowest_equity": float(lowest_equity),
        "max_drawdown": float(max_drawdown * 100),
        "calmar": float(calmar),
        "is_liquidated": False,
        "liquidation_time": None,
        "max_tier_reached": max_tier_reached,
        "tier_counts": tier_counts,
        "total_trades": total_closed_trades,
        "history": history
    }

def main():
    experiments = [
        ("Base (Max Tier 3, Capped 90U)", 3, "mult_total"),
        ("Max Tier 4 (Capped 270U)", 4, "mult_total"),
        ("Max Tier 5 (Capped 810U)", 5, "mult_total"),
        ("Unlimited Tiers (Exp Multiplier)", 10, "mult_total"),
        ("Max Tier 4 (Linear 10/20/40/80U)", 4, "mult_prev"),
        ("Max Tier 5 (Linear 10/20/40/80/160U)", 5, "mult_prev"),
        ("Unlimited Tiers (Linear Multiplier)", 10, "mult_prev")
    ]
    
    results = []
    print("Running Tier experiments...")
    for label, max_t, m_type in experiments:
        t0 = time.time()
        res = run_tier_experiment(max_t, m_type)
        res["label"] = label
        results.append(res)
        el = time.time() - t0
        print(f"[{label}] -> Liquidated: {res['is_liquidated']} | Return: {res['total_return']:+.1f}% | Lowest: ${res['lowest_equity']:.1f} | MaxDD: {res['max_drawdown']:.1f}% | Max Tier: {res['max_tier_reached']} | Time: {el:.1f}s")
        if res["is_liquidated"]:
            print(f"   --> Liquidation triggered at: {res['liquidation_time']}")
            
    # Print comparison table
    print("\n" + "="*115)
    print("                 TIER LIMIT REMOVAL EXPERIMENT (200 SYMBOLS, 1,000U)                 ")
    print("="*115)
    for r in results:
        status = f"💥 爆仓清零 ({r['liquidation_time'][:13]})" if r["is_liquidated"] else f"${r['final_equity']:,.2f} ({r['total_return']:+.1f}%)"
        print(f"{r['label']:<38} | {status:<30} | 最低: ${r['lowest_equity']:<7.1f} | 回撤: {r['max_drawdown']:<6.1f}% | 最大层级: {r['max_tier_reached']}")
    print("="*115)
    
    # Save json summary
    def serialize_obj(obj):
        if isinstance(obj, (np.integer, np.int32, np.int64)):
            return int(obj)
        elif isinstance(obj, (np.floating, np.float32, np.float64)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, dict):
            return {str(k): serialize_obj(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [serialize_obj(x) for x in obj]
        return obj

    clean_res = [{k: serialize_obj(v) for k, v in r.items() if k != "history"} for r in results]
    (RESULTS_DIR / "unlimited_tiers_experiment.json").write_text(json.dumps(clean_res, indent=2))
    
    # Plot equity curves
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), gridspec_kw={"height_ratios": [2.5, 1]})
    colors = ["#787B86", "#E53935", "#D81B60", "#8E24AA", "#1E88E5", "#00ACC1", "#43A047"]
    
    # Timestamps
    timestamps = pd.date_range("2026-01-01 00:00:00", periods=num_bars, freq="1h")
    
    for i, r in enumerate(results):
        hist = r["history"]
        ts_sub = timestamps[:len(hist)]
        ax1.plot(ts_sub, hist, label=f"{r['label']} -> {r['total_return']:+.1f}%", color=colors[i], linewidth=2.0 if i==0 else 1.5, linestyle="--" if i==0 else "-")
        
    ax1.axhline(1000.0, color="#B0BEC5", linestyle=":", label="Initial Capital ($1,000)")
    ax1.set_title("Strategy Comparison: Base (Tier 3 Capped) vs Unlimited Tiers (200 Symbols, 2026)", fontsize=13, fontweight="bold", pad=12)
    ax1.set_ylabel("Account Equity (USD)", fontsize=11)
    ax1.legend(loc="upper left", framealpha=0.9, fontsize=9)
    ax1.grid(True, alpha=0.25)
    
    # Plot Tier counts for Base vs Unlimited
    base_tc = results[0]["tier_counts"]
    unlim_tc = results[3]["tier_counts"]
    tiers = [f"Tier {t}" for t in range(1, 8)]
    base_vals = [base_tc.get(t, 0) for t in range(1, 8)]
    unlim_vals = [unlim_tc.get(t, 0) for t in range(1, 8)]
    
    x = np.arange(len(tiers))
    width = 0.35
    ax2.bar(x - width/2, base_vals, width, label="Base (Capped Tier 3)", color="#787B86", alpha=0.85)
    ax2.bar(x + width/2, unlim_vals, width, label="Unlimited Tiers (Exp)", color="#E53935", alpha=0.85)
    ax2.set_ylabel("Trigger Count", fontsize=11)
    ax2.set_xlabel("Tier Level Escalation", fontsize=11)
    ax2.set_xticks(x)
    ax2.set_xticklabels(tiers)
    ax2.legend(loc="upper right", fontsize=9)
    ax2.grid(True, alpha=0.25, axis="y")
    
    plt.tight_layout()
    chart_path = RESULTS_DIR / "comparison_unlimited_tiers.png"
    plt.savefig(chart_path, dpi=200)
    plt.close()
    print("Saved chart to:", chart_path)

if __name__ == "__main__":
    main()
