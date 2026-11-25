// Main Application Entry Point (ES Module)

import { fetchData } from './api.js';
import { initChartResizeObserver } from './chart.js';
import {
  state,
  renderPositions,
  handleSort,
  filterTier,
  renderFills,
} from './renderers.js';

// Countdown & 15-Minute Boundary Refresh
let lastReviewSlot = Math.floor(Date.now() / 900000);
function updateCountdown() {
  const now = Date.now();
  const slot = Math.floor(now / 900000);
  const remainingSeconds = Math.ceil(((slot + 1) * 900000 - now) / 1000);
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;
  document.getElementById('countdown').textContent =
    `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  if (slot !== lastReviewSlot) {
    lastReviewSlot = slot;
    setTimeout(fetchData, 3500);
  }
}

// Uptime Counter Loop
function renderTradingUptime() {
  const uptimeEl = document.getElementById('trading-uptime');
  if (state.tradingUptimeBaseSeconds === null) {
    uptimeEl.textContent = '—';
    return;
  }
  const elapsedSeconds = state.tradingUptimeBaseSeconds +
    Math.floor((performance.now() - state.tradingUptimeUpdatedAt) / 1000);
  const totalMinutes = Math.floor(elapsedSeconds / 60);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  uptimeEl.textContent = `${days}天 ${hours}小时 ${minutes}分`;
}

export function switchView(view) {
  state.currentView = view;
  const tabs = ['positions', 'fills', 'ranking'];
  tabs.forEach(v => {
    const btn = document.getElementById(`tab-${v}`);
    const el = document.getElementById(`view-${v}`);
    const isSelected = (v === view);
    btn.setAttribute('aria-selected', String(isSelected));
    btn.tabIndex = isSelected ? 0 : -1;
    el.classList.toggle('hidden', !isSelected);
  });

  document.getElementById('tier-filter-group').style.display = (view === 'positions') ? 'inline-flex' : 'none';
  document.getElementById('positions-toolbar').style.display = (view === 'positions' || view === 'fills') ? 'flex' : 'none';
  document.getElementById('position-footer').classList.toggle('hidden', view !== 'positions');

  if (view === 'fills') {
    renderFills(state.rawBuyFills, state.rawSellFills);
  }
}

export function selectPeriod(p) {
  state.currentPeriod = p;
  document.querySelectorAll('.period-btn').forEach(btn => {
    btn.setAttribute('aria-pressed', String(btn.dataset.period === p));
  });
  fetchData();
}

export function handleSearchInput() {
  if (state.currentView === 'positions') {
    renderPositions();
  } else if (state.currentView === 'fills') {
    renderFills(state.rawBuyFills, state.rawSellFills);
  }
}

// Initialize Roving Tabindex for WAI-ARIA tablist
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
      targetTab.click();
    }
  });
}

function bindControls() {
  document.getElementById('btn-refresh').addEventListener('click', () => {
    fetchData();
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
  document.getElementById('symbol-search').addEventListener('input', handleSearchInput);
  document.querySelectorAll('.sort-header-btn').forEach(btn => {
    btn.addEventListener('click', () => handleSort(btn.dataset.sort));
  });
}

// Timers and Page Lifecycle Management (efficient background processing)
let pollIntervalId = null;
let countdownIntervalId = null;
let uptimeIntervalId = null;

function startTimers() {
  if (!countdownIntervalId) {
    updateCountdown();
    countdownIntervalId = setInterval(updateCountdown, 1000);
  }
  if (!uptimeIntervalId) {
    renderTradingUptime();
    uptimeIntervalId = setInterval(renderTradingUptime, 1000);
  }
  if (!pollIntervalId) {
    pollIntervalId = setInterval(fetchData, 30000);
  }
}

function stopTimers() {
  if (pollIntervalId) {
    clearInterval(pollIntervalId);
    pollIntervalId = null;
  }
  if (countdownIntervalId) {
    clearInterval(countdownIntervalId);
    countdownIntervalId = null;
  }
  if (uptimeIntervalId) {
    clearInterval(uptimeIntervalId);
    uptimeIntervalId = null;
  }
}

// Conserve CPU & battery by pausing background timers when tab is hidden
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopTimers();
  } else {
    startTimers();
    // Immediately catch up with fresh data upon foregrounding
    fetchData();
  }
});

bindControls();
initAccessibleTabs();
initChartResizeObserver('canvas-container', () => state.chartHistoryPoints);
fetchData();
startTimers();
