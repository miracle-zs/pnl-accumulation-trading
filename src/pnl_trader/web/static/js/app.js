// Main Application Entry Point (ES Module)

import { fetchData, fetchEquity, syncPeriodButtons, abortInFlightRequests } from './api.js?v=20261126-3';
import { initChartResizeObserver, renderRetinaChart } from './chart.js?v=20261126-3';
import {
  state,
  renderTradingUptime,
  renderPositions,
  handleSort,
  filterTier,
  renderFills,
} from './renderers.js?v=20261126-3';

const SLOT_MS = 900_000;

function formatHm(epochMs) {
  const d = new Date(epochMs);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

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

  const slotWindowText = `${formatHm(slotStartMs)}–${formatHm(slotEndMs)}`;
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

export function switchView(view) {
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
      panel.tabIndex = 0;
      btn.scrollIntoView({ block: 'nearest', inline: 'nearest' });
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

function scheduleSearchFilter() {
  if (searchRafId !== null) {
    cancelAnimationFrame(searchRafId);
  }
  searchRafId = requestAnimationFrame(() => {
    searchRafId = null;
    if (state.currentView === 'positions') {
      renderPositions();
    } else if (state.currentView === 'fills') {
      renderFills(state.rawBuyFills, state.rawSellFills);
    }
  });
}

export function handleSearchInput(e) {
  if (e?.isComposing) return;
  scheduleSearchFilter();
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
      switchView(view);
    });
  });
}

function bindControls() {
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
let pollTimeoutId = null;
let countdownIntervalId = null;
let uptimeIntervalId = null;
let timersActive = false;
let activeFetchSeq = 0;
let chartResizeObserver = null;
let cleanupDprListener = null;

function clearScheduledPoll() {
  if (pollTimeoutId !== null) {
    clearTimeout(pollTimeoutId);
    pollTimeoutId = null;
  }
}

function scheduleNextPoll() {
  clearScheduledPoll();
  if (!timersActive || document.hidden) return;
  pollTimeoutId = setTimeout(() => {
    pollTimeoutId = null;
    runDashboardFetch();
  }, POLL_INTERVAL_MS);
}

async function runDashboardFetch() {
  clearScheduledPoll();
  const seq = ++activeFetchSeq;
  try {
    await fetchData();
  } finally {
    if (seq === activeFetchSeq) {
      scheduleNextPoll();
    }
  }
}

function startTimers() {
  timersActive = true;
  if (!countdownIntervalId) {
    updateCountdown();
    countdownIntervalId = setInterval(updateCountdown, 1000);
  }
  if (!uptimeIntervalId) {
    renderTradingUptime();
    uptimeIntervalId = setInterval(renderTradingUptime, 1000);
  }
  if (pollTimeoutId === null) {
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
    startTimers();
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
    startTimers();
    runDashboardFetch();
  }
});

bindControls();
initAccessibleTabs();
initChartObservers();
startTimers();
runDashboardFetch();
