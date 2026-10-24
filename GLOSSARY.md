# PnL Accumulation Trading Context

This context governs the multi-symbol crypto futures accumulation and grid-martingale strategy on Binance USDT-M.

## Language

**Tier (阶梯)**:
A discrete capital allocation level (Tier 1 = 10U, Tier 2 = 20U, Tier 3 = 60U) triggered when a contract price drops by 50% relative to the prior entry price.
_Avoid_: Layer, stage, level, step

**Devaluation Recycling (市值缩水循环)**:
A risk-controlled recovery mechanism that activates only after Tier 3 is reached and the remaining position's market value drops below 5.0 USDT (-96.5% drawdown from origin), entering a secondary 20U/60U DCA cycle.
_Avoid_: Infinite Martingale, bottom-fishing, averaging down

**Bottom Fresh Pulse TP (底部脉冲止盈)**:
A take-profit rule for recycled additions calculated strictly against the latest entry price (+20% / +50%), exiting quickly without being dragged down by historical high-altitude average price.
_Avoid_: Unified average TP, global TP

**Static Order Lifecycle (方案 A 静态挂单)**:
A policy where pending limit take-profit orders remain untouched on the order book until either hit or cancelled upon falling into the next lower Tier DCA buy.
_Avoid_: Break-even stop, trailing stop, stop-loss

**Rebalance (每日调仓/补仓)**:
The daily 08:00 UTC check that opens new Tier 1 positions on idle candidate symbols up to the available margin capacity.
_Avoid_: Reset, re-entry, reshuffle

**Equity Cushion (沉淀安全垫)**:
The organic accumulation of realized profits retained inside the futures wallet to automatically absorb drawdowns during tail-risk market events.
_Avoid_: Surplus capital, unextracted profit, idle cash

**Delisting Hold-Through (停牌死扛)**:
The operational policy of keeping limit take-profit orders active until trading suspension, settling residual positions via exchange index delivery rather than market-closing.
_Avoid_: Delisting panic sell, manual liquidation

**State Reconciliation (全局状态对齐)**:
The single-batch synchronization routine executed upon WebSocket reconnection using account-wide REST endpoints to align local state with exchange reality.
_Avoid_: Order polling, status sync, cache refresh

**Min-Notional Merging (残单合批)**:
The automatic consolidation of fractional take-profit batch orders into a single 100% order when individual batches fall below Binance's 5.0 USDT threshold.
_Avoid_: Dust handling, rounding up, order skipping

**Volume Fallback (成交额降级)**:
The automatic switch to sorting Binance active futures by lowest 24h quote volume whenever the external CoinMarketCap API fails.
_Avoid_: Random fill, default fallback, cache retry

**WAL State Store (预写日志状态库)**:
The single-file SQLite database configured with Write-Ahead Logging to provide crash-proof ACID persistence without external service dependencies.
_Avoid_: Flat JSON, local cache, external DB

**Asset Classification Directory (资产分类目录)**:
A versioned, reviewed mapping from a contract's base asset to `NATIVE_CRYPTO`, `NON_CRYPTO`, or `UNKNOWN`, with a source and review time. Missing classifications remain `UNKNOWN` until reviewed.
_Avoid_: Symbol-name heuristic, suffix allowlist

**Eligible Contract (合格合约)**:
A currently trading USDT-margined USDT-quoted perpetual whose base asset is reviewed as native crypto and whose market-order quantity rules are usable. Eligibility is separate from account risk authorization.
_Avoid_: Admitted position, all exchange symbols

**Admitted Symbol (准入标的)**:
A symbol whose current observation permits opening a new Tier 1 position; the account risk gate still authorizes each order independently.
_Avoid_: Guaranteed order approval, risk-free position

**Scale-In Authorization (增仓授权)**:
A per-symbol permission to make a DCA increase; an exit-only or unreviewed symbol does not receive this permission, even if an existing position remains managed.
_Avoid_: New-entry admission, account budget approval

**Managed Symbol (托管标的)**:
A symbol that remains in review because it has an active local strategy lifecycle, a non-zero exchange position, or an unresolved order intent, even if it is no longer eligible for new capital.
_Avoid_: Universe candidate, active contract only

**Exit-Only Directive (仅减仓指令)**:
A reviewed contract-level instruction that blocks opening, DCA, and reopening while allowing strategy reductions when the exchange still permits them.
_Avoid_: Automatic delisting prediction, forced market close
