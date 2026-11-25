---
version: 1
slug: "src-pnl-trader-web-static-index-html"
primary_target: "src/pnl_trader/web/static/index.html"
related_targets: ["src/pnl_trader/web/static/js/api.js","src/pnl_trader/web/static/js/app.js","src/pnl_trader/web/static/js/chart.js","src/pnl_trader/web/static/js/renderers.js","frontend/tailwind.input.css"]
---

# Surface Brief: PNL Periodic Review Trader Dashboard

## Scope & Mode

- Primary target: `src/pnl_trader/web/static/index.html` (with `js/app.js`, `js/api.js`, `js/chart.js`, `js/renderers.js`, `frontend/tailwind.input.css`, `frontend/tailwind.config.cjs`).
- Visitor mode: **Operate**
- Audience & job: Quantitative trader / operator monitoring a live Binance USDⓈ-M Futures 15-minute periodic review strategy via a read-only local SQLite ledger; desktop-primary with occasional mobile check-ins.
- Primary priority: Assess account equity trajectory and spot drawdown / execution anomalies within 2 seconds, then inspect tier structure & exposure, losing positions, trading costs, managed positions, recent fills, and per-symbol realized PnL.
- Constraints: Preserve all backend APIs, every existing data metric, Chinese domain terminology (`GLOSSARY.md`), and existing test assertions (`"PNL Periodic Review Trader"`, `/tailwind.css`, `/js/app.js`).

## Direction contract

- **THESIS:** A hydrographic-chart-inspired operational ledger that anchors status and 15-minute slot cadence in a committed deep slate-teal command header over a calm, high-legibility chart-ivory working canvas — refusing the incumbent dark-navy Tailwind card grid and generic neon crypto tropes.
- **OWN-WORLD:** Deep Atlantic slate-teal shell (`#0E2A33`), chart-ivory canvas (`#F4F6F5`), crisp white data planes (`#FFFFFF`), pale sea-mist recessed bands (`#E8EEEC`), slate-ink typography (`#102128`), and disciplined semantic tokens (sea-emerald `#0C7A59` gain, crimson-coral `#C53030` loss/T3/drawdown, buoy-amber `#B4690E` T2/warning, deep-teal `#155E75` T1/selection, plum-slate `#6D4C9E` T3 recovery). Hairline 1px rules (`#D6E0DD`), subtle tonal elevation, tabular numerals throughout, and themed browser chrome (`::selection`, `scrollbar-color`, `accent-color`).
- **STORY:** On load, the operator immediately verifies ledger freshness, 15-minute review slot progress, equity trend, drawdown exposure, and any active execution issues; then scans tier balance and notional exposure in one unified band before filtering, searching, or sorting the full-width ledger workbench.
- **FIRST VIEWPORT:** Top full-bleed deep slate-teal command bar (`56px` desktop) holding the product identity, ledger status pill, live 15-minute slot progress rail (`00–15m`) with countdown, uptime, and refresh button. Immediately below, a clean ruled 4-metric KPI summary ribbon (Account Equity + period delta, Cumulative Realized Net + today's net, Floating Unrealized PnL + profit/loss split, Position Notional + actual leverage) with zero nested card chrome. Directly beneath sits the primary 12-column asymmetric split: left 8 columns host the interactive high-DPI Retina equity curve with period toggle (`24h`/`7d`/`30d`), while right 4 columns host the consolidated **Risk & Anomaly Triage** panel (Max/Current Drawdown metrics paired directly above active Execution Issues with clear visual severity).
- **FORM:** Maritime Chart & Port Dispatch Board — Candidate 6 of 7 grounded directions (seed key `251cb232`). Signature move: the 15-minute slot progress rail in the deep slate-teal header paired with a unified Tier Structure & Notional Exposure ballast strip uniting T1 / T2 / T3 counts and USDT exposure in one visual grammar.
- **FINISH:** unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
