// Main Application Entry Point (ES Module)

import { state } from './state.js';
import { fetchData } from './api.js';
import { initChartResizeObserver } from './chart.js';
import { renderPositions, handleSort, filterTier } from './tables.js';
import { renderFills } from './fills.js';

// Countdown & 15-Minute Boundary Refresh
let lastReviewSlot = Math.floor(Date.now() / 900000);
function updateCountdown() {
  const now = Date.now();
  const slot = Math.floor(now / 900000);
  const remainingSeconds = Math.ceil(((slot + 1) * 900000 - now) / 1000);
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;
  const countdownEl = document.getElementById('countdown');
  if (countdownEl) {
    countdownEl.textContent = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }
  if (slot !== lastReviewSlot) {
    lastReviewSlot = slot;
    setTimeout(fetchData, 3500);
  }
}

// Uptime Counter Loop
function renderTradingUptime() {
  const uptimeEl = document.getElementById('trading-uptime');
  if (!uptimeEl) return;
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

// View Transitions-enhanced view switcher with ARIA tab semantics
export function switchView(view) {
  const applyViewChange = () => {
    state.currentView = view;
    const tabs = ['positions', 'fills', 'ranking'];
    tabs.forEach(v => {
      const btn = document.getElementById(`tab-${v}`);
      const el = document.getElementById(`view-${v}`);
      const isSelected = (v === view);
      if (btn) {
        btn.setAttribute('aria-selected', String(isSelected));
        btn.tabIndex = isSelected ? 0 : -1;
      }
      if (el) {
        if (isSelected) {
          el.classList.remove('hidden');
        } else {
          el.classList.add('hidden');
        }
      }
    });

    const tierFilterGroup = document.getElementById('tier-filter-group');
    const positionsToolbar = document.getElementById('positions-toolbar');
    const positionFooter = document.getElementById('position-footer');

    if (tierFilterGroup) tierFilterGroup.style.display = (view === 'positions') ? 'inline-flex' : 'none';
    if (positionsToolbar) positionsToolbar.style.display = (view === 'positions' || view === 'fills') ? 'flex' : 'none';
    if (positionFooter) positionFooter.classList.toggle('hidden', view !== 'positions');

    if (view === 'fills') {
      renderFills(state.rawBuyFills, state.rawSellFills);
    }
  };

  if (!document.startViewTransition) {
    applyViewChange();
  } else {
    document.startViewTransition(applyViewChange);
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
  if (!tablistEl) return;

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

// Expose handlers to window for inline onclick compatibility
window.fetchData = fetchData;
window.switchView = switchView;
window.selectPeriod = selectPeriod;
window.filterTier = filterTier;
window.handleSort = handleSort;
window.handleSearchInput = handleSearchInput;

// Application Initialization
function init() {
  updateCountdown();
  setInterval(updateCountdown, 1000);
  setInterval(renderTradingUptime, 1000);
  initAccessibleTabs();
  initChartResizeObserver('canvas-container', () => state.chartHistoryPoints);

  fetchData();
  setInterval(fetchData, 30000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
