// API Fetching and Data Synchronization Service

import { renderRetinaChart } from './chart.js?v=20261126-3';
import {
  state,
  renderTradingUptime,
  renderPositions,
  updatePositionStatusKpi,
  renderDrawdown,
  renderEquitySummary,
  renderAccountKpis,
  renderTierBars,
  renderLosingPositions,
  renderTierExposure,
  renderCosts,
  renderExecutionIssues,
  renderFills,
  renderRanking,
} from './renderers.js?v=20261126-3';

export { state };

let activeAbortController = null;
let equityAbortController = null;
let activeBannerSource = null;

const PERIOD_LABELS = { '24h': '24 小时', '7d': '7 天', '30d': '30 天' };

state.lastSyncedAtMs = state.lastSyncedAtMs ?? null;
state.displayedPeriod = state.displayedPeriod ?? state.currentPeriod;

export function abortInFlightRequests() {
  if (activeAbortController) {
    activeAbortController.abort();
  }
  if (equityAbortController) {
    equityAbortController.abort();
  }
}

export function syncPeriodButtons(period) {
  document.querySelectorAll('.period-btn').forEach(btn => {
    btn.setAttribute('aria-pressed', String(btn.dataset.period === period));
  });
}

function formatSyncTime(epochMs) {
  return new Date(epochMs).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function formatFetchError(err) {
  if (err instanceof TypeError) return '网络连接异常';
  return err?.message || '未知错误';
}

function updateLastSyncLabel() {
  const labelEl = document.getElementById('last-sync-label');
  if (!labelEl) return;
  labelEl.textContent = state.lastSyncedAtMs !== null
    ? ` · 最近同步 ${formatSyncTime(state.lastSyncedAtMs)}`
    : '';
}

function showErrorBanner(source, message) {
  activeBannerSource = source;
  const banner = document.getElementById('dashboard-error-banner');
  const msgEl = document.getElementById('error-banner-message');
  const metaEl = document.getElementById('error-banner-meta');
  msgEl.textContent = message;
  metaEl.textContent = state.lastSyncedAtMs !== null
    ? `上次成功同步：${formatSyncTime(state.lastSyncedAtMs)}（当前界面保留上次成功快照）`
    : '尚未成功获取本地账本快照，请检查看板服务是否正常运行。';
  banner.classList.remove('hidden');
}

function hideErrorBanner(source = null) {
  if (source !== null && activeBannerSource !== source) return;
  activeBannerSource = null;
  document.getElementById('dashboard-error-banner').classList.add('hidden');
}

function buildRetryMessageNode(className, message) {
  const wrap = document.createElement('div');
  wrap.className = className;
  const text = document.createElement('span');
  text.textContent = `${message} `;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'font-semibold text-[#135d73] underline hover:text-[#0e4a5c]';
  btn.textContent = '点击重试';
  btn.addEventListener('click', () => fetchData());
  wrap.append(text, btn);
  return wrap;
}

function setContainerErrorText(containerId, className, message) {
  const el = document.createElement('div');
  el.className = className;
  el.textContent = message;
  document.getElementById(containerId).replaceChildren(el);
}

function setEquityPeriodErrorStatus(targetPeriod, fallbackPeriod, errDetail) {
  const snapshotStatus = document.getElementById('equity-snapshot-status');
  const targetLabel = PERIOD_LABELS[targetPeriod] || targetPeriod;
  const fallbackLabel = PERIOD_LABELS[fallbackPeriod] || fallbackPeriod;
  snapshotStatus.textContent = `切换「${targetLabel}」失败（${errDetail}），当前仍显示「${fallbackLabel}」`;
  snapshotStatus.className = 'text-xs mt-0.5 tabular-nums text-[#bd2b2b] font-medium';
  snapshotStatus.setAttribute('aria-busy', 'false');
}

function renderInitialFailurePlaceholders(errDetail) {
  const tbody = document.getElementById('position-table-body');
  const row = document.createElement('tr');
  const cell = document.createElement('td');
  cell.colSpan = 9;
  cell.className = 'py-12 text-center text-[#bd2b2b] font-sans';
  cell.append(buildRetryMessageNode('inline-flex items-center gap-1', `持仓数据加载失败（${errDetail}）`));
  row.append(cell);
  tbody.replaceChildren(row);

  document.getElementById('buy-fills-list').replaceChildren(
    buildRetryMessageNode('text-[#bd2b2b] text-xs py-6 text-center', `买入成交加载失败（${errDetail}）`),
  );
  document.getElementById('sell-fills-list').replaceChildren(
    buildRetryMessageNode('text-[#bd2b2b] text-xs py-6 text-center', `卖出成交加载失败（${errDetail}）`),
  );
  document.getElementById('ranking-container').replaceChildren(
    buildRetryMessageNode('col-span-full text-[#bd2b2b] text-xs py-8 text-center', `收益排行加载失败（${errDetail}）`),
  );

  const snapshotStatus = document.getElementById('equity-snapshot-status');
  snapshotStatus.textContent = `账户净值采样加载失败（${errDetail}）`;
  snapshotStatus.className = 'text-xs mt-0.5 tabular-nums text-[#bd2b2b] font-medium';
  snapshotStatus.setAttribute('aria-busy', 'false');

  document.getElementById('position-summary-text').textContent = '同步失败，请重试';

  setContainerErrorText('execution-issues', 'text-xs text-[#bd2b2b] py-6 text-center', '执行状态加载失败');
  setContainerErrorText('tier-exposure-list', 'text-xs text-[#bd2b2b] py-4', '持仓敞口加载失败');
  setContainerErrorText('cost-days', 'text-xs text-[#bd2b2b] py-2', '交易成本加载失败');

  const losersList = document.getElementById('losers-list');
  const loserRow = document.createElement('tr');
  const loserCell = document.createElement('td');
  loserCell.colSpan = 4;
  loserCell.className = 'py-8 text-center text-[#bd2b2b]';
  loserCell.textContent = '持仓数据加载失败';
  loserRow.append(loserCell);
  losersList.replaceChildren(loserRow);
}

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
  state.displayedPeriod = eq.period;
  state.currentPeriod = eq.period;
  syncPeriodButtons(eq.period);
  renderEquitySummary(eq);
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

  const snapshotStatus = document.getElementById('equity-snapshot-status');
  const targetLabel = PERIOD_LABELS[period] || period;
  snapshotStatus.textContent = `正在加载${targetLabel}权益走势...`;
  snapshotStatus.className = 'text-xs mt-0.5 tabular-nums text-[#135d73] font-medium';
  snapshotStatus.setAttribute('aria-busy', 'true');

  try {
    const response = await fetch(`api/equity?period=${period}`, { signal, cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`);
    }
    const eq = await response.json();
    if (signal.aborted || equityAbortController !== controller) {
      return;
    }
    hideErrorBanner('equity');
    if (state.currentPeriod === period) {
      applyEquity(eq);
    }
  } catch (err) {
    if (err.name === 'AbortError' || equityAbortController !== controller) {
      return;
    }
    const errDetail = formatFetchError(err);
    const fallbackPeriod = state.displayedPeriod || '7d';
    const fallbackLabel = PERIOD_LABELS[fallbackPeriod] || fallbackPeriod;
    state.currentPeriod = fallbackPeriod;
    syncPeriodButtons(fallbackPeriod);

    setEquityPeriodErrorStatus(period, fallbackPeriod, errDetail);
    showErrorBanner('equity', `切换「${targetLabel}」权益数据失败（${errDetail}），已恢复显示「${fallbackLabel}」。`);
    console.warn('Equity poll notice:', err);
  } finally {
    if (equityAbortController === controller) {
      equityAbortController = null;
      snapshotStatus.setAttribute('aria-busy', 'false');
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
  const requestedPeriod = state.currentPeriod;

  const refreshBtn = document.getElementById('btn-refresh');
  const refreshIcon = document.getElementById('refresh-icon');
  const refreshText = document.getElementById('refresh-text');
  const retryBtn = document.getElementById('btn-retry-sync');
  refreshBtn.setAttribute('aria-busy', 'true');
  refreshIcon.classList.add('animate-spin');
  refreshText.textContent = '同步中';
  if (retryBtn) {
    retryBtn.disabled = true;
    retryBtn.textContent = '同步中...';
  }

  try {
    const response = await fetch(
      `api/dashboard?period=${requestedPeriod}&fills_limit=12`,
      { signal, cache: 'no-store' },
    );
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`);
    }
    const snapshot = await response.json();
    if (signal.aborted || activeAbortController !== controller) {
      return;
    }

    state.lastSyncedAtMs = Date.now();
    updateLastSyncLabel();
    hideErrorBanner();

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

    renderAccountKpis(ov, an);

    if (state.currentPeriod === requestedPeriod) {
      applyEquity(eq);
    }

    renderRanking(an.symbols, pendingCloseFills);
    renderCosts(an.costs);
    renderExecutionIssues(an.execution_issues);

    renderTierExposure(td.exposure);
    renderTierBars(td);

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
    const errDetail = formatFetchError(err);
    if (requestedPeriod === state.currentPeriod && requestedPeriod !== state.displayedPeriod) {
      state.currentPeriod = state.displayedPeriod;
      syncPeriodButtons(state.displayedPeriod);
      if (state.lastSyncedAtMs !== null) {
        setEquityPeriodErrorStatus(requestedPeriod, state.displayedPeriod, errDetail);
      }
    }
    setStatusBadge(
      'status-shell-error',
      '数据刷新失败',
      state.lastSyncedAtMs !== null
        ? `本地看板接口请求失败（${errDetail}）· 上次同步 ${formatSyncTime(state.lastSyncedAtMs)}`
        : `本地看板接口请求失败（${errDetail}）`,
    );
    if (state.lastSyncedAtMs === null) {
      renderInitialFailurePlaceholders(errDetail);
      showErrorBanner('dashboard', `首次同步本地账本数据失败（${errDetail}）。`);
    } else {
      showErrorBanner('dashboard', `定时同步本地账本失败（${errDetail}），当前展示可能为过期数据。`);
    }
    console.warn('API poll notice:', err);
  } finally {
    if (activeAbortController === controller) {
      activeAbortController = null;
      refreshBtn.setAttribute('aria-busy', 'false');
      refreshIcon.classList.remove('animate-spin');
      refreshText.textContent = '刷新';
      if (retryBtn) {
        retryBtn.disabled = false;
        retryBtn.textContent = '立即重试';
      }
    }
  }
}
