# 0003. Direct Exchange Native Limit Orders over Virtual Shadow Engine

To minimize execution latency, prevent local process failure risks, and maximize maker fee rebates, we submit all DCA buys and batch take-profit sells directly as native Binance USDT-M LIMIT orders. Binance enforces a per-symbol limit of 200 open orders (`MAX_NUM_ORDERS`) and an account-wide limit of 10,000 open orders; running 200 concurrent symbols with 2-4 orders each generates only 400-600 open orders, well within exchange infrastructure capacity without requiring a complex local virtual order engine.
