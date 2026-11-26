// Main Application Entry Point (ES Module)

import {
  fetchData,
  fetchEquity,
  syncPeriodButtons,
  abortInFlightRequests,
  setRetryHandler,
  setOfflineBadge,
} from './api.js?v=20261126-4';
import { initChartResizeObserver, renderRetinaChart } from './chart.js?v=20261126-4';
import {
  state,
  formatBeijingHm,
  renderTradingUptime,
  renderPositions,
  handleSort,
  filterTier,
  renderFills,
  updateTableScrollAffordance,
  updateExecutionIssuesScrollHint,
} from './renderers.js?v=20261126-4';

const SLOT_MS = 900_000;
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// 15-Minute Slot Cadence Rail & Boundary Refresh
let lastReviewSlot = Math.floor(Date.now() / SLOT_MS);
let slotBoundaryTimeoutId = null;
let countdownEl = null;
let slotWindowLabelEl = null;
let slotProgressFillEl = null;
let lastCountdownText = '';
let lastSlotWindowText = '';
let lastProgressTransform = '';

function updateCountdown() {
  const now = Date.now();
  const slot = Math.floor(now / SLOT_MS);
  const slotStartMs = slot * SLOT_MS;
  const slotEndMs = (slot + 1) * SLOT_MS;
  const elapsedMs = now - slotStartMs;
  const remainingSeconds = Math.ceil((slotEndMs - now) / 1000);
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;

  if (!countdownEl) countdownEl = document.getElementById('countdown');
  if (!slotWindowLabelEl) slotWindowLabelEl = document.getElementById('slot-window-label');
  if (!slotProgressFillEl) slotProgressFillEl = document.getElementById('slot-progress-fill');

  const countdownText = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  if (countdownText !== lastCountdownText) {
    lastCountdownText = countdownText;
    countdownEl.textContent = countdownText;
  }

  const slotWindowText = `${formatBeijingHm(slotStartMs)}–${formatBeijingHm(slotEndMs)}`;
  if (slotWindowText !== lastSlotWindowText) {
    lastSlotWindowText = slotWindowText;
    slotWindowLabelEl.textContent = slotWindowText;
  }

  const progressTransform = `scaleX(${Math.min(1, Math.max(0, elapsedMs / SLOT_MS)).toFixed(4)})`;
  if (progressTransform !== lastProgressTransform) {
    lastProgressTransform = progressTransform;
    slotProgressFillEl.style.transform = progressTransform;
  }

  if (slot !== lastReviewSlot) {
    lastReviewSlot = slot;
    if (slotBoundaryTimeoutId !== null) {
      clearTimeout(slotBoundaryTimeoutId);
    }
    slotBoundaryTimeoutId = setTimeout(() => {
      slotBoundaryTimeoutId = null;
      runDashboardFetch();
    }, 3500);
  }
}

export function switchView(view, { skipScrollIntoView = false } = {}) {
  state.currentView = view;
  const tabs = ['positions', 'fills', 'ranking'];
  tabs.forEach(v => {
    const btn = document.getElementById(`tab-${v}`);
    const panel = document.getElementById(`view-${v}`);
    const isSelected = (v === view);
    btn.setAttribute('aria-selected', String(isSelected));
    btn.tabIndex = isSelected ? 0 : -1;
    if (isSelected) {
      panel.hidden = false;
      const hasFocusableChild = panel.querySelector(FOCUSABLE_SELECTOR) !== null;
      if (hasFocusableChild) {
        panel.removeAttribute('tabindex');
      } else {
        panel.tabIndex = 0;
      }
      if (!skipScrollIntoView) {
        btn.scrollIntoView({ block: 'nearest', inline: 'nearest', container: 'nearest' });
      }
    } else {
      panel.hidden = 'until-found';
      panel.removeAttribute('tabindex');
    }
  });

  document.getElementById('tier-filter-group').style.display = (view === 'positions') ? 'inline-flex' : 'none';
  document.getElementById('positions-toolbar').style.display = (view === 'positions' || view === 'fills') ? 'flex' : 'none';
  document.getElementById('position-footer').classList.toggle('hidden', view !== 'positions');

  if (view === 'positions') {
    renderPositions();
    updateTableScrollAffordance();
  } else if (view === 'fills') {
    renderFills(state.rawBuyFills, state.rawSellFills);
  }
}

export function selectPeriod(p) {
  state.currentPeriod = p;
  syncPeriodButtons(p);
  fetchEquity(p);
}

let searchRafId = null;

function applySearchFilter() {
  const input = document.getElementById('symbol-search');
  state.searchQuery = input ? input.value.trim().toUpperCase() : '';
  if (state.currentView === 'positions') {
    renderPositions();
  } else if (state.currentView === 'fills') {
    renderFills(state.rawBuyFills, state.rawSellFills);
  }
}

function scheduleSearchFilter() {
  if (searchRafId !== null) {
    cancelAnimationFrame(searchRafId);
  }
  searchRafId = requestAnimationFrame(() => {
    searchRafId = null;
    applySearchFilter();
  });
}

export function handleSearchInput(e) {
  if (e?.isComposing) return;
  scheduleSearchFilter();
}

function resetFilters() {
  const input = document.getElementById('symbol-search');
  if (input) input.value = '';
  state.searchQuery = '';
  filterTier('ALL');
  if (state.currentView === 'fills') {
    renderFills(state.rawBuyFills, state.rawSellFills);
  }
}

// Accessible Tablist (selection follows focus + find-in-page beforematch sync)
function initAccessibleTabs() {
  const tablistEl = document.querySelector('[role="tablist"]');

  tablistEl.addEventListener('keydown', (e) => {
    const tabs = Array.from(tablistEl.querySelectorAll('[role="tab"]'));
    const currentIndex = tabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true');
    let nextIndex = null;

    if (e.key === 'ArrowRight') {
      nextIndex = (currentIndex + 1) % tabs.length;
    } else if (e.key === 'ArrowLeft') {
      nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    } else if (e.key === 'Home') {
      nextIndex = 0;
    } else if (e.key === 'End') {
      nextIndex = tabs.length - 1;
    }

    if (nextIndex !== null) {
      e.preventDefault();
      const targetTab = tabs[nextIndex];
      targetTab.focus();
      switchView(targetTab.dataset.view);
    }
  });

  ['positions', 'fills', 'ranking'].forEach(view => {
    const panel = document.getElementById(`view-${view}`);
    panel.addEventListener('beforematch', () => {
      switchView(view, { skipScrollIntoView: true });
    });
  });
}

function bindControls() {
  setRetryHandler(() => runDashboardFetch());

  document.getElementById('btn-refresh').addEventListener('click', () => {
    runDashboardFetch();
  });
  document.getElementById('btn-retry-sync').addEventListener('click', () => {
    runDashboardFetch();
  });
  document.querySelectorAll('.period-btn').forEach(btn => {
    btn.addEventListener('click', () => selectPeriod(btn.dataset.period));
  });
  document.querySelectorAll('[role="tab"]').forEach(tab => {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  });
  document.querySelectorAll('#tier-filter-group [data-tier]').forEach(btn => {
    btn.addEventListener('click', () => filterTier(btn.dataset.tier));
  });
  const symbolSearchInput = document.getElementById('symbol-search');
  symbolSearchInput.addEventListener('input', handleSearchInput);
  symbolSearchInput.addEventListener('compositionend', scheduleSearchFilter);

  // Filter reset delegation on empty search/filter results
  ['view-positions', 'view-fills'].forEach(panelId => {
    document.getElementById(panelId).addEventListener('click', (e) => {
      if (e.target.closest('[data-reset-filters]')) {
        resetFilters();
        symbolSearchInput.focus();
      }
    });
  });

  // Table horizontal scroll shadow/hint & execution issues scroll hint
  const tableShell = document.querySelector('.table-shell');
  if (tableShell) {
    tableShell.addEventListener('scroll', updateTableScrollAffordance, { passive: true });
  }
  const issuesEl = document.getElementById('execution-issues');
  if (issuesEl) {
    issuesEl.addEventListener('scroll', updateExecutionIssuesScrollHint, { passive: true });
  }
  window.addEventListener('resize', () => {
    updateTableScrollAffordance();
    updateExecutionIssuesScrollHint();
  }, { passive: true });

  // Invoker commands for sortable table headers with feature-detected fallback
  const positionsTable = document.getElementById('positions-table');
  positionsTable.addEventListener('command', (event) => {
    if (event.command === '--sort' && event.source?.dataset?.sort) {
      handleSort(event.source.dataset.sort);
    }
  });
  if (!('commandForElement' in HTMLButtonElement.prototype)) {
    positionsTable.tHead.querySelectorAll('button[command="--sort"]').forEach(btn => {
      btn.addEventListener('click', () => handleSort(btn.dataset.sort));
    });
  }
}

// Timers and Page Lifecycle Management
const POLL_INTERVAL_MS = 30_000;
const MAX_BACKOFF_MS = 120_000;
let pollTimeoutId = null;
let countdownIntervalId = null;
let uptimeIntervalId = null;
let timersActive = false;
let activeFetchSeq = 0;
let consecutiveFailures = 0;
let chartResizeObserver = null;
let cleanupDprListener = null;

function clearScheduledPoll() {
  if (pollTimeoutId !== null) {
    clearTimeout(pollTimeoutId);
    pollTimeoutId = null;
  }
}

function nextPollDelayMs() {
  if (consecutiveFailures === 0) return POLL_INTERVAL_MS;
  const backoff = Math.min(MAX_BACKOFF_MS, POLL_INTERVAL_MS * Math.pow(1.5, consecutiveFailures));
  const jitter = Math.floor(Math.random() * 2000);
  return Math.round(backoff + jitter);
}

function scheduleNextPoll() {
  clearScheduledPoll();
  if (!timersActive || document.hidden || navigator.onLine === false) return;
  pollTimeoutId = setTimeout(() => {
    pollTimeoutId = null;
    runDashboardFetch();
  }, nextPollDelayMs());
}

async function runDashboardFetch() {
  clearScheduledPoll();
  const seq = ++activeFetchSeq;
  try {
    const result = await fetchData();
    if (result?.ok) {
      consecutiveFailures = 0;
    } else if (!result?.aborted) {
      consecutiveFailures += 1;
    }
  } finally {
    if (seq === activeFetchSeq) {
      scheduleNextPoll();
    }
  }
}

function startTimers({ schedulePoll = true } = {}) {
  timersActive = true;
  if (!countdownIntervalId) {
    updateCountdown();
    countdownIntervalId = setInterval(updateCountdown, 1000);
  }
  if (!uptimeIntervalId) {
    renderTradingUptime();
    uptimeIntervalId = setInterval(renderTradingUptime, 15_000);
  }
  if (schedulePoll && pollTimeoutId === null) {
    scheduleNextPoll();
  }
}

function stopTimers() {
  timersActive = false;
  clearScheduledPoll();
  if (countdownIntervalId) {
    clearInterval(countdownIntervalId);
    countdownIntervalId = null;
  }
  if (uptimeIntervalId) {
    clearInterval(uptimeIntervalId);
    uptimeIntervalId = null;
  }
  if (slotBoundaryTimeoutId !== null) {
    clearTimeout(slotBoundaryTimeoutId);
    slotBoundaryTimeoutId = null;
  }
}

function watchDevicePixelRatio() {
  if (cleanupDprListener) {
    cleanupDprListener();
    cleanupDprListener = null;
  }
  if (typeof window.matchMedia !== 'function') return;

  const media = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
  const handleDprChange = () => {
    renderRetinaChart(state.chartHistoryPoints);
    watchDevicePixelRatio();
  };

  if (typeof media.addEventListener === 'function') {
    media.addEventListener('change', handleDprChange, { once: true });
    cleanupDprListener = () => media.removeEventListener('change', handleDprChange);
  } else if (typeof media.addListener === 'function') {
    media.addListener(handleDprChange);
    cleanupDprListener = () => media.removeListener(handleDprChange);
  }
}

function initChartObservers() {
  if (chartResizeObserver) {
    chartResizeObserver.disconnect();
  }
  chartResizeObserver = initChartResizeObserver('canvas-container', () => state.chartHistoryPoints);
  watchDevicePixelRatio();
}

function cleanupChartObservers() {
  if (chartResizeObserver) {
    chartResizeObserver.disconnect();
    chartResizeObserver = null;
  }
  if (cleanupDprListener) {
    cleanupDprListener();
    cleanupDprListener = null;
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopTimers();
    abortInFlightRequests();
  } else {
    lastReviewSlot = Math.floor(Date.now() / SLOT_MS);
    startTimers({ schedulePoll: false });
    runDashboardFetch();
  }
});

window.addEventListener('offline', () => {
  clearScheduledPoll();
  abortInFlightRequests();
  setOfflineBadge();
});

window.addEventListener('online', () => {
  consecutiveFailures = 0;
  if (!document.hidden) {
    runDashboardFetch();
  }
});

window.addEventListener('pagehide', (event) => {
  stopTimers();
  abortInFlightRequests();
  if (searchRafId !== null) {
    cancelAnimationFrame(searchRafId);
    searchRafId = null;
  }
  if (!event.persisted) {
    cleanupChartObservers();
  }
});

window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return;
  watchDevicePixelRatio();
  renderRetinaChart(state.chartHistoryPoints);
  if (!document.hidden) {
    lastReviewSlot = Math.floor(Date.now() / SLOT_MS);
    startTimers({ schedulePoll: false });
    runDashboardFetch();
  }
});

bindControls();
initAccessibleTabs();
initChartObservers();
startTimers({ schedulePoll: false });
runDashboardFetch();
