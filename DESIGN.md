---
name: PNL Periodic Review Trader
description: Hydrographic-chart-inspired operational ledger for 15-minute periodic review trading.
colors:
  shell-bg: "#0e2a33"
  shell-surface: "#163b47"
  shell-border: "#255362"
  shell-text: "#f2f7f6"
  shell-muted: "#9eb9c0"
  shell-beacon: "#34d399"
  accent: "#135d73"
  accent-hover: "#0e4a5c"
  accent-soft: "#e6f2f5"
  canvas-bg: "#f3f6f5"
  surface: "#ffffff"
  surface-subtle: "#eaf0ee"
  surface-hover: "#f2f7f5"
  border: "#d4dfdc"
  border-strong: "#8fa5a0"
  ink: "#102128"
  ink-secondary: "#3f5760"
  ink-muted: "#526a73"
  gain: "#0a7052"
  gain-bg: "#e6f5ef"
  loss: "#bd2b2b"
  loss-bg: "#fcecec"
  warn: "#985506"
  warn-bg: "#fdf4e3"
  tier-t1: "#1a7692"
  tier-t2: "#c97b18"
  tier-t3: "#cc3b3b"
  tier-t31: "#7250ad"
typography:
  display:
    fontFamily: '"Barlow", "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", -apple-system, sans-serif'
    fontSize: "1.875rem"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.025em"
    fontFeature: '"tnum"'
  headline:
    fontFamily: '"Barlow", "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", -apple-system, sans-serif'
    fontSize: "1.125rem"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.02em"
  title:
    fontFamily: '"Barlow", -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif'
    fontSize: "0.875rem"
    fontWeight: 600
    lineHeight: 1.35
  body:
    fontFamily: '"Barlow", -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif'
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.5
  mono:
    fontFamily: '"SF Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.4
    fontFeature: '"tnum"'
rounded:
  xs: "2px"
  sm: "3px"
  md: "4px"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "20px"
  xl: "32px"
components:
  shell-button:
    backgroundColor: "{colors.shell-surface}"
    textColor: "{colors.shell-text}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  segmented-button-active:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.accent}"
    rounded: "{rounded.sm}"
    padding: "4px 10px"
  ledger-plane:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "20px"
  search-input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "6px 10px 6px 32px"
---

# Design System: PNL Periodic Review Trader

## Overview

**Creative North Star: "Maritime Chart & Port Dispatch Board"**

This design system treats the 15-minute periodic review trading dashboard as an institutional dispatch sheet rather than a dark-mode crypto terminal. A committed deep Atlantic slate-teal command header anchors system health, 15-minute slot progress, and runtime cadence, while the operational workspace below opens onto a calm, high-legibility chart-ivory canvas designed for long-duration desktop monitoring and rapid mobile check-ins.

Visual authority comes from crisp hairline boundaries, disciplined tabular numerals, and purposeful semantic color rather than glowing dark cards or ornamental chrome. The incumbent dark-navy Tailwind card stack (`#090d16` / `#0f172a` with glowing badges and nested borders) is explicitly retired.

**Key Characteristics:**
- Committed deep slate-teal command header paired with a cool chart-ivory working canvas.
- Self-hosted `Barlow` tabular display numerals paired with native CJK sans copy and restrained monospace strictly for prices, quantities, and timestamps.
- Flat hairline-divided ledger planes with crisp 4px corners and zero nested bordered sub-cards.
- WCAG AA (≥ 4.5:1) semantic contrast paired with non-color geometric indicators (dots, alert/info icons, border underlines) across all status, severity, and tier states.

## Colors

A Restrained-plus-Committed operational palette: deep slate-teal owns the persistent top shell, cool chart-ivory carries the workspace, and semantic hues speak only for PnL direction, execution severity, and the 3-tier strategy ladder.

### Primary
- **Atlantic Slate-Teal** (`shell-bg`): Full-bleed sticky command header grounding brand identity, ledger status, and the 15-minute review cadence rail.
- **Harbor Teal** (`accent`): Active tab underlines, selected segmented controls, focus outlines, text selection, and T1 base-tier identity.
- **Beacon Mint** (`shell-beacon`): Live 15-minute slot countdown readout, slot progress rail fill, and header focus rings on the dark shell.

### Secondary
- **Sea Emerald** (`gain` / `gain-bg`): Positive realized PnL, floating profit, take-profit trigger prices, T2.1 half-position recovery, and healthy execution status.
- **Crimson Coral** (`loss` / `loss-bg`): Negative PnL, maximum drawdown, T3 deep-DCA stress, and critical actionable execution issues.
- **Buoy Amber** (`warn` / `warn-bg`): Current drawdown, T2 DCA stage, DCA trigger thresholds, and stale-snapshot / pending-reconciliation notices.
- **Plum Slate** (`tier-t31`): T3.1 / T3.2 staged profit-recovery positions and T3 write-off prices.

### Neutral
- **Chart Ivory** (`canvas-bg`): Cool sea-tinted page canvas separating flat data planes without drop shadows.
- **Ledger White** (`surface`): Primary data surface for the KPI ribbon, equity chart, risk triage panel, diagnostics, and ledger workbench.
- **Sea Mist** (`surface-subtle`): Recessed table headers, segmented tracks, and workbench navigation bars.
- **Slate Ink / Harbor Gray / Muted Slate** (`ink`, `ink-secondary`, `ink-muted`): Three-step text hierarchy calibrated to maintain ≥ 4.5:1 contrast on both white and chart-ivory surfaces.
- **Hairline Mist & Boundary Slate** (`border`, `border-strong`): 1px structural rules and ≥ 3:1 interactive input borders.

### Named Rules
**The Zero-Decoration Color Rule.** Never tint neutral metadata counts (fill counts, active symbol counts, timestamps) with saturated pill badges; reserve color fills strictly for directional PnL, risk severity, leverage, and tier state.
**The Non-Color Redundancy Rule.** Every state badge, execution issue, and active tab must pair color with a geometric shape, icon, or hairline underline so status survives color blindness and Windows Forced Colors Mode.

## Typography

**Display & Numeric Font:** Self-hosted `Barlow` (weights 500, 600, 700, WOFF2 served from `/fonts/`) with CJK sans fallback.
**Body Font:** `Barlow` leading into platform CJK sans (`PingFang SC`, `Hiragino Sans GB`, `Noto Sans SC`, `Segoe UI`, `sans-serif`).
**Data Monospace Font:** `SF Mono` / `ui-monospace` / `Menlo` / `Consolas`, strictly scoped to prices, quantities, slot windows, and timestamps.

**Character:** Squared industrial grotesque proportions lend nautical dispatch precision to KPI figures, tickers, and axis ticks while allowing Chinese operational labels to stay calm and effortless to scan.

### Hierarchy
- **Display** (`700`, `22px` mobile / `28px–30px` desktop, `line-height: 1`, `tabular-nums`): Primary KPI numerals (`账户总权益`, `累计已实现净盈亏`, `持仓浮动盈亏`, `持仓名义总额`).
- **Headline** (`700`, `16px–18px`, `line-height: 1.25`): Brand title (`PNL Periodic Review Trader`) in the command shell.
- **Title** (`600`, `14px`, `line-height: 1.35`): Section headings (`账户权益走势`, `权益回撤`, `执行异常`, `标的层级与名义敞口`, `浮亏焦点 Top 6`, `近 7 日交易成本`).
- **Body** (`400–600`, `12px–14px`, `line-height: 1.5`): Operational tables, controls, tabs, issue descriptions, and diagnostic rows. Never drop body copy below `12px`.
- **Mono** (`500`, `12px`, `tabular-nums`): Exact prices, order quantities, countdowns, and timestamps.

### Named Rules
**The Honest Monospace Rule.** Use monospace exclusively for exact prices, quantities, and timestamps—never on large KPI display figures or Chinese prose sentences.

## Layout

Desktop-first `1400px` max-width container (`16px`–`32px` horizontal padding) organized into four horizontal operational registers with `16px`–`20px` vertical rhythm:
1. **Sticky Full-Bleed Command Shell**: Brand identity + status pill on left; 15-minute slot progress rail + runtime + refresh action on right.
2. **Unified KPI Ribbon**: Single flat hairline-divided strip—`4` columns on desktop (`>= 1024px`) collapsing cleanly into an aligned `2×2` hairline grid on mobile (`< 1024px`) so the equity chart remains visible in the first mobile viewport.
3. **Primary 12-Column Asymmetric Split (`8 / 4` on `xl`)**: Interactive Retina HTML5 Canvas equity chart paired directly beside consolidated `权益回撤` + `执行异常` triage.
4. **Diagnostics Band (`5 / 4 / 3` on `xl`) & Full-Width Ledger Workbench**: Unified tier count & notional exposure ballast, Top 6 losing positions, 7-day trading costs, and tabbed full-width positions/fills/ranking workbench.

## Elevation & Depth

**Flat by default, tonal by structure.** Panels sit flat on the chart-ivory canvas separated exclusively by `1px` hairline borders (`border`) rather than ambient box shadows. Elevation appears only for floating overlays (the deep slate-teal chart hover tooltip with a compact neutral drop shadow `0 4px 10px rgba(0, 0, 0, 0.14)`).

### Named Rules
**The No-Nested-Card Rule.** Never place bordered rounded sub-cards inside a `.ledger-plane` container; separate internal metrics, issue logs, tier rows, and rankings with `1px` hairline dividers.

## Shapes

Tight architectural corners (`4px` on planes, inputs, and buttons; `3px` on tier badges and segmented controls; `2px` on data bars and progress tracks; full pill exclusively for compact status indicators). Hairline borders are always `1px`—never thick decorative side stripes.

## Components

### Buttons
- **Shape:** Tight architectural corners (`4px`).
- **Shell Action (`#btn-refresh`):** Deep slate-teal elevated surface (`shell-surface`) with `1px` border (`shell-border`), `8px 12px` padding, subtle lighten on hover, `1px` press depression, and mint focus ring.
- **Segmented Controls (Period Toggle & Tier Filter):** Recessed `surface-subtle` track (`2px` padding) with active button promoted to crisp white `surface` + `accent` ink (`aria-pressed="true"`).

### Status & Tier Chips
- **Status Pills:** Paired geometric status dot + concise Chinese label (`status-shell-ok`, `status-shell-warn`, `status-shell-error`) on the dark header.
- **Tier Badges:** Flat tinted `3px` rectangles with `1px` matching hairline borders distinguishing `T1`, `T2`, `T2.1`, `T3`, and `T3.1/T3.2`.

### Inputs / Fields
- **Search Input:** Crisp white surface with `1px` high-contrast border (`border-strong`, ≥ 3:1 against white), leading search icon, `4px` radius, and teal focus ring (`rgba(19, 93, 115, 0.16)`).

### Navigation (Workbench Tabs)
- Single-row accessible `role="tablist"` with keyboard Arrow/Home/End selection-follows-focus and `hidden="until-found"` panel syncing via `beforematch`. Active tab carries `accent` ink plus a `2.5px` bottom border pseudo-element that survives Forced Colors Mode without shifting layout width.

### Signature Components
- **15-Minute Slot Cadence Rail:** Header telemetry module displaying the current wall-clock window (`巡检 HH:MM–HH:MM`), remaining countdown (`下轮 MM:SS`), and a GPU-composited `scaleX` progress bar (`cubic-bezier(0.22, 1, 0.36, 1)`).
- **Retina Financial Equity Canvas:** Linear financial polyline (no synthetic bezier overshoots) with nice round Y-axis steps, intermediate X-axis time markers, labeled period-open reference baseline (`期初 $...`), and interactive dual-axis crosshair with delta tooltip.
- **Sticky-Symbol Sortable Table:** Accessible `<th scope="col">` sort buttons wired via Invoker Commands (`command="--sort"` with feature-detected click fallback), in-place DOM row reordering (`moveBefore`), `role="status"` announcements, sticky `thead` + sticky `<th scope="row">` symbol column, and a right-edge scroll shadow affordance on narrow viewports.

## Do's and Don'ts

### Do:
- **Do** self-host all fonts (`Barlow` WOFF2 in `/fonts/`) so local/offline trading nodes render identically with zero external CDN dependencies.
- **Do** use `tabular-nums` on every financial figure, countdown, timestamp, and table column, and format all USD totals with thousands separators via `formatUsd` / `formatSignedUsd`.
- **Do** pair color with non-color cues (warning/info icons, status dots, border underlines) and maintain ≥ 4.5:1 text contrast.
- **Do** animate progress rails with GPU-composited `transform: scaleX(...)` rather than `width`.

### Don't:
- **Don't** reintroduce dark-navy glowing SaaS cards, gradient text, or nested bordered boxes inside `.ledger-plane` panels.
- **Don't** apply `font-mono` to KPI display headlines or Chinese body prose, or drop readable body text below `12px`.
- **Don't** render missing ledger metrics, un-reconciled close fills, or non-USDT fee caveats as synthetic zeros—preserve honest `—` and warning states.
- **Don't** apply `scrollbar-width: thin` or clip focus outlines inside `overflow-x: auto` containers.
