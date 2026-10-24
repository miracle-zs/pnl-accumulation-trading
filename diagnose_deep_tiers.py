import json
import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR = Path("data")
CACHE_BARS = DATA_DIR / "bars_cache.npy"
CACHE_INFO = DATA_DIR / "bars_info.json"

info = json.loads(CACHE_INFO.read_text())
bars = np.load(CACHE_BARS)
hours = np.array(info["hours"], dtype=np.int32)
symbols = info["symbols"]
num_symbols = len(symbols)
num_bars = len(hours)

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
limit_buy_price = np.zeros(num_symbols, dtype=np.float64)
limit_buy_notional = np.zeros(num_symbols, dtype=np.float64)

max_slots = 10
sell_active = np.zeros((num_symbols, max_slots), dtype=bool)
sell_price = np.zeros((num_symbols, max_slots), dtype=np.float64)
sell_qty = np.zeros((num_symbols, max_slots), dtype=np.float64)
sell_notional = np.zeros((num_symbols, max_slots), dtype=np.float64)

last_p = np.zeros(num_symbols, dtype=np.float64)

for s in range(num_symbols):
    p = bars[0, s, 0]
    if not np.isnan(p):
        last_p[s] = p
        cash_balance -= initial_notional * taker_fee
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

deep_events = []
closed_deep_trades = []

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
        sub_extremes = [l, h] if c >= o else [h, l]
        for pe in sub_extremes:
            lb_p = limit_buy_price[s]
            if lb_p > 0 and l <= lb_p:
                lb_n = limit_buy_notional[s]
                # Check free margin
                op_mask = is_open
                lp_sub = last_p[op_mask]
                q_sub = qty[op_mask]
                cur_upnl = np.sum((lp_sub - avg_price[op_mask]) * q_sub)
                cur_notional = np.sum(lp_sub * q_sub)
                cur_eq = cash_balance + cur_upnl
                free_m = max(0.0, cur_eq - (cur_notional / leverage))
                margin_needed = lb_n / leverage
                
                if free_m >= margin_needed:
                    cash_balance -= lb_n * maker_fee
                    added_q = lb_n / lb_p
                    new_q = qty[s] + added_q
                    new_n = size_notional[s] + lb_n
                    new_avg = new_n / new_q
                    tier[s] += 1
                    curr_t = tier[s]
                    qty[s] = new_q
                    size_notional[s] = new_n
                    avg_price[s] = new_avg
                    
                    if curr_t >= 4:
                        deep_events.append({
                            "bar": b_idx,
                            "timestamp": str(ts),
                            "symbol": symbols[s],
                            "tier": curr_t,
                            "fill_price": float(lb_p),
                            "added_notional": float(lb_n),
                            "total_notional": float(new_n),
                            "avg_price": float(new_avg),
                            "tp_target_1": float(new_avg * 1.20),
                            "cur_equity": float(cur_eq)
                        })
                        
                    sell_active[s, :] = False
                    num_batches = min(curr_t, 3)
                    batch_q = new_q / num_batches
                    batch_n = new_n / num_batches
                    tp_targets = [0.20, 0.50, 1.00]
                    for slot in range(num_batches):
                        sell_active[s, slot] = True
                        sell_price[s, slot] = new_avg * (1.0 + tp_targets[slot])
                        sell_qty[s, slot] = batch_q
                        sell_notional[s, slot] = batch_n
                        
                    limit_buy_price[s] = lb_p * 0.50
                    limit_buy_notional[s] = new_n * 2.0
                    
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
                    if tier[s] >= 4:
                        closed_deep_trades.append({
                            "timestamp": str(ts),
                            "symbol": symbols[s],
                            "tier": int(tier[s]),
                            "slot": slot,
                            "sell_price": float(sp),
                            "pnl": float(pnl - fee)
                        })
                    
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
        print(f"LIQUIDATED AT {ts}! Equity: ${equity:.2f}, Maintenance Margin: ${mm:.2f}")
        break
        
    if hours[b_idx] == 8:
        free_m = max(0.0, equity - (tot_notional / leverage))
        for s in np.where(~is_open)[0]:
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

print(f"\nTotal Deep Tier Entry Events (Tier >= 4): {len(deep_events)}")
for e in deep_events:
    print(f"  {e['timestamp']} | {e['symbol']:<12} | Tier {e['tier']} | Buy: {e['fill_price']:<10.6f} | Added: {e['added_notional']:<6.0f}U | TotalNotional: {e['total_notional']:<6.0f}U | AvgP: {e['avg_price']:<10.6f} | TP1: {e['tp_target_1']:<10.6f} | Eq: ${e['cur_equity']:<8.1f}")

print(f"\nClosed Deep Trades (Tier >= 4): {len(closed_deep_trades)}")
for c in closed_deep_trades:
    print(f"  {c['timestamp']} | {c['symbol']:<12} | Tier {c['tier']} | Slot: {c['slot']} | Sell: {c['sell_price']:<10.6f} | PnL: ${c['pnl']:<8.2f}")
