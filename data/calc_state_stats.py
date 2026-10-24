import numpy as np
import pandas as pd

d = np.load('data/cache_15m_all_crypto.npz')
bars = d['bars']
symbols = list(d['symbols'])
num_bars, num_symbols, _ = bars.shape

initial_notional = 10.0
taker_fee = 0.0005
t2_notional = 20.0
t3_notional = 60.0
leverage = 10.0
initial_capital_scenarios = (1500.0, 2000.0)

is_open = np.zeros(num_symbols, dtype=bool)
tier = np.zeros(num_symbols, dtype=np.int32)
qty = np.zeros(num_symbols, dtype=np.float64)
cost = np.zeros(num_symbols, dtype=np.float64)
avg_price = np.zeros(num_symbols, dtype=np.float64)

entry_t1 = np.zeros(num_symbols, dtype=np.float64)
entry_t2 = np.zeros(num_symbols, dtype=np.float64)
entry_t3 = np.zeros(num_symbols, dtype=np.float64)
writeoff_price = np.zeros(num_symbols, dtype=np.float64)

t2_init_qty = np.zeros(num_symbols, dtype=np.float64)
t3_init_qty = np.zeros(num_symbols, dtype=np.float64)

trade_records = []
dca_records = []
full_close_types = {
    "WRITE_OFF (注销清算)",
    "T1 止盈 (+10%)",
    "T2 止盈全平 (+50%)",
    "T2.1 止盈全平 (+50%)",
    "T3 第三次止盈全平 (+100%)",
    "T3.1 第三次止盈全平 (+100%)",
    "T3.2 第三次止盈全平 (+100%)",
}

p0 = bars[0, :, 0]
for s in range(num_symbols):
    p = p0[s]
    if not np.isnan(p) and p > 0:
        is_open[s] = True
        tier[s] = 1
        qty[s] = initial_notional / p
        cost[s] = initial_notional
        avg_price[s] = p
        entry_t1[s] = p

initial_open_count = int(np.count_nonzero(is_open))
last_mark = np.where(np.isfinite(p0) & (p0 > 0), p0, np.nan)
wallet_change = -initial_open_count * initial_notional * taker_fee
equity_change_history = [0.0]
margin_used_history = [0.0]

for b in range(num_bars):
    c_bar = bars[b, :, 3]
    trade_start = len(trade_records)
    dca_start = len(dca_records)

    for s in np.where(is_open)[0]:
        c = c_bar[s]
        if np.isnan(c) or c <= 0: continue

        curr_t = tier[s]
        cur_q = qty[s]
        cur_avg = avg_price[s]
        ret = (c - cur_avg) / cur_avg

        # 1. Option 1 Write-off check
        if curr_t in (3, 31, 32) and writeoff_price[s] > 0 and c <= writeoff_price[s]:
            pnl = (c - cur_avg) * cur_q
            fee = c * cur_q * taker_fee
            trade_records.append({
                "type": "WRITE_OFF (注销清算)",
                "tier": curr_t,
                "pnl": pnl - fee,
                "fee": fee,
                "ret": ret,
                "symbol": symbols[s]
            })
            qty[s] = initial_notional / c
            cost[s] = initial_notional
            avg_price[s] = c
            entry_t1[s] = c
            tier[s] = 1
            writeoff_price[s] = 0.0
            continue

        # 2. Tier 1
        if curr_t == 1:
            if c <= entry_t1[s] * 0.50:
                add_q = t2_notional / c
                fee = t2_notional * taker_fee
                qty[s] += add_q
                cost[s] += t2_notional
                avg_price[s] = (initial_notional + t2_notional) / qty[s]
                entry_t2[s] = c
                tier[s] = 2
                t2_init_qty[s] = qty[s]
                dca_records.append({"type": "DCA_T1_TO_T2", "cost": t2_notional, "symbol": symbols[s]})
            elif ret >= 0.10:
                pnl = (c - cur_avg) * cur_q
                fee = c * cur_q * taker_fee
                trade_records.append({
                    "type": "T1 止盈 (+10%)",
                    "tier": 1,
                    "pnl": pnl - fee,
                    "fee": fee,
                    "ret": ret,
                    "symbol": symbols[s]
                })
                qty[s] = initial_notional / c
                cost[s] = initial_notional
                avg_price[s] = c
                entry_t1[s] = c
                tier[s] = 1

        # 3. Tier 2 & Tier 2.1
        elif curr_t in (2, 21):
            if c <= entry_t2[s] * 0.50:
                add_q = t3_notional / c
                qty[s] += add_q
                cost[s] += t3_notional
                avg_price[s] = cost[s] / qty[s]
                entry_t3[s] = c
                writeoff_price[s] = 5.0 / qty[s]
                tier[s] = 3
                t3_init_qty[s] = qty[s]
                dca_records.append({"type": "DCA_T2_TO_T3", "cost": t3_notional, "symbol": symbols[s]})
            elif curr_t == 2:
                if ret >= 0.50:
                    pnl = (c - cur_avg) * cur_q
                    fee = c * cur_q * taker_fee
                    trade_records.append({
                        "type": "T2 止盈全平 (+50%)",
                        "tier": 2,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] = initial_notional / c
                    cost[s] = initial_notional
                    avg_price[s] = c
                    entry_t1[s] = c
                    tier[s] = 1
                elif ret >= 0.20:
                    close_q = t2_init_qty[s] * 0.5
                    pnl = (c - cur_avg) * close_q
                    fee = c * close_q * taker_fee
                    trade_records.append({
                        "type": "T2 第一次止盈 (+20% 平50%)",
                        "tier": 2,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] -= close_q
                    cost[s] -= (cost[s] * 0.5)
                    tier[s] = 21
            elif curr_t == 21:
                if ret >= 0.50:
                    pnl = (c - cur_avg) * cur_q
                    fee = c * cur_q * taker_fee
                    trade_records.append({
                        "type": "T2.1 止盈全平 (+50%)",
                        "tier": 21,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] = initial_notional / c
                    cost[s] = initial_notional
                    avg_price[s] = c
                    entry_t1[s] = c
                    tier[s] = 1

        # 4. Tier 3
        elif curr_t in (3, 31, 32):
            if curr_t == 3:
                if ret >= 1.00:
                    pnl = (c - cur_avg) * cur_q
                    fee = c * cur_q * taker_fee
                    trade_records.append({
                        "type": "T3 第三次止盈全平 (+100%)",
                        "tier": 3,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] = initial_notional / c
                    cost[s] = initial_notional
                    avg_price[s] = c
                    entry_t1[s] = c
                    tier[s] = 1
                elif ret >= 0.50:
                    close_q = (t3_init_qty[s] / 3.0) * 2
                    pnl = (c - cur_avg) * close_q
                    fee = c * close_q * taker_fee
                    trade_records.append({
                        "type": "T3 第二次止盈 (+50% 平2/3)",
                        "tier": 3,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] -= close_q
                    cost[s] -= (cost[s] * (2/3))
                    tier[s] = 32
                elif ret >= 0.20:
                    close_q = t3_init_qty[s] / 3.0
                    pnl = (c - cur_avg) * close_q
                    fee = c * close_q * taker_fee
                    trade_records.append({
                        "type": "T3 第一次止盈 (+20% 平1/3)",
                        "tier": 3,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] -= close_q
                    cost[s] -= (cost[s] * (1/3))
                    tier[s] = 31
            elif curr_t == 31:
                if ret >= 1.00:
                    pnl = (c - cur_avg) * cur_q
                    fee = c * cur_q * taker_fee
                    trade_records.append({
                        "type": "T3.1 第三次止盈全平 (+100%)",
                        "tier": 31,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] = initial_notional / c
                    cost[s] = initial_notional
                    avg_price[s] = c
                    entry_t1[s] = c
                    tier[s] = 1
                elif ret >= 0.50:
                    close_q = t3_init_qty[s] / 3.0
                    pnl = (c - cur_avg) * close_q
                    fee = c * close_q * taker_fee
                    trade_records.append({
                        "type": "T3.1 第二次止盈 (+50% 平1/3)",
                        "tier": 31,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] -= close_q
                    cost[s] -= (cost[s] * 0.5)
                    tier[s] = 32
            elif curr_t == 32:
                if ret >= 1.00:
                    pnl = (c - cur_avg) * cur_q
                    fee = c * cur_q * taker_fee
                    trade_records.append({
                        "type": "T3.2 第三次止盈全平 (+100%)",
                        "tier": 32,
                        "pnl": pnl - fee,
                        "fee": fee,
                        "ret": ret,
                        "symbol": symbols[s]
                    })
                    qty[s] = initial_notional / c
                    cost[s] = initial_notional
                    avg_price[s] = c
                    entry_t1[s] = c
                    tier[s] = 1

    valid_close = np.isfinite(c_bar) & (c_bar > 0)
    last_mark[valid_close] = c_bar[valid_close]
    bar_trades = trade_records[trade_start:]
    bar_dcas = dca_records[dca_start:]
    wallet_change += sum(record["pnl"] for record in bar_trades)
    wallet_change -= sum(record["cost"] * taker_fee for record in bar_dcas)
    wallet_change -= sum(
        initial_notional * taker_fee
        for record in bar_trades
        if record["type"] in full_close_types
    )

    marked_positions = is_open & (qty > 0) & np.isfinite(last_mark)
    floating_pnl = np.sum((last_mark[marked_positions] - avg_price[marked_positions]) * qty[marked_positions])
    gross_notional = np.sum(last_mark[marked_positions] * qty[marked_positions])
    equity_change_history.append(wallet_change + floating_pnl)
    margin_used_history.append(gross_notional / leverage)

df_trades = pd.DataFrame(trade_records)

# Every market buy pays taker fees: initial/reopened T1 entries and both DCA tiers.
t1_reopen_count = int(df_trades["type"].isin(full_close_types).sum())
dca_notional = sum(record["cost"] for record in dca_records)
entry_fees_paid = (
    (initial_open_count + t1_reopen_count) * initial_notional + dca_notional
) * taker_fee
exit_fees_paid = df_trades["fee"].sum()
total_fees_paid = entry_fees_paid + exit_fees_paid
take_profit_count = int(df_trades["type"].str.contains("止盈", na=False).sum())

# 1. Closed Trades Breakdown
print("=== 1. 已实现交易（平仓/止盈/注销）统计分布 ===")
trade_summary = []
for t_type, group in df_trades.groupby("type"):
    count = len(group)
    tot_pnl = group["pnl"].sum()
    avg_pnl = group["pnl"].mean()
    tot_fee = group["fee"].sum()
    avg_ret = group["ret"].mean() * 100
    trade_summary.append({
        "type": t_type,
        "count": count,
        "tot_pnl": tot_pnl,
        "avg_pnl": avg_pnl,
        "avg_ret": avg_ret,
        "tot_fee": tot_fee
    })

df_summary = pd.DataFrame(trade_summary).sort_values("tot_pnl", ascending=False)
header = "{:<30} | {:>6} | {:>12} | {:>10} | {:>10} | {:>8}".format("平仓状态类型", "次数", "实现总净额", "单笔均值", "平均收益率", "总手续费")
print(header)
print("-" * 92)
for _, r in df_summary.iterrows():
    print("{:<30} | {:>6} | ${:>11.2f} | ${:>9.2f} | {:>9.1f}% | ${:>7.2f}".format(
        r['type'], int(r['count']), r['tot_pnl'], r['avg_pnl'], r['avg_ret'], r['tot_fee']
    ))
print("-" * 92)
tot_c = df_trades["pnl"].sum() - entry_fees_paid
print(f"止盈次数: {take_profit_count:,} | 补仓次数: {len(dca_records):,} | 注销次数: {(df_trades['type'] == 'WRITE_OFF (注销清算)').sum():,}")
print("手续费：开仓/补仓 ${:,.2f}，平仓 ${:,.2f}，合计 ${:,.2f}".format(
    entry_fees_paid, exit_fees_paid, total_fees_paid
))
print("已实现净现金汇总（扣除全部手续费）: ${:,.2f}".format(tot_c))

# 2. Final Open Positions Breakdown
print("\n=== 2. 期末未结持仓状态（浮动盈亏）分布 ===")
last_c = bars[-1, :, 3]
holdings = []
for s in np.where(is_open)[0]:
    c = last_c[s]
    t = tier[s]
    flt = (c - avg_price[s]) * qty[s]
    name_map = {1: "T1 (底仓10U)", 2: "T2 (持仓30U)", 21: "T2.1 (半仓15U)", 3: "T3 (持仓90U)", 31: "T3.1 (持仓60U)", 32: "T3.2 (持仓30U)"}
    t_name = name_map.get(t, f"Tier {t}")
    holdings.append({
        "symbol": symbols[s],
        "tier": t,
        "tier_name": t_name,
        "cost": cost[s],
        "market_val": c * qty[s],
        "floating": flt,
        "ret": (c - avg_price[s]) / avg_price[s] * 100
    })

df_holdings = pd.DataFrame(holdings)
h_summary = []
for t_name, group in df_holdings.groupby("tier_name"):
    count = len(group)
    tot_cost = group["cost"].sum()
    tot_mv = group["market_val"].sum()
    tot_flt = group["floating"].sum()
    avg_flt = group["floating"].mean()
    avg_ret = group["ret"].mean()
    h_summary.append({
        "tier_name": t_name,
        "count": count,
        "tot_cost": tot_cost,
        "tot_mv": tot_mv,
        "tot_flt": tot_flt,
        "avg_flt": avg_flt,
        "avg_ret": avg_ret
    })

df_h_sum = pd.DataFrame(h_summary).sort_values("tot_cost", ascending=False)
header_h = "{:<20} | {:>6} | {:>10} | {:>10} | {:>12} | {:>10} | {:>10}".format(
    "当前持仓状态", "币种数", "名义总成本", "当前总市值", "总未结浮亏", "单币均浮亏", "平均跌幅"
)
print(header_h)
print("-" * 96)
for _, r in df_h_sum.iterrows():
    print("{:<20} | {:>6} | ${:>9.1f} | ${:>9.1f} | ${:>11.2f} | ${:>9.2f} | {:>9.1f}%".format(
        r['tier_name'], int(r['count']), r['tot_cost'], r['tot_mv'], r['tot_flt'], r['avg_flt'], r['avg_ret']
    ))
print("-" * 96)
print("期末未结浮亏汇总: ${:,.2f}".format(df_holdings['floating'].sum()))
print("总净利润 (已实现现金 + 期末浮亏): ${:,.2f}".format(tot_c + df_holdings['floating'].sum()))

print("\n=== 3. 账户权益与名义保证金估算 ===")
equity_changes = np.asarray(equity_change_history)
margin_used = np.asarray(margin_used_history)
minimum_starting_capital = max(0.0, float(np.max(margin_used - equity_changes)))
print(
    "按 10x 名义初始保证金、不留额外储备，覆盖这条完整交易路径所需的起始资金下限估算: "
    f"${minimum_starting_capital:,.2f}（不等于实盘安全本金）"
)
for starting_capital in initial_capital_scenarios:
    equity_curve = starting_capital + equity_changes
    high_water_mark = np.maximum.accumulate(equity_curve)
    drawdown = equity_curve - high_water_mark
    drawdown_pct = np.divide(
        drawdown,
        high_water_mark,
        out=np.zeros_like(drawdown),
        where=high_water_mark > 0,
    )
    free_equity = equity_curve - margin_used
    positive_equity = equity_curve > 0
    margin_utilization = np.divide(
        margin_used,
        equity_curve,
        out=np.full_like(margin_used, np.inf),
        where=positive_equity,
    )
    max_utilization = np.max(margin_utilization)
    utilization_text = (
        "权益曾非正，比例无意义"
        if np.any(~positive_equity)
        else f"{max_utilization * 100:.1f}%"
    )
    print(
        f"起始资金 ${starting_capital:,.0f}: 期末权益 ${equity_curve[-1]:,.2f} | "
        f"最低权益 ${equity_curve.min():,.2f} | 最大回撤 ${-drawdown.min():,.2f} "
        f"({-drawdown_pct.min() * 100:.1f}%) | 最大名义保证金估算 ${margin_used.max():,.2f} | "
        f"最低可用权益 ${free_equity.min():,.2f} | 最大保证金/权益 {utilization_text}"
    )
