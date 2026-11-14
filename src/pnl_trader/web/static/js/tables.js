// Positions Table Rendering, Filtering, and Semantic Sorting

import { state } from './state.js';
import {
  formatPrice,
  formatSignedUsd,
  createNode,
  showTableMessage,
  announce,
} from './formatters.js';

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

export function handleSort(key) {
  if (state.currentSortKey === key) {
    state.currentSortDirection = state.currentSortDirection === 'asc' ? 'desc' : 'asc';
  } else {
    state.currentSortKey = key;
    state.currentSortDirection = 'desc';
  }

  // Update ARIA sort attributes on the headers
  const sortKeys = ['symbol', 'tier', 'scan_price', 'entry_notional', 'estimated_unrealized_pnl'];
  sortKeys.forEach(k => {
    const th = document.getElementById('th-' + k);
    const indicator = document.getElementById('sort-' + k);
    if (th) {
      if (k === state.currentSortKey) {
        const dir = state.currentSortDirection === 'asc' ? 'ascending' : 'descending';
        th.setAttribute('aria-sort', dir);
        if (indicator) indicator.textContent = state.currentSortDirection === 'asc' ? '▲' : '▼';
      } else {
        th.removeAttribute('aria-sort');
        if (indicator) indicator.textContent = '↕';
      }
    }
  });

  announce(`表格已按 ${key} ${state.currentSortDirection === 'asc' ? '升序' : '降序'} 排序`);
  renderPositions();
}

export function filterTier(tier) {
  state.currentTierFilter = tier;
  ['all', 't1', 't2', 't3'].forEach(k => {
    const btn = document.getElementById('filter-' + k);
    if (btn) {
      if (k.toUpperCase() === tier || (k === 'all' && tier === 'ALL')) {
        btn.className = 'px-2.5 py-1 rounded-md bg-blue-600 text-white font-medium focus-visible:outline-2 focus-visible:outline-blue-500';
      } else {
        btn.className = 'px-2.5 py-1 rounded-md text-slate-400 hover:text-white focus-visible:outline-2 focus-visible:outline-blue-500';
      }
    }
  });
  renderPositions();
}

export function renderPositions() {
  const tbody = document.getElementById('position-table-body');
  const summaryEl = document.getElementById('position-summary-text');
  if (!tbody) return;

  if (!state.ledgerAvailable) {
    showTableMessage(tbody, 9, 'py-12 text-center text-slate-400 font-sans', '等待账本数据');
    if (summaryEl) summaryEl.textContent = '在管持仓: —';
    return;
  }
  if (!state.allPositions || state.allPositions.length === 0) {
    showTableMessage(tbody, 9, 'py-12 text-center text-slate-500 font-sans', '暂无在管持仓记录');
    if (summaryEl) summaryEl.textContent = '在管持仓: 0 个标的';
    return;
  }

  const searchInput = document.getElementById('symbol-search');
  const query = searchInput ? searchInput.value.trim().toUpperCase() : '';
  let filtered = state.allPositions.filter(p => {
    const matchesQuery = p.symbol.includes(query);
    let matchesTier = true;
    if (state.currentTierFilter === 'T1') matchesTier = (p.tier === 1);
    else if (state.currentTierFilter === 'T2') matchesTier = (p.tier === 2);
    else if (state.currentTierFilter === 'T3') matchesTier = (p.tier === 3);
    return matchesQuery && matchesTier;
  });

  // Stable sorting using Intl.Collator for strings and numerical comparison for values
  filtered.sort((a, b) => {
    let valA = a[state.currentSortKey];
    let valB = b[state.currentSortKey];

    if (valA === null || valA === undefined) valA = -Infinity;
    if (valB === null || valB === undefined) valB = -Infinity;

    if (typeof valA === 'string' && typeof valB === 'string') {
      return state.currentSortDirection === 'asc'
        ? collator.compare(valA, valB)
        : collator.compare(valB, valA);
    }
    return state.currentSortDirection === 'asc' ? valA - valB : valB - valA;
  });

  const rows = filtered.map(p => {
    let badgeClass = 'badge-t1';
    if (p.tier === 2) badgeClass = p.completed_batches > 0 ? 'badge-t21' : 'badge-t2';
    else if (p.tier === 3) badgeClass = p.completed_batches > 0 ? 'badge-t31' : 'badge-t3';

    const tpStr = p.tp_price === null ? '—' : `$${formatPrice(p.tp_price)}`;
    const scanPriceStr = p.scan_price !== null ? `$${formatPrice(p.scan_price)}` : '—';

    // Unrealized PnL cell with percentage
    let unpnlCell = createNode('td', 'py-3 px-4 text-right');
    if (p.estimated_unrealized_pnl !== null) {
      const pnlVal = Number(p.estimated_unrealized_pnl);
      const pnlPct = p.avg_entry_price > 0 && p.scan_price
        ? ((p.scan_price - p.avg_entry_price) / p.avg_entry_price * 100)
        : 0;
      const pnlClass = pnlVal > 0 ? 'text-emerald-400' : pnlVal < 0 ? 'text-rose-400' : 'text-slate-300';
      const sign = pnlVal > 0 ? '+' : '';
      const pctStr = `${sign}${pnlPct.toFixed(2)}%`;

      const amountSpan = createNode('span', `font-bold ${pnlClass}`, formatSignedUsd(pnlVal));
      const pctSpan = createNode('span', `text-[10px] ml-1 opacity-80 font-normal ${pnlClass}`, `(${pctStr})`);
      unpnlCell.append(amountSpan, pctSpan);
      unpnlCell.dataset.value = pnlVal;
    } else {
      unpnlCell.textContent = '—';
      unpnlCell.className = 'py-3 px-4 text-right text-slate-500';
      unpnlCell.dataset.value = -Infinity;
    }

    const row = createNode('tr', 'hover:bg-slate-800/50 transition-colors');
    row.append(
      createNode('td', 'py-3 px-4 font-bold text-white font-sans', p.symbol),
    );
    const tierCell = createNode('td', 'py-3 px-3');
    tierCell.append(createNode('span', `${badgeClass} px-2 py-0.5 rounded text-[11px] font-sans`, p.tier_label));
    row.append(tierCell);
    row.append(createNode('td', 'py-3 px-3 text-slate-300', `$${formatPrice(p.avg_entry_price)}`));
    row.append(createNode('td', 'py-3 px-3 text-slate-200 font-medium', scanPriceStr));
    row.append(createNode('td', 'py-3 px-3 text-emerald-400 font-bold', tpStr));

    const riskCell = createNode('td', 'py-3 px-3 text-slate-400');
    if (p.writeoff_price) {
      riskCell.append(createNode('span', 'text-purple-400 font-semibold', `$${formatPrice(p.writeoff_price)} (出清)`));
    } else if (p.dca_price) {
      riskCell.append(createNode('span', 'text-amber-400/90 font-mono', `$${formatPrice(p.dca_price)} (-50%)`));
    } else {
      riskCell.textContent = '—';
    }
    row.append(riskCell);
    row.append(createNode('td', 'py-3 px-3 text-slate-300', p.qty));
    row.append(createNode('td', 'py-3 px-4 text-right text-slate-100 font-bold', `$${p.entry_notional.toFixed(2)}`));
    row.append(unpnlCell);
    return row;
  });

  if (rows.length === 0) {
    showTableMessage(tbody, 9, 'py-8 text-center text-slate-500 font-sans', '无符合当前层级或搜索条件的持仓标的');
    if (summaryEl) summaryEl.textContent = `显示 0 / ${state.allPositions.length} 个在管标的`;
    return;
  }
  tbody.replaceChildren(...rows);
  if (summaryEl) summaryEl.textContent = `显示 ${filtered.length} / ${state.allPositions.length} 个在管标的`;
}
