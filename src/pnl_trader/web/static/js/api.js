// API Fetching and Data Synchronization Service

import { renderRetinaChart, setChartVisualState } from './chart.js?v=20261126-4';
import {
  state,
  PERIOD_LABELS,
  formatBeijingTime,
  renderSnapshotStatusFromState,
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
} from './renderers.js?v=20261126-4';

const REQUEST_TIMEOUT_MS = 10_000;

let activeAbortController = null;
let equityAbortController = null;
let activeBannerSource = null;
let lastRenderedBannerKey = null;
let requestSeq = 0;
let latestBannerSeq = 0;
let latestEquityEventSeq = 0;
let retryHandler = null;

export function setRetryHandler(handler) {
  retryHandler = typeof handler === 'function' ? handler : null;
}

function triggerRetry() {
  if (retryHandler) {
    retryHandler();
  } else {
    fetchData();
  }
}

function composeRequestSignal(controllerSignal) {
  if (typeof AbortSignal?.timeout === 'function' && typeof AbortSignal?.any === 'function') {
    return AbortSignal.any([controllerSignal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]);
  }
  return controllerSignal;
}

export function abortInFlightRequests() {
  if (activeAbortController) {
    activeAbortController.abort();
  }
  if (equityAbortController) {
    equityAbortController.abort();
    const fallbackPeriod = state.displayedPeriod || '7d';
    state.currentPeriod = fallbackPeriod;
    syncPeriodButtons(fallbackPeriod);
    setChartVisualState('ready');
    renderSnapshotStatusFromState();
  }
}

export function syncPeriodButtons(period) {
  document.querySelectorAll('.period-btn').forEach(btn => {
    btn.setAttribute('aria-pressed', String(btn.dataset.period === period));
  });
}

function formatFetchError(err, { isNetworkPhase = true } = {}) {
  if (err?.name === 'TimeoutError') return '请求超时（超过 10 秒）';
  if (isNetworkPhase && err instanceof TypeError) return '网络连接异常';
  return err?.message || '未知错误';
}

function updateLastSyncLabel() {
  const labelEl = document.getElementById('last-sync-label');
  if (!labelEl) return;
  if (state.lastSyncedAtMs === null) {
    labelEl.replaceChildren();
    return;
  }
  const timeNode = document.createElement('time');
  timeNode.className = 'font-mono tabular-nums';
  timeNode.dateTime = new Date(state.lastSyncedAtMs).toISOString();
  timeNode.textContent = formatBeijingTime(state.lastSyncedAtMs);
  labelEl.replaceChildren(document.createTextNode('同步 '), timeNode);
}

function showErrorBanner(source, message, seq) {
  activeBannerSource = source;
  latestBannerSeq = Math.max(latestBannerSeq, seq);
  const banner = document.getElementById('dashboard-error-banner');
  const msgEl = document.getElementById('error-banner-message');
  const metaEl = document.getElementById('error-banner-meta');
  const metaText = state.lastSyncedAtMs !== null
    ? `上次成功同步：${formatBeijingTime(state.lastSyncedAtMs)}（当前界面保留上次成功快照）`
    : '尚未成功获取本地账本快照，请检查看板服务是否正常运行。';
  const bannerKey = `${source}|${message}|${metaText}`;
  if (lastRenderedBannerKey !== bannerKey || banner.classList.contains('hidden')) {
    msgEl.textContent = message;
    metaEl.textContent = metaText;
    lastRenderedBannerKey = bannerKey;
    banner.classList.remove('hidden');
  }
}

function hideErrorBanner(source = null, seq = Infinity) {
  if (seq < latestBannerSeq) return;
  if (source !== null && activeBannerSource !== source) return;
  activeBannerSource = null;
  lastRenderedBannerKey = null;
  document.getElementById('dashboard-error-banner').classList.add('hidden');
}

function buildRetryMessageNode(className, message) {
  const wrap = document.createElement('div');
  wrap.className = className;
  const text = document.createElement('span');
  text.textContent = `${message} `;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'min-h-6 px-2 py-1 inline-flex items-center rounded font-semibold text-[#135d73] underline hover:text-[#0e4a5c]';
  btn.textContent = '点击重试';
  btn.addEventListener('click', () => triggerRetry());
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
  setChartVisualState('error', `账户净值曲线加载失败（${errDetail}）`);

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

export function setOfflineBadge() {
  setStatusBadge('status-shell-warn', '网络已断开', '浏览器处于离线状态，恢复联网后将自动同步');
}

function applyEquity(eq) {
  state.displayedPeriod = eq.period;
  state.currentPeriod = eq.period;
  syncPeriodButtons(eq.period);
  state.chartHistoryPoints = Array.isArray(eq.points) ? eq.points : [];
  renderDrawdown(eq);
  renderEquitySummary(eq);
  renderRetinaChart(state.chartHistoryPoints, eq.period);
}

export async function fetchEquity(period) {
  if (equityAbortController) {
    equityAbortController.abort();
  }
  const controller = new AbortController();
  equityAbortController = controller;
  const seq = ++requestSeq;
  latestEquityEventSeq = seq;
  const signal = composeRequestSignal(controller.signal);

  const snapshotStatus = document.getElementById('equity-snapshot-status');
  const targetLabel = PERIOD_LABELS[period] || period;
  snapshotStatus.textContent = `正在切换至「${targetLabel}」权益走势...`;
  snapshotStatus.className = 'text-xs mt-0.5 tabular-nums text-[#135d73] font-medium';
  snapshotStatus.setAttribute('aria-busy', 'true');
  setChartVisualState('switching');

  let eq;
  try {
    const response = await fetch(`api/equity?period=${encodeURIComponent(period)}`, { signal, cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`);
    }
    eq = await response.json();
  } catch (err) {
    if (err.name === 'AbortError' || equityAbortController !== controller) {
      return { ok: false, aborted: true };
    }
    latestEquityEventSeq = Math.max(latestEquityEventSeq, seq);
    const errDetail = formatFetchError(err, { isNetworkPhase: true });
    const fallbackPeriod = state.displayedPeriod || '7d';
    const fallbackLabel = PERIOD_LABELS[fallbackPeriod] || fallbackPeriod;
    state.currentPeriod = fallbackPeriod;
    syncPeriodButtons(fallbackPeriod);
    setChartVisualState('ready');

    setEquityPeriodErrorStatus(period, fallbackPeriod, errDetail);
    showErrorBanner('equity', `切换「${targetLabel}」权益数据失败（${errDetail}），已恢复显示「${fallbackLabel}」。`, seq);
    console.warn('Equity poll notice:', err);
    return { ok: false, aborted: false };
  } finally {
    if (equityAbortController === controller) {
      equityAbortController = null;
      snapshotStatus.setAttribute('aria-busy', 'false');
    }
  }

  if (controller.signal.aborted || seq < latestEquityEventSeq) {
    return { ok: false, aborted: true };
  }

  setChartVisualState('ready');
  hideErrorBanner('equity', seq);
  if (state.currentPeriod === period) {
    applyEquity(eq);
  }
  return { ok: true, aborted: false };
}

export async function fetchData() {
  if (activeAbortController) {
    activeAbortController.abort();
  }
  if (equityAbortController) {
    equityAbortController.abort();
  }
  const controller = new AbortController();
  activeAbortController = controller;
  const seq = ++requestSeq;
  const signal = composeRequestSignal(controller.signal);
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
  if (state.lastSyncedAtMs === null) {
    setChartVisualState('loading', '正在同步本地账户权益曲线...');
  }

  let snapshot;
  try {
    const response = await fetch(
      `api/dashboard?period=${encodeURIComponent(requestedPeriod)}&fills_limit=12`,
      { signal, cache: 'no-store' },
    );
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`);
    }
    snapshot = await response.json();
  } catch (err) {
    if (err.name === 'AbortError' || activeAbortController !== controller) {
      return { ok: false, aborted: true };
    }
    const errDetail = formatFetchError(err, { isNetworkPhase: true });
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
        ? `本地看板接口请求失败（${errDetail}）· 上次同步 ${formatBeijingTime(state.lastSyncedAtMs)}`
        : `本地看板接口请求失败（${errDetail}）`,
    );
    if (state.lastSyncedAtMs === null) {
      renderInitialFailurePlaceholders(errDetail);
      showErrorBanner('dashboard', `首次同步本地账本数据失败（${errDetail}）。`, seq);
    } else {
      showErrorBanner('dashboard', `定时同步本地账本失败（${errDetail}），当前展示可能为过期数据。`, seq);
    }
    console.warn('API poll notice:', err);
    return { ok: false, aborted: false };
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

  if (controller.signal.aborted) {
    return { ok: false, aborted: true };
  }

  try {
    state.lastSyncedAtMs = Date.now();
    updateLastSyncLabel();
    hideErrorBanner(null, seq);

    const {
      overview: ov,
      equity: eq,
      tier_distribution: td,
      positions,
      buy_fills: buyFills,
      sell_fills: sellFills,
      analytics: an,
    } = snapshot;

    state.tradingUptimeBaseSeconds = ov.trading_uptime_seconds !== null && Number.isFinite(ov.trading_uptime_seconds)
      ? ov.trading_uptime_seconds
      : null;
    state.tradingUptimeUpdatedAt = performance.now();
    renderTradingUptime();

    state.ledgerAvailable = ov.status === 'LOCAL_LEDGER_ONLY';
    const pendingCloseFills = ov.pending_close_fill_count ?? 0;

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

    if (state.currentPeriod === requestedPeriod && seq >= latestEquityEventSeq) {
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
    return { ok: true, aborted: false };
  } catch (renderErr) {
    const errDetail = formatFetchError(renderErr, { isNetworkPhase: false });
    setStatusBadge('status-shell-error', '界面渲染异常', `看板渲染出错：${errDetail}`);
    showErrorBanner('render', `看板数据渲染异常（${errDetail}）。`, ++requestSeq);
    console.error('Dashboard render error:', renderErr);
    return { ok: false, aborted: false };
  }
}
