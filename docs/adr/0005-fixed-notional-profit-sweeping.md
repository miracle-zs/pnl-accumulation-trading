# 0005. Fixed Notional Sizing with Periodic Profit Sweeping

We keep Tier 1-3 position sizing strictly fixed at 10U, 20U, and 60U notional, with profits periodically swept out of the trading account rather than compounded. In mean-reverting martingale and grid strategies, compounding equity inflates absolute nominal risk exposure at cycle tops, creating disproportionate dollar drawdowns during sharp market-wide corrections.
