# 0008. Global REST Reconciliation on WebSocket Disconnect

Upon any User Data Stream WebSocket disconnection or reconnection, the trading daemon invokes single-call account-wide endpoints (`GET /fapi/v1/openOrders` and `GET /fapi/v1/positionRisk`) to reconcile all 400-600 orders and 200 positions against the local state machine. This replaces iterative per-symbol polling, preventing API rate-limit exhaustion while ensuring zero state drift across network disruptions.
