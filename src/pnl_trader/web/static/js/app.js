// Main Application Entry Point (ES Module)

import { fetchData, fetchEquity } from './api.js';
import { initChartResizeObserver } from './chart.js';
import {
  state,
  renderTradingUptime,
  renderPositions,
  handleSort,
  filterTier,
  renderFills,
} from './renderers.js';

const SLOT_MS = 900_000;

function formatHm(epochMs) {
  const d = new Date(epochMs);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// 15-Minute Slot Cadence Rail & Boundary Refresh
let lastReviewSlot = Math.floor(Date.now() / SLOT_MS);
function updateCountdown() {
  const now = Date.now();
  const slot = Math.floor(now / SLOT_MS);
  const slotStartMs = slot * SLOT_MS;
  const slotEndMs = (slot + 1) * SLOT_MS;
  const elapsedMs = now - slotStartMs;
  const remainingSeconds = Math.ceil((slotEndMs - now) / 1000);
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;

  document.getElementById('countdown').textContent =
    `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  document.getElementById('slot-window-label').textContent =
    `${formatHm(slotStartMs)}–${formatHm(slotEndMs)}`;
  document.getElementById('slot-progress-fill').style.transform =
    `scaleX(${Math.min(1, Math.max(0, elapsedMs / SLOT_MS)).toFixed(4)})`;

  if (slot !== lastReviewSlot) {
    lastReviewSlot = slot;
    setTimeout(fetchData, 3500);
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

  if (view === 'fills') {
    renderFills(state.rawBuyFills, state.rawSellFills);
  }
}

export function selectPeriod(p) {
  state.currentPeriod = p;
  document.querySelectorAll('.period-btn').forEach(btn => {
    btn.setAttribute('aria-pressed', String(btn.dataset.period === p));
  });
  fetchEquity(p);
}

export function handleSearchInput() {
  if (state.currentView === 'positions') {
    renderPositions();
  } else if (state.currentView === 'fills') {
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
      switchView(view);
    });
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

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopTimers();
  } else {
    startTimers();
    fetchData();
  }
});

bindControls();
initAccessibleTabs();
initChartResizeObserver('canvas-container', () => state.chartHistoryPoints);
fetchData();
startTimers();
