// Ledger Workbench: Positions Filtering/Sorting, Risk Distance Visualization, and Fills Feed

import {
  state,
  collator,
  formatBeijingTime,
  isRenderUnchanged,
  saveRenderSnapshot,
  formatSignedUsd,
  formatUsd,
  formatSignedPercent,
  formatUsdPrice,
  formatQty,
  formatCommission,
  pnlColorClass,
  createNode,
  createTimeNode,
  showTableMessage,
  showMessage,
  isClosingPosition,
  isReducingPosition,
  buildTierBadgeNode,
} from './state.js?v=20261128-1';

const TIER_FILTER_LABELS = {
  ALL: '全部层级',
  T1: 'T1',
  T2: 'T2',
  T3: 'T3',
  REDUCING: '减仓中',
  CLOSING: '待全平',
};

const PNL_FILTER_LABELS = {
  ALL: '全部盈亏',
  PROFIT: '仅浮盈',
  LOSS: '仅浮亏',
};

export function compareValues(valA, valB, type, direction) {
  const numA = type === 'number' ? Number(valA) : NaN;
  const numB = type === 'number' ? Number(valB) : NaN;
  const missingA = valA === null || valA === undefined || valA === '' || (type === 'number' && !Number.isFinite(numA));
  const missingB = valB === null || valB === undefined || valB === '' || (type === 'number' && !Number.isFinite(numB));
  if (missingA && missingB) return 0;
  if (missingA) return 1;
  if (missingB) return -1;

  const factor = direction === 'asc' ? 1 : -1;
  if (type === 'string') {
    return factor * collator.compare(String(valA), String(valB));
  }
  return factor * (numA - numB);
}

export function getSearchQuery() {
  if (state.searchQuery) return state.searchQuery;
  const input = document.getElementById('symbol-search');
  return input ? input.value.trim().toUpperCase() : '';
}

function getFilteredSortedPositions(query) {
  const filtered = state.allPositions.filter(p => {
    const matchesQuery = p.symbol.includes(query);

    let matchesTier = true;
    if (state.currentTierFilter === 'T1') matchesTier = (p.tier === 1);
    else if (state.currentTierFilter === 'T2') matchesTier = (p.tier === 2);
    else if (state.currentTierFilter === 'T3') matchesTier = (p.tier === 3);
    else if (state.currentTierFilter === 'REDUCING') matchesTier = isReducingPosition(p);
    else if (state.currentTierFilter === 'CLOSING') matchesTier = isClosingPosition(p);

    let matchesPnl = true;
    if (state.currentPnlFilter === 'PROFIT') {
      const val = Number(p.estimated_unrealized_pnl);
      matchesPnl = p.estimated_unrealized_pnl !== null && p.estimated_unrealized_pnl !== undefined && Number.isFinite(val) && val > 0;
    } else if (state.currentPnlFilter === 'LOSS') {
      const val = Number(p.estimated_unrealized_pnl);
      matchesPnl = p.estimated_unrealized_pnl !== null && p.estimated_unrealized_pnl !== undefined && Number.isFinite(val) && val < 0;
    }

    return matchesQuery && matchesTier && matchesPnl;
  });

  const activeTh = document.getElementById(`th-${state.currentSortKey}`);
  const sortType = activeTh ? activeTh.dataset.type || 'number' : 'number';
  filtered.sort((a, b) => {
    const cmp = compareValues(
      a[state.currentSortKey],
      b[state.currentSortKey],
      sortType,
      state.currentSortDirection,
    );
    if (cmp !== 0) return cmp;
    return collator.compare(a.symbol, b.symbol);
  });
  return filtered;
}

function buildPositionsRenderKey(filtered, query) {
  return JSON.stringify([
    state.allPositions.length,
    state.currentTierFilter,
    state.currentPnlFilter,
    query,
    state.currentSortKey,
    state.currentSortDirection,
    state.highlightedSymbol,
    filtered.map(p => [
      p.symbol,
      p.tier,
      p.lifecycle,
      p.tier_label,
      p.completed_batches,
      p.avg_entry_price,
      p.scan_price,
      p.tp_price,
      p.writeoff_price,
      p.dca_price,
      p.qty,
      p.entry_notional,
      p.estimated_unrealized_pnl,
    ]),
  ]);
}

function buildDistanceBar(ratio, fillColor) {
  const track = createNode('div', 'viz-bar-track w-16 ml-auto h-[3px] rounded-sm bg-[#eaf0ee] overflow-hidden mt-1');
  track.setAttribute('aria-hidden', 'true');
  const fill = createNode('div', 'viz-bar-fill h-full rounded-sm');
  fill.style.width = `${Math.max(4, Math.min(100, ratio * 100)).toFixed(1)}%`;
  fill.style.backgroundColor = fillColor;
  track.append(fill);
  return track;
}

function buildTakeProfitCell(p) {
  const tpPrice = Number(p.tp_price);
  const tpHasPrice = p.tp_price !== null && p.tp_price !== undefined && Number.isFinite(tpPrice);
  if (!tpHasPrice) {
    return createNode('td', 'text-right font-mono tabular-nums text-[#526a73]', '—');
  }

  const cell = createNode('td', 'text-right tabular-nums');
  cell.append(createNode('div', 'font-mono font-semibold text-[#0a7052]', formatUsdPrice(tpPrice)));

  const scanPrice = Number(p.scan_price);
  if (p.scan_price !== null && p.scan_price !== undefined && Number.isFinite(scanPrice) && scanPrice > 0) {
    const distPct = ((tpPrice - scanPrice) / scanPrice) * 100;
    const isReached = distPct <= 0;
    const isNear = !isReached && distPct <= 3;
    const labelText = isReached ? '已达止盈线' : `距止盈 ${formatSignedPercent(distPct, 1)}`;
    const toneClass = isReached || isNear ? 'text-[#0a7052] font-semibold' : 'text-[#526a73]';
    cell.append(createNode('div', `text-xs leading-tight mt-0.5 ${toneClass}`, labelText));

    const avgEntry = Number(p.avg_entry_price);
    if (Number.isFinite(avgEntry) && tpPrice > avgEntry) {
      const progress = Math.max(0, Math.min(1, (scanPrice - avgEntry) / (tpPrice - avgEntry)));
      cell.append(buildDistanceBar(progress, '#0a7052'));
    }
  }
  return cell;
}

function buildRiskTriggerCell(p) {
  const writeoffPrice = Number(p.writeoff_price);
  const dcaPrice = Number(p.dca_price);
  const scanPrice = Number(p.scan_price);
  const hasScan = p.scan_price !== null && p.scan_price !== undefined && Number.isFinite(scanPrice) && scanPrice > 0;

  if (p.writeoff_price !== null && p.writeoff_price !== undefined && Number.isFinite(writeoffPrice)) {
    const cell = createNode('td', 'text-right tabular-nums');
    const top = createNode('div', 'text-[#5c3d91] font-semibold');
    top.append(document.createTextNode('注销价 '), createNode('span', 'font-mono', formatUsdPrice(writeoffPrice)));
    cell.append(top);

    if (hasScan) {
      const distPct = ((writeoffPrice - scanPrice) / scanPrice) * 100;
      const triggered = distPct >= 0;
      const near = !triggered && distPct >= -12;
      const note = triggered ? '已触及出清' : `距出清 ${formatSignedPercent(distPct, 1)}`;
      const tone = triggered ? 'text-[#bd2b2b] font-semibold' : near ? 'text-[#5c3d91] font-semibold' : 'text-[#526a73]';
      cell.append(createNode('div', `text-xs leading-tight mt-0.5 ${tone}`, note));

      const proximity = triggered ? 1 : Math.max(0, Math.min(1, 1 - Math.abs(distPct) / 60));
      cell.append(buildDistanceBar(proximity, triggered ? '#bd2b2b' : '#7250ad'));
    }
    return cell;
  }

  if (p.dca_price !== null && p.dca_price !== undefined && Number.isFinite(dcaPrice)) {
    const cell = createNode('td', 'text-right tabular-nums');
    const top = createNode('div', 'text-[#985506]');
    top.append(createNode('span', 'font-mono font-medium', formatUsdPrice(dcaPrice)), document.createTextNode(' -50%'));
    cell.append(top);

    if (hasScan) {
      const distPct = ((dcaPrice - scanPrice) / scanPrice) * 100;
      const triggered = distPct >= 0;
      const near = !triggered && distPct >= -12;
      const note = triggered ? '已触及补仓' : `距补仓 ${formatSignedPercent(distPct, 1)}`;
      const tone = triggered ? 'text-[#bd2b2b] font-semibold' : near ? 'text-[#985506] font-semibold' : 'text-[#526a73]';
      cell.append(createNode('div', `text-xs leading-tight mt-0.5 ${tone}`, note));

      const proximity = triggered ? 1 : Math.max(0, Math.min(1, 1 - Math.abs(distPct) / 50));
      cell.append(buildDistanceBar(proximity, triggered ? '#bd2b2b' : '#b86c0e'));
    }
    return cell;
  }

  return createNode('td', 'text-right tabular-nums text-[#526a73]', '—');
}

function populatePositionRow(row, p) {
  row.dataset.symbol = p.symbol;
  row.dataset.tier = String(p.tier);
  row.dataset.scan_price = p.scan_price === null || p.scan_price === undefined ? '' : String(p.scan_price);
  row.dataset.entry_notional = p.entry_notional === null || p.entry_notional === undefined ? '' : String(p.entry_notional);
  row.dataset.estimated_unrealized_pnl =
    p.estimated_unrealized_pnl === null || p.estimated_unrealized_pnl === undefined
      ? ''
      : String(p.estimated_unrealized_pnl);
  row.className = state.highlightedSymbol === p.symbol ? 'row-locate-highlight' : '';

  const symbolHeader = createNode('th', 'font-display', p.symbol);
  symbolHeader.scope = 'row';

  const tierCell = createNode('td', '');
  tierCell.append(buildTierBadgeNode(p, 'font-sans'));

  const tpCell = buildTakeProfitCell(p);
  const scanPriceStr = formatUsdPrice(p.scan_price);
  const scanHasPrice = scanPriceStr !== '—';
  const riskCell = buildRiskTriggerCell(p);

  const unpnlCell = createNode('td', 'text-right font-display tabular-nums pr-4');
  if (
    p.estimated_unrealized_pnl !== null
    && p.estimated_unrealized_pnl !== undefined
    && Number.isFinite(Number(p.estimated_unrealized_pnl))
  ) {
    const pnlVal = Number(p.estimated_unrealized_pnl);
    const pnlClass = pnlColorClass(pnlVal);
    const avgEntryPrice = Number(p.avg_entry_price);
    const scanPrice = Number(p.scan_price);
    const pnlPct = (
      p.avg_entry_price !== null
      && p.avg_entry_price !== undefined
      && Number.isFinite(avgEntryPrice)
      && avgEntryPrice > 0
      && p.scan_price !== null
      && p.scan_price !== undefined
      && Number.isFinite(scanPrice)
    ) ? ((scanPrice - avgEntryPrice) / avgEntryPrice) * 100 : null;
    unpnlCell.append(createNode('span', `font-bold ${pnlClass}`, formatSignedUsd(pnlVal)));
    if (pnlPct !== null) {
      unpnlCell.append(createNode('span', `text-xs ml-1.5 font-medium ${pnlClass}`, formatSignedPercent(pnlPct)));
    }
  } else {
    unpnlCell.textContent = '—';
    unpnlCell.className = 'text-right font-display tabular-nums pr-4 text-[#526a73]';
  }

  row.replaceChildren(
    symbolHeader,
    tierCell,
    createNode('td', 'text-right font-mono tabular-nums text-[#3f5760]', formatUsdPrice(p.avg_entry_price)),
    createNode(
      'td',
      scanHasPrice
        ? 'text-right font-mono tabular-nums text-[#102128] font-medium'
        : 'text-right font-mono tabular-nums text-[#526a73]',
      scanPriceStr,
    ),
    tpCell,
    riskCell,
    createNode('td', 'text-right font-mono tabular-nums text-[#3f5760]', formatQty(p.qty)),
    createNode('td', 'text-right font-display tabular-nums text-[#102128] font-semibold', formatUsd(p.entry_notional)),
    unpnlCell,
  );
  return row;
}

function reorderRowsInPlace(tbody, sortKey, sortType, direction) {
  const rows = Array.from(tbody.rows).filter(row => row.dataset.symbol);
  if (rows.length <= 1) return;

  rows.sort((rowA, rowB) => {
    const rawA = rowA.dataset[sortKey];
    const rawB = rowB.dataset[sortKey];
    const cmp = compareValues(
      rawA === '' ? null : rawA,
      rawB === '' ? null : rawB,
      sortType,
      direction,
    );
    if (cmp !== 0) return cmp;
    return collator.compare(rowA.dataset.symbol || '', rowB.dataset.symbol || '');
  });

  const canMoveBefore = typeof tbody.moveBefore === 'function';
  for (const row of rows) {
    if (canMoveBefore) {
      tbody.moveBefore(row, null);
    } else {
      tbody.appendChild(row);
    }
  }
}

export function handleSort(key) {
  if (state.currentSortKey === key) {
    state.currentSortDirection = state.currentSortDirection === 'asc' ? 'desc' : 'asc';
  } else {
    state.currentSortKey = key;
    state.currentSortDirection = key === 'symbol' ? 'asc' : 'desc';
  }

  const sortHeaders = document.querySelectorAll('#positions-table thead th[id^="th-"]');
  let activeLabel = key;
  let activeType = 'number';

  sortHeaders.forEach(th => {
    const headerKey = th.id.slice(3);
    if (headerKey === state.currentSortKey) {
      th.setAttribute('aria-sort', state.currentSortDirection === 'asc' ? 'ascending' : 'descending');
      activeLabel = th.textContent.trim();
      activeType = th.dataset.type || 'number';
    } else {
      th.removeAttribute('aria-sort');
    }
  });

  const tbody = document.getElementById('position-table-body');
  const existingDataRows = Array.from(tbody.rows).filter(row => row.dataset.symbol);
  if (existingDataRows.length > 0) {
    reorderRowsInPlace(tbody, state.currentSortKey, activeType, state.currentSortDirection);
    const query = getSearchQuery();
    const sorted = getFilteredSortedPositions(query);
    saveRenderSnapshot(tbody, buildPositionsRenderKey(sorted, query));
    const status = document.getElementById('positions-sort-status');
    const dirLabel = state.currentSortDirection === 'asc' ? '升序' : '降序';
    status.textContent = `已按${activeLabel}${dirLabel}排列，共 ${existingDataRows.length} 行`;
  } else {
    renderPositions();
  }
}

export function syncToolbarFilterStatus() {
  const query = getSearchQuery();
  const clearSearchBtn = document.getElementById('btn-clear-search');
  const searchShortcutHint = document.getElementById('search-shortcut-hint');
  if (clearSearchBtn) {
    clearSearchBtn.classList.toggle('hidden', !query);
  }
  if (searchShortcutHint) {
    searchShortcutHint.style.display = query ? 'none' : '';
  }

  const filterBar = document.getElementById('active-filter-banner');
  const filterTextEl = document.getElementById('active-filter-text');
  if (!filterBar || !filterTextEl) return;

  const parts = [];
  if (state.currentView === 'positions') {
    if (state.currentTierFilter !== 'ALL') {
      parts.push(`层级: ${TIER_FILTER_LABELS[state.currentTierFilter] || state.currentTierFilter}`);
    }
    if (state.currentPnlFilter !== 'ALL') {
      parts.push(`盈亏: ${PNL_FILTER_LABELS[state.currentPnlFilter] || state.currentPnlFilter}`);
    }
  }
  if (query && (state.currentView === 'positions' || state.currentView === 'fills')) {
    parts.push(`标的: ${query}`);
  }

  filterBar.classList.toggle('hidden', parts.length === 0);
  if (parts.length > 0) {
    filterTextEl.textContent = `已筛选 · ${parts.join(' · ')}`;
  }
}

export function filterTier(tier) {
  state.currentTierFilter = tier;
  [
    ['all', 'ALL'],
    ['t1', 'T1'],
    ['t2', 'T2'],
    ['t3', 'T3'],
    ['reducing', 'REDUCING'],
    ['closing', 'CLOSING'],
  ].forEach(([suffix, val]) => {
    const btn = document.getElementById(`filter-${suffix}`);
    if (btn) {
      btn.setAttribute('aria-pressed', String(val === tier));
    }
  });
  renderPositions();
}

export function filterPnl(pnl) {
  state.currentPnlFilter = pnl;
  [
    ['all', 'ALL'],
    ['profit', 'PROFIT'],
    ['loss', 'LOSS'],
  ].forEach(([suffix, val]) => {
    const btn = document.getElementById(`pnl-filter-${suffix}`);
    if (btn) {
      btn.setAttribute('aria-pressed', String(val === pnl));
    }
  });
  renderPositions();
}

export function updateTableScrollAffordance() {
  const wrap = document.querySelector('.table-shell-wrap');
  const shell = document.querySelector('.table-shell');
  const hint = document.getElementById('table-scroll-hint');
  if (!wrap || !shell) return;

  const hasHorizontalOverflow = shell.scrollWidth > shell.clientWidth + 1;
  const isScrolledX = shell.scrollLeft > 1;
  const hasMoreRight = shell.scrollLeft + shell.clientWidth < shell.scrollWidth - 1;

  shell.classList.toggle('is-scrolled-x', isScrolledX);
  wrap.classList.toggle('has-overflow-right', hasHorizontalOverflow && hasMoreRight);
  if (hint) {
    hint.classList.toggle('hidden', !hasHorizontalOverflow);
  }
}

export function renderPositions() {
  const tbody = document.getElementById('position-table-body');
  const summaryEl = document.getElementById('position-summary-text');
  syncToolbarFilterStatus();

  if (!state.ledgerAvailable) {
    const key = 'WAIT';
    if (isRenderUnchanged(tbody, key)) return;
    showTableMessage(tbody, 9, 'py-12 text-center text-[#526a73] font-sans', '等待账本数据');
    summaryEl.textContent = '托管持仓: —';
    saveRenderSnapshot(tbody, key);
    updateTableScrollAffordance();
    return;
  }
  if (state.allPositions.length === 0) {
    const key = 'EMPTY';
    if (isRenderUnchanged(tbody, key)) return;
    showTableMessage(tbody, 9, 'py-12 text-center text-[#526a73] font-sans', '暂无托管持仓记录');
    summaryEl.textContent = '托管持仓: 0 个标的';
    saveRenderSnapshot(tbody, key);
    updateTableScrollAffordance();
    return;
  }

  const query = getSearchQuery();
  const filtered = getFilteredSortedPositions(query);
  const key = buildPositionsRenderKey(filtered, query);
  if (isRenderUnchanged(tbody, key)) {
    updateTableScrollAffordance();
    return;
  }

  if (filtered.length === 0) {
    showTableMessage(tbody, 9, 'py-10 text-center text-[#526a73] font-sans', '无符合当前筛选条件的托管标的', {
      withResetBtn: true,
    });
    summaryEl.textContent = `显示 0 / ${state.allPositions.length} 个托管标的`;
    saveRenderSnapshot(tbody, key);
    updateTableScrollAffordance();
    return;
  }

  const existingBySymbol = new Map();
  for (const row of Array.from(tbody.rows)) {
    if (row.dataset.symbol) {
      existingBySymbol.set(row.dataset.symbol, row);
    }
  }

  const sameSequence = existingBySymbol.size === filtered.length
    && Array.from(tbody.rows).every((row, idx) => row.dataset.symbol === filtered[idx].symbol);

  if (sameSequence) {
    filtered.forEach((p, idx) => {
      populatePositionRow(tbody.rows[idx], p);
    });
  } else {
    const rows = filtered.map(p => {
      const row = existingBySymbol.get(p.symbol) || document.createElement('tr');
      return populatePositionRow(row, p);
    });
    tbody.replaceChildren(...rows);
  }

  summaryEl.textContent = `显示 ${filtered.length} / ${state.allPositions.length} 个托管标的`;
  saveRenderSnapshot(tbody, key);
  updateTableScrollAffordance();
}

export function renderFills(buyFills, sellFills) {
  syncToolbarFilterStatus();
  const query = getSearchQuery();
  const filteredBuys = query ? buyFills.filter(f => f.symbol.includes(query)) : buyFills;
  const filteredSells = query ? sellFills.filter(f => f.symbol.includes(query)) : sellFills;

  renderFillGroup('buy-fills-list', 'buy-fills-count', filteredBuys, 'BUY', buyFills.length, query);
  renderFillGroup('sell-fills-list', 'sell-fills-count', filteredSells, 'SELL', sellFills.length, query);
}

function renderFillGroup(listId, countId, fills, side, totalCount = fills.length, query = '') {
  const list = document.getElementById(listId);
  const countEl = document.getElementById(countId);

  if (!state.ledgerAvailable) {
    const key = `WAIT:${side}`;
    if (isRenderUnchanged(list, key)) return;
    countEl.textContent = '—';
    showMessage(list, 'text-[#526a73] text-xs py-6 text-center', '等待账本数据');
    saveRenderSnapshot(list, key);
    return;
  }

  const isFiltered = Boolean(query);
  const key = JSON.stringify([
    side,
    isFiltered,
    totalCount,
    state.fillsLimit,
    fills.map(f => [f.symbol, f.price, f.qty, f.realized_pnl, f.commission, f.commission_asset, f.traded_at_ms]),
  ]);
  if (isRenderUnchanged(list, key)) return;

  countEl.textContent = isFiltered
    ? `筛选命中 ${fills.length} / 最近 ${totalCount} 笔`
    : `最近 ${fills.length} 笔订单`;
  if (fills.length === 0) {
    const emptyText = isFiltered
      ? '无匹配该标的的成交记录'
      : (side === 'BUY' ? '暂无买入成交明细' : '暂无卖出成交明细');
    showMessage(list, 'text-[#526a73] text-xs py-6 text-center', emptyText, {
      withResetBtn: isFiltered,
    });
    saveRenderSnapshot(list, key);
    return;
  }

  const items = fills.map(fill => {
    const isBuy = side === 'BUY';
    const amount = isBuy
      ? formatUsd(Number(fill.price) * Number(fill.qty))
      : formatSignedUsd(fill.realized_pnl);
    const amountLabel = isBuy ? '成交额' : '已实现毛盈亏';
    const amountClass = isBuy ? 'text-[#102128]' : pnlColorClass(fill.realized_pnl);

    const item = createNode('article', 'py-3 flex items-center justify-between gap-4 text-xs');
    const identity = createNode('div', 'min-w-0');
    identity.append(
      createNode('p', 'font-display font-semibold text-[#102128] truncate', fill.symbol),
      createNode(
        'p',
        'text-[#526a73] text-xs mt-0.5 font-mono tabular-nums',
        `均价 ${formatUsdPrice(fill.price)} · 数量 ${formatQty(fill.qty)}`,
      ),
    );

    const feeText = formatCommission(fill.commission, fill.commission_asset);
    if (feeText !== null) {
      const isNonUsdt = fill.commission_asset && fill.commission_asset !== 'USDT';
      const feeLine = createNode(
        'p',
        `text-xs mt-0.5 font-mono tabular-nums ${isNonUsdt ? 'text-[#985506] font-medium' : 'text-[#526a73]'}`,
        isNonUsdt ? `规费 ${feeText} (非USDT)` : `规费 ${feeText}`,
      );
      identity.append(feeLine);
    }

    const details = createNode('div', 'shrink-0 text-right');
    const timeNode = createTimeNode(fill.traded_at_ms, 'block mt-0.5 text-xs text-[#526a73] font-mono tabular-nums', formatBeijingTime);
    details.append(
      createNode('span', 'block text-xs text-[#526a73]', amountLabel),
      createNode('span', `block font-display font-semibold tabular-nums ${amountClass}`, amount),
      timeNode,
    );
    item.append(identity, details);
    return item;
  });
  list.replaceChildren(...items);
  saveRenderSnapshot(list, key);
}
