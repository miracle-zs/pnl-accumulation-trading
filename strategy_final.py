"""
Final Production Strategy Specification & Engine:
Architecture: Tier 3 Capped + Market Value Devaluation Recycling + Option A (Static Pending Orders, Reset on DCA)
Universe: Binance USDT-M Top 200 contracts
"""

import json
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

@dataclass
class StrategyConfig:
    initial_balance: float = 1000.0
    initial_notional: float = 10.0   # Tier 1 notional (USDT)
    leverage: float = 10.0          # 10x leverage Cross Margin
    maker_fee: float = 0.0002       # 0.02%
    taker_fee: float = 0.0005       # 0.05%
    mmr: float = 0.01               # 1.0% Maintenance Margin Rate
    
    # Tier DCA settings
    t1_tp_ratio: float = 1.20       # Tier 1 TP: +20%
    t2_buy_drop: float = 0.50       # Tier 2 buy price: -50% from Tier 1
    t2_notional: float = 20.0       # Tier 2 buy notional: 20U
    t2_tp_batch1: float = 1.20      # Tier 2 batch 1: +20% (50% size)
    t2_tp_batch2: float = 1.50      # Tier 2 batch 2: +50% (50% size)
    
    t3_buy_drop: float = 0.50       # Tier 3 buy price: -50% from Tier 2
    t3_notional: float = 60.0       # Tier 3 buy notional: 60U
    t3_tp_batch1: float = 1.20      # Tier 3 batch 1: +20% (1/3 size)
    t3_tp_batch2: float = 1.50      # Tier 3 batch 2: +50% (1/3 size)
    t3_tp_batch3: float = 2.00      # Tier 3 batch 3: +100% (1/3 size)
    
    # Option A flag: Static orders, no break-even
    use_breakeven: bool = False
    
    # Devaluation recycling triggers
    recycling_enabled: bool = True
    mv_arm_threshold: float = 10.0  # Trigger arm when MV <= 10U
    mv_trigger_target: float = 5.0  # Buy 20U when MV hits <= 5U
    recycled_t2_notional: float = 20.0
    recycled_t3_notional: float = 60.0
    max_recycled_cycles: int = 1    # Cap at 1 full cycle to avoid delist risk

def run_strategy(config: StrategyConfig = StrategyConfig(), data_dir: str = "data"):
    data_path = Path(data_dir)
    bars = np.load(data_path / "bars_cache.npy")
    info = json.loads((data_path / "bars_info.json").read_text())
    hours = np.array(info["hours"], dtype=np.int32)
    symbols = info["symbols"]
    num_symbols = len(symbols)
    num_bars = len(hours)
    
    cash_balance = config.initial_balance
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
    
    lowest_equity = config.initial_balance
    peak_equity = config.initial_balance
    max_drawdown = 0.0
    total_trades = 0
    total_realized_pnl = 0.0
    is_liquidated = False
    liquidation_time = None
    
    last_p = np.zeros(num_symbols, dtype=np.float64)
    history = []
    recycled_events = []
    
    # Bar 0 Initial Open
    for s in range(num_symbols):
        p = bars[0, s, 0]
        if not np.isnan(p):
            last_p[s] = p
            fee = config.initial_notional * config.taker_fee
            cash_balance -= fee
            q = config.initial_notional / p
            is_open[s] = True
            tier[s] = 1
            qty[s] = q
            size_notional[s] = config.initial_notional
            avg_price[s] = p
            limit_buy_price[s] = p * (1.0 - config.t2_buy_drop)
            limit_buy_notional[s] = config.t2_notional
            sell_active[s, 0] = True
            sell_price[s, 0] = p * config.t1_tp_ratio
            sell_qty[s, 0] = q
            sell_notional[s, 0] = config.initial_notional

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
            h, l, c = h_bar[s], l_bar[s], c_bar[s]
            
            # Devaluation check
            if config.recycling_enabled and tier[s] == 3 and cycle[s] < config.max_recycled_cycles:
                mv_low = l * qty[s]
                if not armed_10u[s] and mv_low <= config.mv_arm_threshold:
                    armed_10u[s] = True
                    p_trigger = config.mv_trigger_target / qty[s]
                    limit_buy_price[s] = p_trigger
                    limit_buy_notional[s] = config.recycled_t2_notional
                    
            sub_extremes = [l, h] if c >= o else [h, l]
            for pe in sub_extremes:
                if not is_open[s]: break
                
                # Check DCA buy
                lb_p = limit_buy_price[s]
                if lb_p > 0 and l <= lb_p:
                    lb_n = limit_buy_notional[s]
                    margin_needed = lb_n / config.leverage
                    
                    op_mask = is_open
                    lp_sub = last_p[op_mask]
                    q_sub = qty[op_mask]
                    cur_upnl = np.sum((lp_sub - avg_price[op_mask]) * q_sub)
                    cur_notional = np.sum(lp_sub * q_sub)
                    cur_eq = cash_balance + cur_upnl
                    free_m = max(0.0, cur_eq - (cur_notional / config.leverage))
                    
                    if free_m >= margin_needed:
                        fee = lb_n * config.maker_fee
                        cash_balance -= fee
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
                                hq = new_q * 0.5
                                hn = new_n * 0.5
                                sell_active[s, 0] = True; sell_price[s, 0] = new_avg * config.t2_tp_batch1; sell_qty[s, 0] = hq; sell_notional[s, 0] = hn
                                sell_active[s, 1] = True; sell_price[s, 1] = new_avg * config.t2_tp_batch2; sell_qty[s, 1] = hq; sell_notional[s, 1] = hn
                                limit_buy_price[s] = lb_p * (1.0 - config.t3_buy_drop)
                                limit_buy_notional[s] = config.t3_notional
                            elif curr_t == 3:
                                tq = new_q / 3.0
                                tn = new_n / 3.0
                                sell_active[s, 0] = True; sell_price[s, 0] = new_avg * config.t3_tp_batch1; sell_qty[s, 0] = tq; sell_notional[s, 0] = tn
                                sell_active[s, 1] = True; sell_price[s, 1] = new_avg * config.t3_tp_batch2; sell_qty[s, 1] = tq; sell_notional[s, 1] = tn
                                sell_active[s, 2] = True; sell_price[s, 2] = new_avg * config.t3_tp_batch3; sell_qty[s, 2] = tq; sell_notional[s, 2] = tn
                                limit_buy_price[s] = 0.0
                                limit_buy_notional[s] = 0.0
                        else:
                            # Recycled tier execution
                            cycle[s] += 1
                            recycled_events.append({
                                "symbol": symbols[s],
                                "timestamp": str(ts),
                                "cycle": int(cycle[s]),
                                "buy_price": float(lb_p),
                                "added_notional": float(lb_n),
                                "prev_mv": float(lb_p * qty[s])
                            })
                            qty[s] = new_q
                            size_notional[s] = new_n
                            local_effective_p = (config.mv_trigger_target + lb_n) / new_q
                            avg_price[s] = new_avg
                            sell_active[s, :] = False
                            hq = new_q * 0.5
                            hn = new_n * 0.5
                            sell_active[s, 0] = True; sell_price[s, 0] = local_effective_p * config.t2_tp_batch1; sell_qty[s, 0] = hq; sell_notional[s, 0] = hn
                            sell_active[s, 1] = True; sell_price[s, 1] = local_effective_p * config.t2_tp_batch2; sell_qty[s, 1] = hq; sell_notional[s, 1] = hn
                            
                            if cycle[s] == 1:
                                limit_buy_price[s] = lb_p * 0.50
                                limit_buy_notional[s] = config.recycled_t3_notional
                            else:
                                limit_buy_price[s] = 0.0
                                limit_buy_notional[s] = 0.0
                                armed_10u[s] = False
                                
                # Limit sells check (Option A: static maker orders)
                for slot in range(max_slots):
                    if sell_active[s, slot] and h >= sell_price[s, slot]:
                        sp = sell_price[s, slot]
                        sq = sell_qty[s, slot]
                        sn = sell_notional[s, slot]
                        pnl = (sp - avg_price[s]) * sq
                        fee = (sp * sq) * config.maker_fee
                        cash_balance += (pnl - fee)
                        qty[s] -= sq
                        size_notional[s] -= sn
                        sell_active[s, slot] = False
                        total_trades += 1
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
        mm = tot_notional * config.mmr
        
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
            free_m = max(0.0, equity - (tot_notional / config.leverage))
            closed_indices = np.where(~is_open)[0]
            for s in closed_indices:
                if free_m < 1.0: break
                p = bars[b_idx, s, 0]
                if not np.isnan(p):
                    cash_balance -= config.initial_notional * config.taker_fee
                    free_m -= 1.0
                    q = config.initial_notional / p
                    is_open[s] = True
                    tier[s] = 1
                    qty[s] = q
                    size_notional[s] = config.initial_notional
                    avg_price[s] = p
                    limit_buy_price[s] = p * (1.0 - config.t2_buy_drop)
                    limit_buy_notional[s] = config.t2_notional
                    sell_active[s, :] = False
                    sell_active[s, 0] = True
                    sell_price[s, 0] = p * config.t1_tp_ratio
                    sell_qty[s, 0] = q
                    sell_notional[s, 0] = config.initial_notional
                    armed_10u[s] = False
                    cycle[s] = 0

    tot_ret = (equity - config.initial_balance) / config.initial_balance * 100 if not is_liquidated else -100.0
    calmar = tot_ret / abs(max_drawdown * 100) if (max_drawdown < 0 and not is_liquidated) else 0.0
    
    return {
        "final_equity": float(equity),
        "total_return": float(tot_ret),
        "lowest_equity": float(lowest_equity),
        "max_drawdown": float(max_drawdown * 100),
        "calmar": float(calmar),
        "is_liquidated": is_liquidated,
        "liquidation_time": liquidation_time,
        "total_trades": total_trades,
        "recycled_events": recycled_events,
        "history": history
    }

if __name__ == "__main__":
    cfg = StrategyConfig()
    print("Executing Adopted Final Strategy: [Recycled Tier + Option A]...")
    res = run_strategy(cfg)
    print("\n" + "="*80)
    print("                    ADOPTED MASTER STRATEGY RESULTS                    ")
    print("="*80)
    print(f"Final Equity        : ${res['final_equity']:,.2f}")
    print(f"Total Return        : {res['total_return']:+.2f}%")
    print(f"Lowest Equity       : ${res['lowest_equity']:,.2f}")
    print(f"Max Drawdown        : {res['max_drawdown']:.2f}%")
    print(f"Calmar Ratio        : {res['calmar']:.2f}")
    print(f"Total Closed Batches: {res['total_trades']}")
    print(f"Recycled Triggers   : {len(res['recycled_events'])}")
    print(f"Liquidation Status  : {'LIQUIDATED' if res['is_liquidated'] else 'SAFE (0 Liquidations)'}")
    print("="*80)
