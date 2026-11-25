// API Fetching and Data Synchronization Service

import { renderRetinaChart } from './chart.js';
import {
  state,
  formatSignedUsd,
  renderTradingUptime,
  renderPositions,
  updatePositionStatusKpi,
  renderDrawdown,
  renderLosingPositions,
  renderTierExposure,
  renderCosts,
  renderExecutionIssues,
  renderFills,
  renderRanking,
} from './renderers.js';

let activeAbortController = null;
let equityAbortController = null;

function setStatusBadge(variant, label, title) {
  const badge = document.getElementById('system-status-badge');
  const textEl = document.getElementById('system-status-text');
  badge.className = `status-pill ${variant}`;
  textEl.textContent = label;
  if (title !== undefined) {
    badge.title = title;
  }
}

function applyEquity(eq) {
  renderDrawdown(eq);
  const snapshotStatus = document.getElementById('equity-snapshot-status');
  snapshotStatus.textContent = eq.latest
    ? `采样：${new Date(eq.latest.captured_at_ms).toLocaleString('zh-CN')}${eq.latest.is_current_slot ? '' : ' · 等待本轮快照'}`
    : '等待账户净值采样';
  snapshotStatus.className = `text-xs mt-0.5 tabular-nums ${
    eq.latest && !eq.latest.is_current_slot ? 'text-[#985506] font-medium' : 'text-[#526a73]'
  }`;

  const kpiEquity = document.getElementById('kpi-equity');
  const kpiAvailable = document.getElementById('kpi-available');
  const kpiNotional = document.getElementById('kpi-notional');
  const kpiLeverage = document.getElementById('kpi-actual-leverage');
  const kpiUnrealized = document.getElementById('kpi-unrealized-pnl');
  const kpiCushion = document.getElementById('kpi-margin-cushion');

  const kpiBaseClass = 'font-display text-[22px] sm:text-[28px] lg:text-[30px] leading-none font-bold tabular-nums tracking-tight';

  if (eq.latest) {
    kpiEquity.textContent = `$${eq.latest.equity.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
    kpiAvailable.textContent = `$${eq.latest.available_balance.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
    kpiNotional.textContent = `$${eq.latest.position_notional.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
    kpiLeverage.textContent = eq.latest.leverage !== null
      ? `实际杠杆 ${eq.latest.leverage.toFixed(2)}x`
      : '实际杠杆 —';

    const unpnl = eq.latest.unrealized_pnl;
    kpiUnrealized.textContent = formatSignedUsd(unpnl);
    kpiUnrealized.className = `${kpiBaseClass} ${
      unpnl > 0 ? 'text-[#0a7052]' : unpnl < 0 ? 'text-[#bd2b2b]' : 'text-[#102128]'
    }`;

    if (eq.latest.equity > 0 && Number.isFinite(eq.latest.available_balance)) {
      const cushionPct = (eq.latest.available_balance / eq.latest.equity) * 100;
      kpiCushion.textContent = `${cushionPct.toFixed(1)}%`;
      kpiCushion.className = `font-display font-semibold tabular-nums ${
        cushionPct < 25 ? 'text-[#bd2b2b]' : cushionPct < 45 ? 'text-[#985506]' : 'text-[#0a7052]'
      }`;
    } else {
      kpiCushion.textContent = '—';
      kpiCushion.className = 'font-display font-medium tabular-nums text-[#526a73]';
    }
  } else {
    kpiEquity.textContent = '—';
    kpiAvailable.textContent = '—';
    kpiNotional.textContent = '—';
    kpiLeverage.textContent = '实际杠杆 —';
    kpiUnrealized.textContent = '—';
    kpiUnrealized.className = `${kpiBaseClass} text-[#102128]`;
    kpiCushion.textContent = '—';
    kpiCushion.className = 'font-display font-medium tabular-nums text-[#526a73]';
  }

  const badge = document.getElementById('equity-change-badge');
  if (eq.change !== null) {
    const percentage = eq.change_pct === null ? '' : ` (${formatSignedUsd(eq.change_pct).replace('$', '')}%)`;
    badge.textContent = `${formatSignedUsd(eq.change)}${percentage}`;
    badge.className = `font-display tabular-nums text-xs font-semibold px-1.5 py-0.5 rounded ${
      eq.change >= 0 ? 'bg-[#e6f5ef] text-[#0a7052]' : 'bg-[#fcecec] text-[#bd2b2b]'
    }`;
  } else {
    badge.textContent = '—';
    badge.className = 'font-display tabular-nums text-xs font-semibold px-1.5 py-0.5 rounded bg-[#eaf0ee] text-[#3f5760]';
  }

  state.chartHistoryPoints = eq.points;
  renderRetinaChart(state.chartHistoryPoints);
}

export async function fetchEquity(period) {
  if (equityAbortController) {
    equityAbortController.abort();
  }
  const controller = new AbortController();
  equityAbortController = controller;
  const { signal } = controller;

  try {
    const response = await fetch(`api/equity?period=${period}`, { signal });
    if (!response.ok) {
      throw new Error('Equity API returned an unsuccessful response');
    }
    const eq = await response.json();
    if (signal.aborted || equityAbortController !== controller) {
      return;
    }
    applyEquity(eq);
  } catch (err) {
    if (err.name === 'AbortError' || equityAbortController !== controller) {
      return;
    }
    console.warn('Equity poll notice:', err);
  } finally {
    if (equityAbortController === controller) {
      equityAbortController = null;
    }
  }
}

export async function fetchData() {
  if (activeAbortController) {
    activeAbortController.abort();
  }
  if (equityAbortController) {
    equityAbortController.abort();
    equityAbortController = null;
  }
  const controller = new AbortController();
  activeAbortController = controller;
  const { signal } = controller;

  const refreshIcon = document.getElementById('refresh-icon');
  const refreshText = document.getElementById('refresh-text');
  refreshIcon.classList.add('animate-spin');
  refreshText.textContent = '同步中';

  try {
    const response = await fetch(
      `api/dashboard?period=${state.currentPeriod}&fills_limit=12`,
      { signal },
    );
    if (!response.ok) {
      throw new Error('Dashboard API returned an unsuccessful response');
    }
    const snapshot = await response.json();
    if (signal.aborted || activeAbortController !== controller) {
      return;
    }

    const {
      overview: ov,
      equity: eq,
      tier_distribution: td,
      positions,
      buy_fills: buyFills,
      sell_fills: sellFills,
      analytics: an,
    } = snapshot;

    state.tradingUptimeBaseSeconds = Number.isFinite(ov.trading_uptime_seconds)
      ? ov.trading_uptime_seconds
      : null;
    state.tradingUptimeUpdatedAt = performance.now();
    renderTradingUptime();

    state.ledgerAvailable = ov.status === 'LOCAL_LEDGER_ONLY';
    const pendingCloseFills = ov.pending_close_fill_count;

    if (!state.ledgerAvailable) {
      setStatusBadge('status-shell-warn', '等待账本数据', '等待 state.sqlite3 生成');
    } else if (pendingCloseFills > 0) {
      setStatusBadge(
        'status-shell-warn',
        `待对账 ${pendingCloseFills} 笔`,
        '等待币安撮合流水归档，暂缓统计净利润',
      );
    } else {
      setStatusBadge('status-shell-ok', '只读本地账本', '只读监控 · 数据源自本地 SQLite 状态库');
    }

    const realizedCash = state.ledgerAvailable ? ov.realized_cash : null;
    const realizedEl = document.getElementById('kpi-realized-cash');
    realizedEl.textContent = formatSignedUsd(realizedCash);
    realizedEl.className = `font-display text-[22px] sm:text-[28px] lg:text-[30px] leading-none font-bold tabular-nums tracking-tight ${
      realizedCash === null ? 'text-[#526a73]' : realizedCash >= 0 ? 'text-[#0a7052]' : 'text-[#bd2b2b]'
    }`;
    realizedEl.title = pendingCloseFills > 0
      ? '平仓明细待补录，净利润暂不可用'
      : ov.other_fee_count > 0
        ? '包含非 USDT 手续费，无法准确计算净利润'
        : '';

    document.getElementById('kpi-fill-count').textContent = state.ledgerAvailable ? `${ov.fill_count} 笔成交` : '—';
    document.getElementById('kpi-active-symbols').textContent = state.ledgerAvailable ? `${ov.active_positions_count} 个标的` : '—';

    applyEquity(eq);

    const todayPnl = document.getElementById('kpi-today-pnl');
    const todayPendingCloseFills = an.today.pending_close_fill_count;
    const realizedNet = state.ledgerAvailable ? an.today.realized_net : null;
    todayPnl.textContent = formatSignedUsd(realizedNet);
    todayPnl.title = todayPendingCloseFills > 0
      ? '今日平仓明细待补录，净利润暂不可用'
      : (an.today.other_fee_count > 0 ? '包含非 USDT 手续费，无法准确计算净利润' : '');
    todayPnl.className = `font-display font-semibold tabular-nums ${
      realizedNet === null ? 'text-[#526a73]' : realizedNet >= 0 ? 'text-[#0a7052]' : 'text-[#bd2b2b]'
    }`;

    renderRanking(an.symbols, pendingCloseFills);
    renderCosts(an.costs);
    renderExecutionIssues(an.execution_issues);

    renderTierExposure(td.exposure);
    if (state.ledgerAvailable) {
      document.getElementById('tier-cnt-t1').textContent = `${td.t1} 币`;
      document.getElementById('tier-cnt-t2').textContent = `${td.t2 + td.t21} 币`;
      document.getElementById('tier-cnt-t3').textContent = `${td.t3} 币`;
      document.getElementById('tier-cnt-t31').textContent = `${td.t31 + td.t32} 币`;
      document.getElementById('tier-total-badge').textContent = `${td.total} 个标的`;

      const percentagePerSymbol = td.total > 0 ? 100 / td.total : 0;
      document.getElementById('bar-t1').style.width = `${td.t1 * percentagePerSymbol}%`;
      document.getElementById('bar-t2').style.width = `${(td.t2 + td.t21) * percentagePerSymbol}%`;
      document.getElementById('bar-t3').style.width = `${td.t3 * percentagePerSymbol}%`;
      document.getElementById('bar-t31').style.width = `${(td.t31 + td.t32) * percentagePerSymbol}%`;
    } else {
      ['tier-cnt-t1', 'tier-cnt-t2', 'tier-cnt-t3', 'tier-cnt-t31'].forEach(id => {
        document.getElementById(id).textContent = '—';
      });
      document.getElementById('tier-total-badge').textContent = '— 个标的';
      ['bar-t1', 'bar-t2', 'bar-t3', 'bar-t31'].forEach(id => {
        document.getElementById(id).style.width = '0%';
      });
    }

    state.allPositions = positions;
    renderPositions();
    renderLosingPositions(state.allPositions);
    updatePositionStatusKpi(state.allPositions);

    state.rawBuyFills = buyFills;
    state.rawSellFills = sellFills;
    renderFills(state.rawBuyFills, state.rawSellFills);
  } catch (err) {
    if (err.name === 'AbortError' || activeAbortController !== controller) {
      return;
    }
    setStatusBadge('status-shell-error', '数据刷新失败', '本地看板接口请求失败');
    console.warn('API poll notice:', err);
  } finally {
    if (activeAbortController === controller) {
      activeAbortController = null;
      refreshIcon.classList.remove('animate-spin');
      refreshText.textContent = '刷新';
    }
  }
}
