# 0001. Market Value Devaluation Recycling over Uncapped Martingale

To handle catastrophic drawdown in extreme altcoin bear markets without triggering account ruin, we adopt a Devaluation Recycling mechanism (adding 20U only when Tier 3 market value shrinks below 5 USDT) rather than uncapped geometric martingale. Backtests showed uncapped martingale balloons single-coin exposure to 5,120 USDT causing 100% account liquidation, whereas devaluation recycling caps active risk at 25U~85U, yielding +286.8% return with zero liquidations across 200 symbols.
