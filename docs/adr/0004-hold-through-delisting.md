# 0004. Hold-Through Delisting Until Official Settlement

When a symbol receives a Binance delisting announcement, we maintain active grid and take-profit limit orders until trading halts, rather than executing immediate market-close liquidation. Extreme altcoins facing delisting frequently experience speculative short squeezes ("dead-cat surges") before contract expiration, allowing positions to hit take-profit levels, while remaining positions settle at the exchange index delivery price without aggressive taker market-impact slippage.
