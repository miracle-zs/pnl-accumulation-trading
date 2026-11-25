// API Fetching and Data Synchronization Service

import { state } from './state.js';
import { formatSignedUsd } from './formatters.js';
import { renderRetinaChart } from './chart.js';
import { renderPositions } from './tables.js';
import {
  updatePositionStatusKpi,
  renderDrawdown,
  renderLosingPositions,
  renderTierExposure,
  renderCosts,
  renderExecutionIssues,
} from './analytics.js';
import { renderFills, renderRanking } from './fills.js';

let activeAbortController = null;

export async function fetchData() {
  // If an existing request is in-flight, cancel it to avoid race conditions
  if (activeAbortController) {
    activeAbortController.abort();
  }
  const controller = new AbortController();
  activeAbortController = controller;
  const { signal } = controller;

  const refreshIcon = document.getElementById('refresh-icon');
  const refreshText = document.getElementById('refresh-text');
  if (refreshIcon) refreshIcon.classList.add('animate-spin');
  if (refreshText) refreshText.textContent = '刷新中...';

  const statusBadge = document.getElementById('system-status-badge');
  try {
    const responses = await Promise.all([
      fetch('api/overview', { signal }),
      fetch(`api/equity?period=${state.currentPeriod}`, { signal }),
      fetch('api/tier-distribution', { signal }),
      fetch('api/positions', { signal }),
      fetch('api/fills?limit=12&side=BUY', { signal }),
      fetch('api/fills?limit=12&side=SELL', { signal }),
      fetch('api/analytics', { signal }),
    ]);
    if (responses.some(response => !response.ok)) {
      throw new Error('Dashboard API returned an unsuccessful response');
    }
    const [ov, eq, td, positions, buyFills, sellFills, an] = await Promise.all(
      responses.map(response => response.json()),
    );
    if (signal.aborted || activeAbortController !== controller) {
      return;
    }

    let pendingCloseFills = null;
    {
      state.tradingUptimeBaseSeconds = Number.isFinite(ov.trading_uptime_seconds)
        ? ov.trading_uptime_seconds
        : null;
      state.tradingUptimeUpdatedAt = performance.now();
      state.ledgerAvailable = ov.status === 'LOCAL_LEDGER_ONLY';
      pendingCloseFills = ov.pending_close_fill_count;

      if (statusBadge) {
        if (!state.ledgerAvailable) {
          statusBadge.textContent = '等待账本数据';
          statusBadge.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-500/10 text-amber-300 border border-amber-500/30';
        } else if (pendingCloseFills > 0) {
          statusBadge.textContent = `平仓流水待对账 ${pendingCloseFills} 笔`;
          statusBadge.title = '等待币安撮合流水归档，暂缓统计净利润';
          statusBadge.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-500/10 text-amber-300 border border-amber-500/30';
        } else {
          statusBadge.textContent = '只读本地账本';
          statusBadge.title = '只读监控 · 数据源自本地 SQLite 状态库';
          statusBadge.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-slate-800 text-slate-300 border border-slate-700';
        }
      }

      const realizedCash = state.ledgerAvailable ? ov.realized_cash : null;
      const realizedEl = document.getElementById('kpi-realized-cash');
      if (realizedEl) {
        realizedEl.textContent = formatSignedUsd(realizedCash);
        realizedEl.className = `text-2xl lg:text-3xl font-bold font-mono tracking-tight ${realizedCash === null ? 'text-slate-400' : realizedCash >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
        realizedEl.title = pendingCloseFills > 0 ? '平仓明细待补录，净利润暂不可用' : ov.other_fee_count > 0 ? '包含非 USDT 手续费，无法准确计算净利润' : '';
      }
      const fillCountEl = document.getElementById('kpi-fill-count');
      const activeSymbolsEl = document.getElementById('kpi-active-symbols');
      if (fillCountEl) fillCountEl.textContent = state.ledgerAvailable ? `${ov.fill_count} 笔成交` : '—';
      if (activeSymbolsEl) activeSymbolsEl.textContent = state.ledgerAvailable ? `${ov.active_positions_count} 个在管标的` : '—';
    }

    {
      renderDrawdown(eq);
      const snapshotStatus = document.getElementById('equity-snapshot-status');
      if (snapshotStatus) {
        snapshotStatus.textContent = eq.latest
          ? `采样时间：${new Date(eq.latest.captured_at_ms).toLocaleString('zh-CN')}${eq.latest.is_current_slot ? '' : ' · 等待下一轮快照'}`
          : '等待账户净值采样';
        snapshotStatus.className = `text-xs mt-0.5 ${eq.latest && !eq.latest.is_current_slot ? 'text-amber-400' : 'text-slate-400'}`;
      }

      const kpiEquity = document.getElementById('kpi-equity');
      const kpiAvailable = document.getElementById('kpi-available');
      const kpiNotional = document.getElementById('kpi-notional');
      const kpiLeverage = document.getElementById('kpi-actual-leverage');
      const kpiUnrealized = document.getElementById('kpi-unrealized-pnl');

      if (eq.latest) {
        if (kpiEquity) kpiEquity.textContent = `$${eq.latest.equity.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
        if (kpiAvailable) kpiAvailable.textContent = eq.latest.available_balance !== null ? `$${eq.latest.available_balance.toFixed(2)}` : '—';
        if (kpiNotional) kpiNotional.textContent = eq.latest.position_notional !== null ? `$${eq.latest.position_notional.toFixed(2)}` : '—';
        if (kpiLeverage) kpiLeverage.textContent = eq.latest.leverage !== null ? `实际杠杆 ${eq.latest.leverage.toFixed(2)}x` : '实际杠杆 —';

        const unpnl = eq.latest.unrealized_pnl;
        if (kpiUnrealized) {
          kpiUnrealized.textContent = formatSignedUsd(unpnl);
          kpiUnrealized.className = `text-2xl lg:text-3xl font-bold font-mono tracking-tight ${unpnl === null ? 'text-slate-400' : unpnl >= 0 ? 'text-emerald-400' : 'text-amber-400'}`;
        }
      } else {
        if (kpiEquity) kpiEquity.textContent = '—';
        if (kpiAvailable) kpiAvailable.textContent = '—';
        if (kpiNotional) kpiNotional.textContent = '—';
        if (kpiLeverage) kpiLeverage.textContent = '实际杠杆 —';
        if (kpiUnrealized) kpiUnrealized.textContent = '—';
      }

      const badge = document.getElementById('equity-change-badge');
      if (badge) {
        if (eq.change !== null) {
          const percentage = eq.change_pct === null ? '' : ` (${formatSignedUsd(eq.change_pct).replace('$', '')}%)`;
          badge.textContent = `${formatSignedUsd(eq.change)}${percentage}`;
          badge.className = `font-mono text-xs px-1.5 py-0.5 rounded ${eq.change >= 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'}`;
        } else {
          badge.textContent = '—';
          badge.className = 'font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-300';
        }
      }

      state.chartHistoryPoints = eq.points;
      renderRetinaChart(state.chartHistoryPoints);
    }

    {
      const todayPnl = document.getElementById('kpi-today-pnl');
      const todayPendingCloseFills = an.today ? an.today.pending_close_fill_count : 0;
      const realizedNet = state.ledgerAvailable && an.today ? an.today.realized_net : null;
      if (todayPnl) {
        todayPnl.textContent = formatSignedUsd(realizedNet);
        todayPnl.title = todayPendingCloseFills > 0 ? '今日平仓明细待补录，净利润暂不可用' : (an.today && an.today.other_fee_count > 0 ? '包含非 USDT 手续费，无法准确计算净利润' : '');
        todayPnl.className = `font-mono ${realizedNet === null ? 'text-slate-400' : realizedNet >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
      }
      renderRanking(an.symbols, pendingCloseFills);
      renderCosts(an.costs);
      renderExecutionIssues(an.execution_issues);
    }

    {
      renderTierExposure(td.exposure);
      if (state.ledgerAvailable) {
        const cntT1 = document.getElementById('tier-cnt-t1');
        const cntT2 = document.getElementById('tier-cnt-t2');
        const cntT3 = document.getElementById('tier-cnt-t3');
        const cntT31 = document.getElementById('tier-cnt-t31');
        const totalBadge = document.getElementById('tier-total-badge');
        if (cntT1) cntT1.textContent = `${td.t1} 币`;
        if (cntT2) cntT2.textContent = `${td.t2 + td.t21} 币`;
        if (cntT3) cntT3.textContent = `${td.t3} 币`;
        if (cntT31) cntT31.textContent = `${td.t31 + td.t32} 币`;
        if (totalBadge) totalBadge.textContent = `${td.total} 个在管标的`;

        const percentagePerSymbol = td.total > 0 ? 100 / td.total : 0;
        const bT1 = document.getElementById('bar-t1');
        const bT2 = document.getElementById('bar-t2');
        const bT3 = document.getElementById('bar-t3');
        const bT31 = document.getElementById('bar-t31');
        if (bT1) bT1.style.width = `${td.t1 * percentagePerSymbol}%`;
        if (bT2) bT2.style.width = `${(td.t2 + td.t21) * percentagePerSymbol}%`;
        if (bT3) bT3.style.width = `${td.t3 * percentagePerSymbol}%`;
        if (bT31) bT31.style.width = `${(td.t31 + td.t32) * percentagePerSymbol}%`;
      } else {
        ['tier-cnt-t1', 'tier-cnt-t2', 'tier-cnt-t3', 'tier-cnt-t31'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.textContent = '—';
        });
        const totalBadge = document.getElementById('tier-total-badge');
        if (totalBadge) totalBadge.textContent = '— 个在管标的';
        ['bar-t1', 'bar-t2', 'bar-t3', 'bar-t31'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.style.width = '0%';
        });
      }
    }

    {
      state.allPositions = positions;
      renderPositions();
      renderLosingPositions(state.allPositions);
      updatePositionStatusKpi(state.allPositions);
    }

    {
      state.rawBuyFills = buyFills;
      state.rawSellFills = sellFills;
      renderFills(state.rawBuyFills, state.rawSellFills);
    }
  } catch (err) {
    if (err.name === 'AbortError' || activeAbortController !== controller) {
      return; // Silently ignore cancelled or superseded requests
    }
    if (statusBadge) {
      statusBadge.textContent = '数据刷新失败';
      statusBadge.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-rose-500/10 text-rose-300 border border-rose-500/30';
    }
    console.warn('API poll notice:', err);
  } finally {
    if (activeAbortController === controller) {
      activeAbortController = null;
      if (refreshIcon) refreshIcon.classList.remove('animate-spin');
      if (refreshText) refreshText.textContent = '刷新';
    }
  }
}
