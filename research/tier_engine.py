"""
Parameterized Tier-3-capped grid engine.

The execution body is a faithful copy of the frozen production engine in
strategy_final.py; every knob below is inert at its default, so the baseline
reproduces strategy_final.py exactly: $3,867.85 / +286.78% / maxDD -54.06%.

This is the shared engine for the research scripts (simulate_compounding.py,
simulate_entry_selection.py). Keep strategy_final.py as the frozen reference.

Position sizing / costs
  compound            size each NEW grid cycle by equity / initial_balance
  compound_cap        clamp the scale factor from above (inf = uncapped)
  compound_min        1.0 = ratchet (never shrink); <1 lets size de-risk in DD
  scale_mv_thresholds keep the 10U/5U devaluation thresholds proportional
  funding_hourly      per-hour funding paid on open long notional

Universe selection
  universe_symbols    explicit symbol whitelist (caller decides, no look-ahead
                      decisions inside the engine)
  n_symbols           0 = all; else restrict to the first N columns

Entry cadence / timing
  rebalance_hours         UTC hours at which idle slots are refilled (default [8])
  rebalance_every_n_days  daily (1), every 2 days, weekly (7), ...
  max_new_per_rebalance   0 = unlimited; else throttle new Tier-1 opens
  phase_in_days           spread the initial deployment over N days instead of
                          opening the whole universe on bar 0
  entry_drop_lookback_h   only open if price is down over this lookback
  entry_drop_min          ... by at least this fraction (0 = filter off)

Diagnostics
  bar_shift           rotate the time axis (path-dependency stress test)
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

    # --- experiment knobs (inert at defaults => frozen engine behaviour) ---
    compound: bool = False
    compound_cap: float = float("inf")
    compound_min: float = 1.0
    scale_mv_thresholds: bool = True
    funding_hourly: float = 0.0
    funding_matrix: object = None   # (num_bars, num_symbols) real hourly rates; overrides funding_hourly
    bar_shift: int = 0   # rotate the time axis (robustness / path-dependency test)
    n_symbols: int = 0   # 0 = all; else restrict to the first N columns

    # --- universe selection ---
    universe_symbols: tuple = ()   # explicit whitelist; empty = all columns
    start_bar: int = 0             # skip a burn-in window (selection made out-of-sample)
    cache_prefix: str = ""         # "" = frozen bars_cache.npy; "_full" = whole pool
    max_open: int = 0              # 0 = no cap on concurrent positions
    rank_matrix: object = None     # (num_bars, num_symbols) selection score, higher = better

    # --- entry cadence / timing ---
    rebalance_hours: tuple = (8,)
    rebalance_every_n_days: int = 1
    max_new_per_rebalance: int = 0     # 0 = unlimited
    phase_in_days: int = 0             # 0 = open whole universe on bar 0
    entry_drop_lookback_h: int = 0     # 0 = entry filter off
    entry_drop_min: float = 0.0        # require >= this drop over the lookback
    cooldown_hours: int = 0            # hours to wait after full close before re-opening
    reentry_pullback_pct: float = 0.0  # require price <= last_exit_price * (1 - pct)

    def scale_for(self, equity: float) -> float:
        if not self.compound:
            return 1.0
        return max(self.compound_min, min(self.compound_cap, equity / self.initial_balance))

def run_strategy(config: StrategyConfig = StrategyConfig(), data_dir: str = "data"):
    data_path = Path(data_dir)
    bars = np.load(data_path / f"bars_cache{config.cache_prefix}.npy")
    info = json.loads((data_path / f"bars_info{config.cache_prefix}.json").read_text())
    hours = np.array(info["hours"], dtype=np.int32)
    if config.bar_shift:
        bars = np.roll(bars, config.bar_shift, axis=0)
        hours = np.roll(hours, config.bar_shift)
    symbols = info["symbols"]
    fund = None
    if config.funding_matrix is not None:
        fund = np.asarray(config.funding_matrix, dtype=np.float64)
        if config.bar_shift:
            fund = np.roll(fund, config.bar_shift, axis=0)
    rank = None
    if config.rank_matrix is not None:
        rank = np.asarray(config.rank_matrix, dtype=np.float64)
        if config.bar_shift:
            rank = np.roll(rank, config.bar_shift, axis=0)
    if config.n_symbols:
        bars = bars[:, :config.n_symbols, :]
        if fund is not None:
            fund = fund[:, :config.n_symbols]
        if rank is not None:
            rank = rank[:, :config.n_symbols]
        symbols = symbols[:config.n_symbols]
    if config.universe_symbols:
        keep = [i for i, s in enumerate(symbols) if s in set(config.universe_symbols)]
        # de-duplicate identical symbol names (bars_info.json contains a dup)
        seen, uniq = set(), []
        for i in keep:
            if symbols[i] in seen:
                continue
            seen.add(symbols[i])
            uniq.append(i)
        bars = bars[:, uniq, :]
        if fund is not None:
            fund = fund[:, uniq]
        if rank is not None:
            rank = rank[:, uniq]
        symbols = [symbols[i] for i in uniq]
    if config.start_bar:
        bars = bars[config.start_bar:]
        hours = hours[config.start_bar:]
        if fund is not None:
            fund = fund[config.start_bar:]
        if rank is not None:
            rank = rank[config.start_bar:]
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
    mdd_bar = 0
    
    last_p = np.zeros(num_symbols, dtype=np.float64)
    sym_scale = np.ones(num_symbols, dtype=np.float64)
    last_exit_bar = np.full(num_symbols, -99999, dtype=np.int64)
    last_exit_price = np.zeros(num_symbols, dtype=np.float64)
    history = []
    recycled_events = []
    reentry_events = []
    sym_realized = np.zeros(num_symbols, dtype=np.float64)
    sym_fees = np.zeros(num_symbols, dtype=np.float64)
    sym_trades = np.zeros(num_symbols, dtype=np.int64)
    sym_opens = np.zeros(num_symbols, dtype=np.int64)
    dca_filled = 0
    dca_rejected = 0
    tier1_opens = 0
    max_scale_seen = 1.0
    peak_notional = 0.0
    funding_paid = 0.0
    
    def entry_ok(s, b_idx):
        """Mean-reversion entry gate: cooldown and price drop requirements."""
        if config.cooldown_hours > 0 and last_exit_bar[s] >= 0:
            if (b_idx - last_exit_bar[s]) < config.cooldown_hours:
                return False
        if config.reentry_pullback_pct > 0 and last_exit_price[s] > 0:
            p_now = bars[b_idx, s, 0]
            if np.isnan(p_now) or p_now > last_exit_price[s] * (1.0 - config.reentry_pullback_pct):
                return False
        if config.entry_drop_lookback_h <= 0:
            return True
        lb = b_idx - config.entry_drop_lookback_h
        if lb < 0:
            return False                    # wait until enough history exists
        p_now = bars[b_idx, s, 0]
        p_ref = bars[lb, s, 3]
        if np.isnan(p_now) or np.isnan(p_ref) or p_ref <= 0:
            return False
        return (p_now / p_ref - 1.0) <= -config.entry_drop_min

    def open_tier1(s, p, sc, b_idx):
        nonlocal cash_balance, tier1_opens
        if last_exit_bar[s] >= 0:
            reentry_events.append({
                "symbol": symbols[s],
                "bar": b_idx,
                "delta_h": int(b_idx - last_exit_bar[s]),
                "exit_p": float(last_exit_price[s]),
                "entry_p": float(p),
                "ratio": float(p / last_exit_price[s]) if last_exit_price[s] > 0 else 1.0
            })
        init_n = config.initial_notional * sc
        cash_balance -= init_n * config.taker_fee
        sym_fees[s] += init_n * config.taker_fee
        q = init_n / p
        is_open[s] = True
        tier[s] = 1
        qty[s] = q
        size_notional[s] = init_n
        avg_price[s] = p
        sym_scale[s] = sc
        limit_buy_price[s] = p * (1.0 - config.t2_buy_drop)
        limit_buy_notional[s] = config.t2_notional * sc
        sell_active[s, :] = False
        sell_active[s, 0] = True
        sell_price[s, 0] = p * config.t1_tp_ratio
        sell_qty[s, 0] = q
        sell_notional[s, 0] = init_n
        armed_10u[s] = False
        cycle[s] = 0
        sym_opens[s] += 1
        tier1_opens += 1
        return init_n / config.leverage   # initial margin consumed

    # Initial deployment schedule (spread over phase_in_days if requested)
    phase_bars = config.phase_in_days * 24
    deploy_bar = np.full(num_symbols, -1, dtype=np.int64)
    for s in range(num_symbols):
        if not np.isnan(bars[0, s, 0]):
            deploy_bar[s] = (s * phase_bars) // max(1, num_symbols) if phase_bars > 0 else 0

    equity = config.initial_balance

    def free_slots():
        if not config.max_open:
            return num_symbols
        return max(0, config.max_open - int(is_open.sum()))

    def ranked_candidates(b_idx):
        """Idle, eligible symbols at this bar, best-ranked first.

        With rank=None this preserves the original column order exactly, so the
        frozen baseline is unchanged.
        """
        out = [s for s in np.where(~is_open)[0]
               if deploy_bar[s] <= b_idx
               and not np.isnan(bars[b_idx, s, 0])
               and entry_ok(s, b_idx)]
        if rank is not None:
            row = rank[b_idx]
            out.sort(key=lambda s: (-row[s] if np.isfinite(row[s]) else np.inf))
        return out

    def open_some(b_idx, free_m, limit=0):
        """Open eligible symbols until slots/margin/throttle run out."""
        nonlocal max_scale_seen
        sc = config.scale_for(equity)
        opened = 0
        for s in ranked_candidates(b_idx):
            if limit and opened >= limit:
                break
            if free_slots() <= 0:
                break
            if free_m < config.initial_notional * sc / config.leverage:
                break
            free_m -= open_tier1(s, bars[b_idx, s, 0], sc, b_idx)
            opened += 1
            max_scale_seen = max(max_scale_seen, sc)
        return opened

    for b_idx in range(num_bars):
        c_bar = bars[b_idx, :, 3]
        valid_c = ~np.isnan(c_bar)
        last_p[valid_c] = c_bar[valid_c]
        o_bar = bars[b_idx, :, 0]
        h_bar = bars[b_idx, :, 1]
        l_bar = bars[b_idx, :, 2]
        ts = pd.Timestamp("2026-01-01") + pd.Timedelta(hours=b_idx)

        # --- initial / scheduled deployment ---
        if b_idx == 0 or np.any(deploy_bar == b_idx):
            tot_n = float(np.sum(last_p[is_open] * qty[is_open])) if np.any(is_open) else 0.0
            open_some(b_idx, max(0.0, equity - tot_n / config.leverage))

        
        open_indices = np.where(is_open)[0]
        for s in open_indices:
            o = o_bar[s]
            if np.isnan(o): continue
            h, l, c = h_bar[s], l_bar[s], c_bar[s]
            
            # Devaluation check
            if config.recycling_enabled and tier[s] == 3 and cycle[s] < config.max_recycled_cycles:
                mv_arm = config.mv_arm_threshold * (sym_scale[s] if config.scale_mv_thresholds else 1.0)
                mv_tgt = config.mv_trigger_target * (sym_scale[s] if config.scale_mv_thresholds else 1.0)
                mv_low = l * qty[s]
                if not armed_10u[s] and mv_low <= mv_arm:
                    armed_10u[s] = True
                    p_trigger = mv_tgt / qty[s]
                    limit_buy_price[s] = p_trigger
                    limit_buy_notional[s] = config.recycled_t2_notional * sym_scale[s]
                    
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
                    
                    if free_m < margin_needed:
                        dca_rejected += 1
                    if free_m >= margin_needed:
                        dca_filled += 1
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
                                limit_buy_notional[s] = config.t3_notional * sym_scale[s]
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
                                limit_buy_notional[s] = config.recycled_t3_notional * sym_scale[s]
                            else:
                                limit_buy_price[s] = 0.0
                                limit_buy_notional[s] = 0.0
                                armed_10u[s] = False
                                
                # Limit sells check (Option A: static maker orders)
                last_sp = 0.0
                for slot in range(max_slots):
                    if sell_active[s, slot] and h >= sell_price[s, slot]:
                        sp = sell_price[s, slot]
                        last_sp = sp
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
                        sym_realized[s] += (pnl - fee)
                        sym_fees[s] += fee
                        sym_trades[s] += 1
                        
                if not np.any(sell_active[s, :]) or qty[s] <= 1e-6:
                    is_open[s] = False
                    tier[s] = 0
                    qty[s] = 0.0
                    limit_buy_price[s] = 0.0
                    armed_10u[s] = False
                    cycle[s] = 0
                    last_exit_bar[s] = b_idx
                    if last_sp > 0:
                        last_exit_price[s] = last_sp
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
            
        if fund is not None:
            if b_idx < fund.shape[0]:
                charge = float(np.sum(fund[b_idx][is_open] * last_p[is_open] * qty[is_open]))
                cash_balance -= charge
                funding_paid += charge
        else:
            cash_balance -= tot_notional * config.funding_hourly
            funding_paid += tot_notional * config.funding_hourly
        peak_notional = max(peak_notional, tot_notional)
        equity = cash_balance + tot_upnl
        mm = tot_notional * config.mmr
        
        if equity <= mm:
            is_liquidated = True
            liquidation_time = str(ts)
            break
            
        if equity > peak_equity: peak_equity = equity
        dd = (equity - peak_equity) / peak_equity
        if dd < max_drawdown:
            max_drawdown = dd
            mdd_bar = b_idx
        if equity < lowest_equity: lowest_equity = equity
        history.append(equity)
        
        # Scheduled rebalance: refill idle slots on the configured cadence
        if (hours[b_idx] in config.rebalance_hours
                and (b_idx // 24) % max(1, config.rebalance_every_n_days) == 0):
            free_m = max(0.0, equity - (tot_notional / config.leverage))
            open_some(b_idx, free_m, limit=config.max_new_per_rebalance)

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
        "history": history,
        "dca_filled": dca_filled,
        "dca_rejected": dca_rejected,
        "tier1_opens": tier1_opens,
        "max_scale_seen": max_scale_seen,
        "peak_notional": float(peak_notional),
        "open_at_end": int(is_open.sum()),
        "open_notional": float(np.sum(last_p[is_open] * qty[is_open])),
        "mdd_bar": int(mdd_bar),
        "funding_paid": float(funding_paid),
        "symbols": symbols,
        "sym_realized": sym_realized,
        "sym_fees": sym_fees,
        "sym_trades": sym_trades,
        "sym_opens": sym_opens,
        "final_equity_open": float(np.sum((last_p - avg_price) * qty * is_open)),
        "reentry_events": reentry_events,
    }

