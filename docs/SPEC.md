# Specification: Binance USDT-M PnL Accumulation Live Trading Engine

> **状态：历史规格，不作为当前实现依据。** 本文描述的是事件驱动、交易所限价挂单和循环补仓方案。当前 15 分钟定时市价巡检方案以 [PRD](PRD_15M_PERIODIC_REVIEW_TRADING.md) 和[架构设计](ARCHITECTURE_15M_PERIODIC_REVIEW_TRADING.md)为准。保留本文用于追溯旧方案与历史研究。

## Problem Statement

Automating a multi-symbol crypto futures portfolio across up to 200 altcoins presents severe capital preservation, exchange constraint, and lifecycle management challenges. Uncapped martingale approaches suffer from explosive position scaling (ballooning to over 5,000 USDT on single assets) that inevitably triggers 100% account liquidation during market-wide crashes. Conversely, traditional grid or static averaging bots trap margin in illiquid, dying altcoins without an exit mechanism. 

Furthermore, operating up to 200 concurrent symbols on Binance USDT-M encounters hard operational barriers: exchange rate limits, the 5.0 USDT `minNotional` filter on small batches, 200 open orders per symbol limits, WebSocket connection drops, sudden exchange leverage reductions ahead of token delistings, and third-party data feed downtime. Manual execution is impossible, and naive scripts fail under real-world exchange constraints.

## Solution

A production-grade, 7x24 autonomous event-driven execution daemon for Binance USDT-M that implements the mathematically validated **"Tier 3 Capped + Market Value Devaluation Recycling + Option A (Static Maker Orders)"** architecture. 

The system operates with invariant notional sizing (Tier 1 = 10U, Tier 2 = 20U, Tier 3 = 60U, 10x leverage) and holds pending take-profit orders statically on the exchange orderbook without premature break-even stops. When catastrophic altcoin depreciation occurs (-96.5%+), a devaluation recycling mechanism deploys a micro-cycle of 20U/60U to capture high-volatility oversold bounces with fresh entry take-profit targets. 

All orders are native Binance Limit orders (utilizing only 4%-6% of the account-wide 10,000 order limit). The system persists transactional state to an embedded SQLite database in WAL mode, reconciles state across WebSocket drops using single-call global REST endpoints, falls back to native Binance volume metrics if CoinMarketCap fails, and dispatches real-time alerts via ServerChan/Bark.

## User Stories

1. As a quant trader, I want the bot to open Tier 1 positions (10 USDT notional, 10x leverage) on candidate symbols during daily 08:00 UTC rebalance, so that idle account margin is systematically deployed into high-volatility micro-cap contracts.
2. As a quant trader, I want the bot to place a 100% position limit take-profit order at +20% immediately upon opening Tier 1, so that quick upside pulses are banked as Maker fills (0.02% fee).
3. As a quant trader, I want the bot to place a Tier 2 limit buy order at -50% below the Tier 1 entry price with 20 USDT notional, so that adverse price moves automatically average down entry costs.
4. As a quant trader, I want the bot to recalculate the weighted average cost price when Tier 2 fills, so that subsequent take-profit thresholds accurately reflect blended inventory cost.
5. As a quant trader, I want the bot to split Tier 2 take-profit into two equal 50% limit sell orders at +20% and +50% above the blended average price, so that half the position banks quick profit while the remaining half captures extended trend-reversal gains.
6. As a quant trader, I want the bot to place a Tier 3 limit buy order at -50% below the Tier 2 buy price with 60 USDT notional, so that secondary severe drops are met with disciplined, capped scaling.
7. As a quant trader, I want Tier 2 take-profit limit orders to remain static on the exchange without trailing break-even stops (Option A), so that normal post-bounce retests do not prematurely wash out high-margin runners.
8. As a quant trader, I want the bot to cancel pending Tier 2 take-profit limit orders immediately when Tier 3 executes, so that stale resting orders do not create naked short positions.
9. As a quant trader, I want the bot to split Tier 3 into three 1/3 limit sell orders at +20%, +50%, and +100% above the new blended average price, so that massive oversold rebounds capture high-payoff profits.
10. As a risk manager, I want standard DCA additions to hard-cap at Tier 3 (90 USDT cumulative notional), so that single-symbol exposure is strictly bounded and account margin can never be overwhelmed.
11. As a risk manager, I want the bot to monitor the real-time market value of Tier 3 positions, so that deeply underwater positions are tracked for devaluation recycling.
12. As a quant trader, I want the bot to trigger a 20 USDT recycled buy only when Tier 3 position market value falls below 5.0 USDT, so that capital is redeployed only after extreme (-96.5%+) value attrition.
13. As a quant trader, I want recycled additions to post bottom fresh pulse take-profit orders at +20% and +50% calculated strictly from the fresh buy price, so that dead-cat bounces are harvested immediately without waiting for historical break-even.
14. As a risk manager, I want devaluation recycling to cap at a maximum of 1 full cycle (20U + 60U), so that terminal delisting coins do not indefinitely absorb capital.
15. As a quant trader, I want the bot to hold contracts through delisting announcements until final exchange delivery settlement, so that late-stage short squeezes are not missed due to panic selling.
16. As an operator, I want the bot to continue DCA execution even if the exchange cuts leverage to 1x or 2x on a delisting coin (provided free margin permits), so that the recovery grid remains unbroken.
17. As an operator, I want take-profit orders with notional value under 5.0 USDT to automatically merge into a single 100% position limit order, so that orders comply with Binance `minNotional` filters without failing.
18. As an operator, I want the bot to maintain native exchange limit orders directly on Binance, so that all executions achieve Maker fee rebates (0.02%) and zero local latency.
19. As a systems engineer, I want the bot to reconcile all open orders and positions via single-call account-wide endpoints upon WebSocket reconnection, so that state drift is resolved in milliseconds without tripping API rate limits.
20. As a systems engineer, I want all state changes persisted to an embedded SQLite database in WAL mode, so that power failures or crashes recover instantly with zero state loss and zero database lockups.
21. As a systems engineer, I want daily 08:00 UTC symbol selection to query CoinMarketCap for the lowest market cap futures contracts, so that the universe aligns precisely with backtested alpha.
22. As an operator, I want symbol selection to automatically fall back to Binance 24h quote volume sorting if CoinMarketCap API fails, so that daily rebalancing never stalls.
23. As a portfolio manager, I want realized profits to accumulate organically inside the futures account without compounding position sizing, so that the growing equity cushion provides safety against portfolio drawdowns.
24. As a mobile operator, I want asynchronous alerts sent to ServerChan (and optionally Bark) for Tier transitions, recycling triggers, and daily equity summaries, so that I maintain complete operational awareness on mobile.

## Implementation Decisions

### 1. System Subsystems and Architecture

The daemon is structured into four decoupled modules:
- **`MarketDataFeed`**: Handles daily universe scanning via CoinMarketCap API, manages the 24h ticker volume fallback mechanism, and tracks live price updates.
- **`ExchangeGateway`**: Encapsulates Binance USDT-M Futures REST endpoints and the User Data Stream WebSocket. Handles signature generation, listenKey keep-alive pings (every 45 minutes), and rate limit backoff.
- **`OrderManager & Engine`**: Implements the finite state machine for all 200 symbols, handles order placement, order cancellations, and partial fill accounting.
- **`StateStore & Notifier`**: Provides crash-resilient ACID state persistence in SQLite (WAL mode) and background webhook dispatching to ServerChan/Bark.

### 2. State Machine Reducer & Precision Prototype

```python
# Prototype state transition reducer for SymbolState
def apply_trade_fill(state, fill_slot, fill_side, fill_price, fill_qty):
    if fill_side == "BUY":
        # DCA Addition
        new_qty = state.qty + fill_qty
        new_notional = state.size_notional + (fill_price * fill_qty)
        state.avg_price = new_notional / new_qty
        state.qty = new_qty
        state.size_notional = new_notional
        state.tier += 1
        return transition_to_next_tier_orders(state)
    elif fill_side == "SELL":
        # Batch Take-Profit Fill
        state.qty -= fill_qty
        state.size_notional -= (state.avg_price * fill_qty)
        state.active_sell_slots[fill_slot] = False
        if not any(state.active_sell_slots) or state.qty <= 1e-6:
            return reset_symbol_to_idle(state)
        return state
```

### 3. Database Schema (SQLite WAL Mode)

- `symbols_state`: Primary key `symbol`. Fields: `tier`, `cycle`, `is_open`, `qty`, `avg_price`, `entry_p0`, `armed_10u`, `updated_at`.
- `active_orders`: Primary key `client_order_id`. Fields: `symbol`, `exchange_order_id`, `tier`, `side`, `order_type`, `price`, `qty`, `slot_index`, `status`, `created_at`.
- `trade_fills`: Primary key `trade_id`. Fields: `symbol`, `client_order_id`, `side`, `price`, `qty`, `realized_pnl`, `fee`, `timestamp`.
- `account_snapshots`: Primary key `id`. Fields: `timestamp`, `total_equity`, `cash_balance`, `unrealized_pnl`, `margin_ratio`, `open_symbol_count`.

### 4. Architectural Constraints & Policies (ADR Alignments)

- **ADR 0001 & ADR 0006**: Devaluation recycling triggered at $\le 5.0\text{ USDT}$ market value. Delisting leverage reductions will not abort pending DCA orders if account margin allows.
- **ADR 0002**: Option A strictly enforced. No break-even stop logic exists on partial take-profit fills.
- **ADR 0003**: All orders placed as native Binance `LIMIT` orders (`timeInForce: GTC`), utilizing account-wide capacity.
- **ADR 0008**: WebSocket disconnection triggers a single `GET /fapi/v1/openOrders` and `GET /fapi/v1/positionRisk` to sync state without per-symbol polling.
- **ADR 0009**: Minimum notional check evaluates `price * qty >= 5.0`. If $< 5.0$, remaining batches collapse into a single 100% order.
- **ADR 0010**: Daily universe rebalance queries CMC; if non-200 or timeout $> 10\text{s}$, falls back to `GET /fapi/v1/ticker/24hr` sorted by `quoteVolume ASC`.
- **ADR 0011 & ADR 0012**: SQLite initialized with `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`. Push alerts run in non-blocking worker threads.

## Testing Decisions

### 1. Philosophy: Highest Seam Testing
A single high-level seam—**the `ExchangeGateway` interface**—is tested. Unit tests never inspect internal private state attributes or mock low-level helper functions; instead, they simulate external exchange stimuli (WebSocket message payloads, REST responses) and verify observable outputs (order placement calls, cancellation calls, database states).

### 2. Test Suites
- **`test_state_machine_lifecycle.py`**:
  - Tests Tier 1 fill $\rightarrow$ verifies placement of Tier 1 TP and Tier 2 Buy.
  - Tests Tier 2 DCA fill $\rightarrow$ verifies cancellation of old TP, placement of two 50% TPs (+20%, +50%), and Tier 3 Buy.
  - Tests Tier 3 DCA fill $\rightarrow$ verifies placement of three 1/3 TPs (+20%, +50%, +100%).
  - Tests Option A behavior: 1st TP fill $\rightarrow$ subsequent price drop $\rightarrow$ verifies remaining TP order stays active on orderbook without break-even cancellation.
- **`test_devaluation_recycling.py`**:
  - Tests deep drawdown simulation $\rightarrow$ position market value drops below 5U $\rightarrow$ verifies placement of 20U recycled buy.
  - Tests recycled buy fill $\rightarrow$ verifies placement of fresh pulse TP orders (+20%, +50% from fresh buy price).
  - Tests 1-cycle cap: ensures no third cycle is armed.
- **`test_exchange_constraints.py`**:
  - Tests `minNotional` merging: simulates batch order worth 3.2 USDT $\rightarrow$ verifies automatic consolidation into a single 100% order.
  - Tests WebSocket disconnect: injects disconnect event $\rightarrow$ verifies single-call `/openOrders` reconciliation execution.
- **`test_cmc_failover.py`**:
  - Mocks CMC 500 error $\rightarrow$ verifies automatic fallback to Binance 24h ticker volume ranking.

### 3. Prior Art
Built on the proven vectorized simulation harness in `research/strategy_final.py` which demonstrated +286.78% return, -54.06% max drawdown, and 0 liquidations across 6,552 hourly candles on 200 Binance USDT-M symbols.

## Out of Scope

- Discretionary GUI / web dashboard (reporting handled via CLI and ServerChan/Bark pushes).
- Multi-account copy trading lead/follower distribution (focused strictly on private master/sub-account API operation).
- Cross-exchange arbitrage or non-USDT margin contracts (Coin-M, Spot).
- High-frequency tick orderbook spoofing or millisecond-level HFT latency tuning.

## Further Notes

- Target environment: Python 3.11+ running under Linux / macOS as a `systemd` or `pm2` daemon process.
- Configuration: API keys and credentials managed via `.env` (`BINANCE_API_KEY`, `BINANCE_API_SECRET`, `SERVERCHAN_KEY`, `CMC_API_KEY`).
