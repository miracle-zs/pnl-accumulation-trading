# 0002. Static Order Lifecycle (Option A) over Break-even Stop (Option B)

To maximize capture of high-payoff mean-reversion bounces (+50%), we keep partial take-profit limit orders open on the exchange without trailing break-even stops. Backtests across 200 Binance USDT-M symbols revealed that break-even stops were triggered 142 times by normal re-tests, cutting high-margin runners short and reducing net return from +285.0% to +233.5% without improving maximum drawdown (-54.1%).
