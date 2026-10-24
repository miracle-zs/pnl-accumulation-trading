# 0009. Min-Notional Batch Merging for Deep Drawdown Orders

When calculated take-profit batch orders drop below the Binance 5.0 USDT minimum notional threshold (`minNotional`), we automatically merge the remaining sub-batches into a single 100% position limit order rather than purchasing extra coins to satisfy the limit. If the entire symbol position value is still below 5.0 USDT, the order is managed by the local event loop and submitted as an aggressive market close once the target trigger price is breached.
