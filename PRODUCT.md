# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary user: a quantitative trader / strategy operator running a live Binance USDⓈ-M Futures automated trading bot locally or on a dedicated server.

Situation and job: desktop-primary monitoring (kept open for periodic check-ins and ambient oversight), with occasional mobile viewing. On every check-in, the operator needs to assess account equity health and spot execution or risk anomalies within seconds, then drill into tier structure, losing positions, exposure, managed positions, recent fills, and per-symbol realized PnL as needed.

## Product Purpose

Provide a read-only local web ledger dashboard (`PNL Periodic Review Trader`) for the 15-minute periodic review trading strategy. Success means the operator can verify system status, equity trajectory, drawdown, execution anomalies, and per-tier position risk at a glance without touching exchange APIs or risking unintended order submission.

## Positioning

A strictly read-only, deterministic local SQLite ledger monitor tailored specifically to a 3-tier periodic-review strategy (T1 base entry, T2 / T2.1 DCA & half-take-profit, T3 / T3.1 / T3.2 deep DCA, staged profit recovery, and $5 notional write-off recycling) running on 15-minute wall-clock slots (`00`, `15`, `30`, `45`) across Binance USDT-M COIN / INDEX / RWA perpetual contracts.

## Operating Context

- Backed by a FastAPI server (`src/pnl_trader/web/app.py` and `service.py`) reading `state.sqlite3` in read-only mode.
- Polls `/api/dashboard` every 30 seconds (pausing when the tab is hidden and catching up on foreground) and triggers an automatic refresh 3.5 seconds after each 15-minute slot boundary.
- Displays Beijing time (`UTC+8`) for daily boundaries and local Chinese UI copy (`zh-CN`) with exact domain terminology from `GLOSSARY.md`.
- Static frontend built with vanilla HTML, ES modules (`src/pnl_trader/web/static/js/`), HTML5 Canvas equity chart, and Tailwind CSS v3 compiled via `frontend/`.

## Capabilities and Constraints

- Strictly read-only: no order placement, manual intervention, or backend write endpoints.
- Must preserve all existing backend API endpoints (`/api/dashboard`, `/api/overview`, `/api/equity`, `/api/analytics`, `/api/tier-distribution`, `/api/positions`, `/api/fills`) and all currently exposed data metrics.
- Must preserve static serving routes (`/`, `/tailwind.css`, `/js/*`) and existing test invariants (including `"PNL Periodic Review Trader"` in the HTML response and serving `js/app.js` and `/tailwind.css`).
- Layout and information hierarchy may be freely reorganized to elevate equity health and risk/execution anomalies while retaining every data metric and interactive workflow (period switching `24h`/`7d`/`30d`, manual refresh, view tabs for positions/fills/ranking, tier filtering, symbol search, and sortable position columns).
- Must clearly distinguish missing ledger (`WAITING_DATA`), pending close fills awaiting reconciliation (`pending_close_fill_count > 0`), non-USDT fee caveats (`other_fee_count > 0`), stale snapshots (`!is_current_slot`), and pending full-close (`待全平`) positions without fabricating zero values.

## Brand Commitments

- Product title: `PNL Periodic Review Trader`.
- Domain terminology follows `GLOSSARY.md` (15 分钟巡检, Tier / 层级, 补仓, 分批止盈, 待全平, 注销价 / T3 出清机制, 托管标的).

## Evidence on Hand

- Full backend query service (`src/pnl_trader/web/service.py`), FastAPI routes (`src/pnl_trader/web/app.py`), and test suite (`tests/web/test_dashboard_web.py`).
- No live `var/state.sqlite3` committed to the repository; verification and previews must use clearly synthetic SQLite ledger fixtures matching the exact schema.

## Product Principles

1. **Truth over decoration**: Never hide missing data, reconciliation lag, non-USDT fee gaps, or stale snapshots behind synthetic zeros or optimistic styling.
2. **Equity and risk first**: Account equity health, drawdown, and execution anomalies must be immediately scannable on load before deep table inspection.
3. **Calm operational clarity**: Support long-duration desktop monitoring with crisp numerical alignment, scannable hierarchy, and minimal label clutter.
4. **Domain precision**: Surface the exact 3-tier lifecycle (T1 → T2/T2.1 → T3/T3.1/T3.2 → write-off/reopen) and 15-minute slot cadence without diluting strategy semantics.
5. **Fast, resilient read-only surface**: Keep the frontend dependency-light, responsive from desktop monitors down to occasional mobile checks, and accessible via keyboard.
