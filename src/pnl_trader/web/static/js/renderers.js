// Centralized UI State, Formatters, and Dashboard Panel Renderers

export const state = {
  allPositions: [],
  currentTierFilter: 'ALL',
  currentPeriod: '7d',
  chartHistoryPoints: [],
  ledgerAvailable: false,
  tradingUptimeBaseSeconds: null,
  tradingUptimeUpdatedAt: 0,
  currentView: 'positions',
  rawBuyFills: [],
  rawSellFills: [],
  currentSortKey: 'tier',
  currentSortDirection: 'desc',
};

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

const CRITICAL_ISSUE_CATEGORIES = new Set([
  'REVIEW_RUN_ERROR',
  'REVIEW_ERROR',
  'ORDER_REJECTED',
  'ORDER_UNKNOWN',
  'ORDER_PARTIALLY_FILLED',
  'INTENT_CONFLICT',
  'LEVERAGE_UNAVAILABLE',
]);

export function formatSignedUsd(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  const amount = Number(value);
  const sign = amount > 0 ? '+' : amount < 0 ? '-' : '';
  return `${sign}$${Math.abs(amount).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatUsd(value) {
  if (value === null || value === undefined) return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  return `$${amount.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatPrice(value) {
  if (value === null || value === undefined) return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  if (amount === 0) return '0';
  const magnitude = Math.abs(amount);
  if (magnitude < 1e-20) return amount.toExponential(6);
  const fractionDigits = Math.min(20, Math.max(4, 5 - Math.floor(Math.log10(magnitude))));
  return amount.toLocaleString('en-US', { maximumFractionDigits: fractionDigits });
}

function createNode(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function showTableMessage(container, columns, className, message) {
  const cell = createNode('td', className, message);
  cell.colSpan = columns;
  const row = document.createElement('tr');
  row.append(cell);
  container.replaceChildren(row);
}

function showMessage(container, className, message) {
  container.replaceChildren(createNode('div', className, message));
}

function tierBadgeClass(position) {
  if (position.tier === 2) return position.completed_batches > 0 ? 'badge-t21' : 'badge-t2';
  if (position.tier === 3) return position.completed_batches > 0 ? 'badge-t31' : 'badge-t3';
  return 'badge-t1';
}

export function renderTradingUptime() {
  const wrapEl = document.getElementById('trading-uptime-wrap');
  const dividerEl = document.getElementById('trading-uptime-divider');
  const uptimeEl = document.getElementById('trading-uptime');

  if (state.tradingUptimeBaseSeconds === null) {
    wrapEl.className = 'hidden flex-col text-xs leading-tight shrink-0';
    dividerEl.className = 'h-6 w-px bg-[#255362] hidden';
    uptimeEl.textContent = '—';
    return;
  }

  wrapEl.className = 'hidden sm:flex flex-col text-xs leading-tight shrink-0';
  dividerEl.className = 'h-6 w-px bg-[#255362] hidden sm:block';

  const elapsedSeconds = state.tradingUptimeBaseSeconds +
    Math.floor((performance.now() - state.tradingUptimeUpdatedAt) / 1000);
  const totalMinutes = Math.floor(elapsedSeconds / 60);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  uptimeEl.textContent = `${days}天 ${hours}时 ${minutes}分`;
}

export function updatePositionStatusKpi(positions) {
  const statusEl = document.getElementById('kpi-position-status');
  if (!state.ledgerAvailable || positions.length === 0) {
    statusEl.textContent = '暂无持仓';
    statusEl.className = 'font-display font-medium tabular-nums text-[#526a73]';
    return;
  }
  const priced = positions.filter(p => Number.isFinite(p.estimated_unrealized_pnl));
  if (priced.length === 0) {
    statusEl.textContent = '价格采样中';
    statusEl.className = 'font-display font-medium tabular-nums text-[#526a73]';
    return;
  }
  const profitCount = priced.filter(p => p.estimated_unrealized_pnl > 0).length;
  const lossCount = priced.filter(p => p.estimated_unrealized_pnl < 0).length;
  statusEl.textContent = `${profitCount} 盈 / ${lossCount} 亏`;
  statusEl.className = `font-display font-semibold tabular-nums ${lossCount > 0 ? 'text-[#985506]' : 'text-[#0a7052]'}`;
}

export function renderDrawdown(equity) {
  const periodNames = { '24h': '最近 24 小时', '7d': '最近 7 天', '30d': '最近 30 天' };
  document.getElementById('drawdown-period').textContent = periodNames[equity.period];
  const hasSamples = equity.points.length > 0;
  document.getElementById('max-drawdown').textContent = hasSamples && equity.max_drawdown !== null
    ? formatUsd(equity.max_drawdown) : '—';
  document.getElementById('max-drawdown-pct').textContent = hasSamples && equity.max_drawdown_pct !== null
    ? `${equity.max_drawdown_pct.toFixed(2)}%` : '采样不足';
  document.getElementById('current-drawdown-pct').textContent = hasSamples && equity.current_drawdown_pct !== null
    ? `${equity.current_drawdown_pct.toFixed(2)}%` : '—';
}

export function renderLosingPositions(positions) {
  const body = document.getElementById('losers-list');
  const countEl = document.getElementById('losers-count');

  if (!state.ledgerAvailable) {
    countEl.textContent = '等待账本';
    showTableMessage(body, 4, 'py-8 text-center text-[#526a73]', '等待账本数据');
    return;
  }
  if (positions.length === 0) {
    countEl.textContent = '0 持仓';
    showTableMessage(body, 4, 'py-8 text-center text-[#526a73]', '暂无在管持仓');
    return;
  }
  const pricedPositions = positions
    .filter(position => Number.isFinite(position.estimated_unrealized_pnl));
  const losses = pricedPositions
    .filter(position => position.estimated_unrealized_pnl < 0)
    .sort((a, b) => a.estimated_unrealized_pnl - b.estimated_unrealized_pnl);

  countEl.textContent = pricedPositions.length > 0
    ? `${losses.length} 浮亏 · ${pricedPositions.length}/${positions.length} 已估值`
    : '等待价格采样';

  if (losses.length === 0) {
    const message = pricedPositions.length > 0
      ? '当前无浮亏持仓'
      : '持仓价格将在下一轮巡检后显示';
    showTableMessage(body, 4, 'py-8 text-center text-[#0a7052]', message);
    return;
  }

  const rows = losses.slice(0, 6).map(position => {
    const sampledAt = position.price_captured_at_ms
      ? new Date(position.price_captured_at_ms).toLocaleTimeString('zh-CN', {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })
      : '—';
    const row = createNode('tr', 'hover:bg-[#f2f7f5] transition-colors');
    const tierCell = createNode('td', 'py-2 px-2');
    tierCell.append(createNode('span', `tier-badge ${tierBadgeClass(position)}`, position.tier_label));
    row.append(
      createNode('td', 'py-2 pr-2 font-display font-semibold text-[#102128]', position.symbol),
      tierCell,
      createNode('td', 'py-2 px-2 text-right font-mono tabular-nums font-semibold text-[#bd2b2b]', formatSignedUsd(position.estimated_unrealized_pnl)),
      createNode('td', 'py-2 pl-2 text-right font-mono tabular-nums text-[#526a73]', sampledAt),
    );
    return row;
  });
  body.replaceChildren(...rows);
}

export function renderTierExposure(exposure) {
  const container = document.getElementById('tier-exposure-list');
  const countEl = document.getElementById('exposure-priced-count');

  if (!state.ledgerAvailable || exposure.total_positions === 0) {
    countEl.textContent = state.ledgerAvailable ? '暂无持仓' : '等待账本';
    showMessage(container, 'text-xs text-[#526a73] py-4', '暂无在管持仓');
    return;
  }
  countEl.textContent = `${exposure.priced_positions}/${exposure.total_positions} 已估值`;

  const tiers = [
    { key: 't1', label: 'T1 敞口', barColor: '#1a7692' },
    { key: 't2', label: 'T2 敞口', barColor: '#c97b18' },
    { key: 't3', label: 'T3 敞口', barColor: '#cc3b3b', title: '包含 T3 深度补仓与 T3.1/T3.2 阶梯减仓合计敞口' },
  ];

  const rows = tiers.map(tier => {
    const amount = exposure[tier.key];
    const ratio = exposure.total > 0 ? (amount / exposure.total) * 100 : 0;
    const row = document.createElement('div');
    if (tier.title) row.title = tier.title;
    const labels = createNode('div', 'flex items-center justify-between gap-2 text-xs mb-1.5');
    const amountLabel = createNode('span', 'font-display tabular-nums text-[#102128] font-semibold whitespace-nowrap');
    amountLabel.append(
      document.createTextNode(`${formatUsd(amount)} `),
      createNode('span', 'text-[#526a73] font-normal', `${ratio.toFixed(1)}%`),
    );
    labels.append(createNode('span', 'text-[#3f5760]', tier.label), amountLabel);

    const track = createNode('div', 'h-2 rounded-sm bg-[#eaf0ee] overflow-hidden');
    const bar = createNode('div', 'h-full rounded-sm');
    bar.style.width = `${ratio}%`;
    bar.style.backgroundColor = tier.barColor;
    track.append(bar);
    row.append(labels, track);
    return row;
  });

  const total = createNode('div', 'flex items-center justify-between border-t border-[#e3ece9] pt-2.5 text-xs');
  total.append(
    createNode('span', 'text-[#526a73]', '已估值总敞口'),
    createNode('span', 'font-display tabular-nums font-bold text-[#102128]', formatUsd(exposure.total)),
  );
  container.replaceChildren(...rows, total);
}

export function renderCosts(costs) {
  const rows = document.getElementById('cost-days');
  const weeklyCommEl = document.getElementById('weekly-commission');
  const weeklyCountEl = document.getElementById('weekly-fill-count');
  const otherFeesNote = document.getElementById('other-fees-note');

  if (!state.ledgerAvailable) {
    weeklyCommEl.textContent = '—';
    weeklyCountEl.textContent = '—';
    otherFeesNote.classList.add('hidden');
    showMessage(rows, 'text-xs text-[#526a73] py-2', '等待账本数据');
    return;
  }
  weeklyCommEl.textContent = formatUsd(costs.commission_usdt);
  weeklyCountEl.textContent = `${costs.fill_count} 笔`;

  const warnings = [];
  if (costs.other_fee_count > 0) warnings.push(`含 ${costs.other_fee_count} 笔非 USDT 规费（如 BNB），未折算入总额。`);
  if (costs.pending_close_fill_count > 0) warnings.push(`含 ${costs.pending_close_fill_count} 笔待对账平仓，规费统计可能未完结。`);
  otherFeesNote.textContent = warnings.join(' ');
  otherFeesNote.classList.toggle('hidden', warnings.length === 0);

  const days = costs.days;
  if (days.length === 0) {
    showMessage(rows, 'text-xs text-[#526a73] py-2', '近 7 日暂无成交');
    return;
  }

  const recentDays = days.slice(-7).reverse();
  const maxDayCost = Math.max(...recentDays.map(d => Number(d.commission_usdt) || 0), 0.0001);

  const dayRows = recentDays.map(day => {
    const ratio = Math.min(100, (Number(day.commission_usdt) / maxDayCost) * 100);
    const row = createNode('div', 'grid grid-cols-[44px_1fr_auto] items-center gap-2.5 text-xs');
    const date = createNode('time', 'text-[#526a73] font-mono tabular-nums', day.day.slice(5));
    date.dateTime = day.day;

    const barWrap = createNode('div', 'flex items-center gap-2 min-w-0');
    const track = createNode('div', 'flex-1 h-1.5 rounded-sm bg-[#eaf0ee] overflow-hidden');
    const fill = createNode('div', 'h-full rounded-sm bg-[#1a7692]');
    fill.style.width = `${day.commission_usdt > 0 ? Math.max(6, ratio) : 0}%`;
    track.append(fill);
    barWrap.append(track, createNode('span', 'text-xs tabular-nums text-[#526a73] w-9 text-right shrink-0', `${day.fill_count}笔`));

    row.append(
      date,
      barWrap,
      createNode('span', 'text-[#102128] font-mono tabular-nums font-medium text-right w-14', formatUsd(day.commission_usdt)),
    );
    return row;
  });
  rows.replaceChildren(...dayRows);
}

export function renderExecutionIssues(issues) {
  const container = document.getElementById('execution-issues');
  const countEl = document.getElementById('execution-issue-count');

  const labels = {
    PRICE_MISSING: '行情价格缺失',
    LEVERAGE_UNAVAILABLE: '合约杠杆不可用',
    INTENT_CONFLICT: '委托意图状态冲突',
    ORDER_PENDING: '委托等待成交回报',
    ORDER_PARTIALLY_FILLED: '部分成交挂起',
    ORDER_BELOW_MINIMUM: '未达最小下单名义额',
    REVIEW_ERROR: '单币巡检异常',
    REVIEW_RUN_ERROR: '全局巡检批处理失败',
    ORDER_UNKNOWN: '委托结果未决 (需对账)',
    ORDER_REJECTED: '币安拒绝委托',
    ORDER_CANCELED: '委托已撤销',
    ORDER_EXPIRED: '委托已过期',
  };

  if (!state.ledgerAvailable) {
    countEl.textContent = '等待账本';
    countEl.className = 'text-xs tabular-nums px-2 py-0.5 rounded-full bg-[#eaf0ee] text-[#3f5760]';
    showMessage(container, 'text-xs text-[#526a73] py-6 text-center', '等待账本数据');
    return;
  }

  if (issues.length === 0) {
    countEl.textContent = '正常 · 0 项';
    countEl.className = 'text-xs tabular-nums font-semibold px-2 py-0.5 rounded-full bg-[#e6f5ef] text-[#0a7052] border border-[#a5dcc8]';

    const okRow = createNode('div', 'py-5 flex items-center gap-2 text-xs text-[#0a7052]');
    okRow.innerHTML = `
      <svg class="w-4 h-4 shrink-0" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
        <path d="m9 11 3 3L22 4"/>
      </svg>
      <span>巡检执行正常，无待处理异常</span>
    `;
    container.replaceChildren(okRow);
    return;
  }

  const criticalCount = issues.filter(i => CRITICAL_ISSUE_CATEGORIES.has(i.category)).length;
  countEl.textContent = criticalCount > 0 ? `${issues.length} 项 (${criticalCount} 高优)` : `${issues.length} 项提示`;
  countEl.className = criticalCount > 0
    ? 'text-xs tabular-nums font-semibold px-2 py-0.5 rounded-full bg-[#fcecec] text-[#bd2b2b] border border-[#f0b5b5]'
    : 'text-xs tabular-nums font-semibold px-2 py-0.5 rounded-full bg-[#fdf4e3] text-[#985506] border border-[#ebcc94]';

  container.replaceChildren(...issues.map(issue => {
    const isCritical = CRITICAL_ISSUE_CATEGORIES.has(issue.category);
    const item = createNode('article', 'py-3 first:pt-2.5 last:pb-1');
    const header = createNode('div', 'flex items-center justify-between gap-2');

    const titleWrap = createNode('div', 'flex items-center gap-1.5 min-w-0');
    const iconWrap = createNode('span', isCritical ? 'text-[#bd2b2b] shrink-0' : 'text-[#985506] shrink-0');
    iconWrap.setAttribute('aria-hidden', 'true');
    iconWrap.innerHTML = isCritical
      ? '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>'
      : '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>';
    titleWrap.append(
      iconWrap,
      createNode('span', 'font-display text-xs font-bold text-[#102128] truncate', issue.symbol),
    );

    const badgeClass = isCritical
      ? 'text-xs font-semibold px-1.5 py-0.5 rounded bg-[#fcecec] text-[#bd2b2b] border border-[#f0b5b5]'
      : 'text-xs font-medium px-1.5 py-0.5 rounded bg-[#eaf0ee] text-[#3f5760] border border-[#d4dfdc]';
    header.append(titleWrap, createNode('span', badgeClass, labels[issue.category] || issue.category));

    const detail = createNode('p', 'text-xs text-[#3f5760] mt-1.5 break-words leading-relaxed', issue.detail);
    const time = createNode('time', 'block text-xs text-[#526a73] font-mono tabular-nums mt-1', new Date(issue.occurred_at_ms).toLocaleString('zh-CN'));
    time.dateTime = new Date(issue.occurred_at_ms).toISOString();
    item.append(header, detail, time);
    return item;
  }));
}

function compareValues(valA, valB, type, direction) {
  const missingA = valA === null || valA === undefined || valA === '';
  const missingB = valB === null || valB === undefined || valB === '';
  if (missingA && missingB) return 0;
  if (missingA) return 1;
  if (missingB) return -1;

  const factor = direction === 'asc' ? 1 : -1;
  if (type === 'string') {
    return factor * collator.compare(String(valA), String(valB));
  }
  return factor * (Number(valA) - Number(valB));
}

function reorderRowsInPlace(tbody, sortKey, sortType, direction) {
  const rows = Array.from(tbody.rows).filter(row => row.dataset.symbol);
  if (rows.length <= 1) return;

  rows.sort((rowA, rowB) => {
    const rawA = rowA.dataset[sortKey];
    const rawB = rowB.dataset[sortKey];
    return compareValues(
      rawA === '' ? null : rawA,
      rawB === '' ? null : rawB,
      sortType,
      direction,
    );
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

  const sortKeys = ['symbol', 'tier', 'scan_price', 'entry_notional', 'estimated_unrealized_pnl'];
  let activeLabel = key;
  let activeType = 'number';

  sortKeys.forEach(k => {
    const th = document.getElementById('th-' + k);
    if (k === state.currentSortKey) {
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
    const status = document.getElementById('positions-sort-status');
    const dirLabel = state.currentSortDirection === 'asc' ? '升序' : '降序';
    status.textContent = `已按${activeLabel}${dirLabel}排列，共 ${existingDataRows.length} 行`;
  } else {
    renderPositions();
  }
}

export function filterTier(tier) {
  state.currentTierFilter = tier;
  ['all', 't1', 't2', 't3'].forEach(k => {
    const btn = document.getElementById('filter-' + k);
    const isActive = k.toUpperCase() === tier || (k === 'all' && tier === 'ALL');
    btn.setAttribute('aria-pressed', String(isActive));
  });
  renderPositions();
}

export function renderPositions() {
  const tbody = document.getElementById('position-table-body');
  const summaryEl = document.getElementById('position-summary-text');

  if (!state.ledgerAvailable) {
    showTableMessage(tbody, 9, 'py-12 text-center text-[#526a73] font-sans', '等待账本数据');
    summaryEl.textContent = '在管持仓: —';
    return;
  }
  if (state.allPositions.length === 0) {
    showTableMessage(tbody, 9, 'py-12 text-center text-[#526a73] font-sans', '暂无在管持仓记录');
    summaryEl.textContent = '在管持仓: 0 个标的';
    return;
  }

  const query = document.getElementById('symbol-search').value.trim().toUpperCase();
  const filtered = state.allPositions.filter(p => {
    const matchesQuery = p.symbol.includes(query);
    let matchesTier = true;
    if (state.currentTierFilter === 'T1') matchesTier = (p.tier === 1);
    else if (state.currentTierFilter === 'T2') matchesTier = (p.tier === 2);
    else if (state.currentTierFilter === 'T3') matchesTier = (p.tier === 3);
    return matchesQuery && matchesTier;
  });

  const activeTh = document.getElementById('th-' + state.currentSortKey);
  const sortType = activeTh ? activeTh.dataset.type || 'number' : 'number';
  filtered.sort((a, b) => compareValues(
    a[state.currentSortKey],
    b[state.currentSortKey],
    sortType,
    state.currentSortDirection,
  ));

  if (filtered.length === 0) {
    showTableMessage(tbody, 9, 'py-10 text-center text-[#526a73] font-sans', '无符合当前筛选条件的持仓标的');
    summaryEl.textContent = `显示 0 / ${state.allPositions.length} 个在管标的`;
    return;
  }

  const rows = filtered.map(p => {
    const row = document.createElement('tr');
    row.dataset.symbol = p.symbol;
    row.dataset.tier = String(p.tier);
    row.dataset.scan_price = p.scan_price === null ? '' : String(p.scan_price);
    row.dataset.entry_notional = String(p.entry_notional);
    row.dataset.estimated_unrealized_pnl = p.estimated_unrealized_pnl === null ? '' : String(p.estimated_unrealized_pnl);

    const symbolHeader = createNode('th', 'font-display', p.symbol);
    symbolHeader.scope = 'row';

    const tierCell = createNode('td', '');
    tierCell.append(createNode('span', `tier-badge font-sans ${tierBadgeClass(p)}`, p.tier_label));

    const tpStr = p.tp_price === null ? '—' : `$${formatPrice(p.tp_price)}`;
    const scanPriceStr = p.scan_price !== null ? `$${formatPrice(p.scan_price)}` : '—';

    const riskCell = createNode('td', 'text-right');
    if (p.writeoff_price) {
      riskCell.append(createNode('span', 'text-[#5c3d91] font-semibold', `$${formatPrice(p.writeoff_price)} 出清`));
    } else if (p.dca_price) {
      riskCell.append(createNode('span', 'text-[#985506]', `$${formatPrice(p.dca_price)} -50%`));
    } else {
      riskCell.textContent = '—';
      riskCell.className = 'text-right text-[#526a73]';
    }

    const unpnlCell = createNode('td', 'text-right pr-4');
    if (p.estimated_unrealized_pnl !== null) {
      const pnlVal = Number(p.estimated_unrealized_pnl);
      const pnlPct = ((p.scan_price - p.avg_entry_price) / p.avg_entry_price) * 100;
      const pnlClass = pnlVal > 0 ? 'text-[#0a7052]' : pnlVal < 0 ? 'text-[#bd2b2b]' : 'text-[#3f5760]';
      const sign = pnlPct > 0 ? '+' : '';
      unpnlCell.append(
        createNode('span', `font-bold ${pnlClass}`, formatSignedUsd(pnlVal)),
        createNode('span', `text-xs ml-1.5 font-normal ${pnlClass}`, `${sign}${pnlPct.toFixed(2)}%`),
      );
    } else {
      unpnlCell.textContent = '—';
      unpnlCell.className = 'text-right pr-4 text-[#526a73]';
    }

    row.append(
      symbolHeader,
      tierCell,
      createNode('td', 'text-right text-[#3f5760]', `$${formatPrice(p.avg_entry_price)}`),
      createNode('td', 'text-right text-[#102128] font-medium', scanPriceStr),
      createNode('td', 'text-right text-[#0a7052] font-semibold', tpStr),
      riskCell,
      createNode('td', 'text-right text-[#3f5760]', String(p.qty)),
      createNode('td', 'text-right text-[#102128] font-semibold', formatUsd(p.entry_notional)),
      unpnlCell,
    );
    return row;
  });

  tbody.replaceChildren(...rows);
  summaryEl.textContent = `显示 ${filtered.length} / ${state.allPositions.length} 个在管标的`;
}

export function renderFills(buyFills, sellFills) {
  const query = document.getElementById('symbol-search').value.trim().toUpperCase();
  const filteredBuys = query ? buyFills.filter(f => f.symbol.includes(query)) : buyFills;
  const filteredSells = query ? sellFills.filter(f => f.symbol.includes(query)) : sellFills;

  renderFillGroup('buy-fills-list', 'buy-fills-count', filteredBuys, 'BUY');
  renderFillGroup('sell-fills-list', 'sell-fills-count', filteredSells, 'SELL');
}

function renderFillGroup(listId, countId, fills, side) {
  const list = document.getElementById(listId);
  const countEl = document.getElementById(countId);

  if (!state.ledgerAvailable) {
    countEl.textContent = '—';
    showMessage(list, 'text-[#526a73] text-xs py-6 text-center', '等待账本数据');
    return;
  }
  countEl.textContent = `最近 ${fills.length} 笔`;
  if (fills.length === 0) {
    const emptyText = side === 'BUY' ? '暂无买入成交明细' : '暂无卖出成交明细';
    showMessage(list, 'text-[#526a73] text-xs py-6 text-center', emptyText);
    return;
  }

  const items = fills.map(fill => {
    const tradedAt = new Date(fill.traded_at_ms);
    const time = tradedAt.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const isBuy = side === 'BUY';
    const amount = isBuy
      ? formatUsd(Number(fill.price) * Number(fill.qty))
      : formatSignedUsd(fill.realized_pnl);
    const amountLabel = isBuy ? '成交额' : '已实现毛盈亏';
    const pnl = Number(fill.realized_pnl);
    const amountClass = isBuy
      ? 'text-[#102128]'
      : !Number.isFinite(pnl)
        ? 'text-[#526a73]'
        : pnl >= 0 ? 'text-[#0a7052]' : 'text-[#bd2b2b]';

    const item = createNode('article', 'py-3 flex items-center justify-between gap-4 text-xs');
    const identity = createNode('div', 'min-w-0');
    identity.append(
      createNode('p', 'font-display font-semibold text-[#102128] truncate', fill.symbol),
      createNode('p', 'text-[#526a73] text-xs mt-0.5 font-mono tabular-nums', `均价 $${formatPrice(fill.price)} · 数量 ${formatPrice(fill.qty)}`),
    );

    const details = createNode('div', 'shrink-0 text-right');
    const timeNode = createNode('time', 'block mt-0.5 text-xs text-[#526a73] font-mono tabular-nums', time);
    timeNode.dateTime = tradedAt.toISOString();
    details.append(
      createNode('span', 'block text-xs text-[#526a73]', amountLabel),
      createNode('span', `block font-display font-semibold tabular-nums ${amountClass}`, amount),
      timeNode,
    );
    item.append(identity, details);
    return item;
  });
  list.replaceChildren(...items);
}

export function renderRanking(symbols, pendingCloseFills) {
  const container = document.getElementById('ranking-container');

  if (!state.ledgerAvailable || pendingCloseFills > 0) {
    const message = !state.ledgerAvailable ? '等待账本数据' : '平仓流水待对账，收益排名暂不可用';
    showMessage(container, 'text-[#526a73] text-xs py-8 text-center', message);
    return;
  }
  if (symbols.length === 0) {
    showMessage(container, 'text-[#526a73] text-xs py-8 text-center', '暂无收益排名记录');
    return;
  }

  const maxAbs = Math.max(...symbols.map(s => Math.abs(Number(s.realized_gross) || 0)), 0.01);

  const rows = symbols.map((symbol, idx) => {
    const val = Number(symbol.realized_gross);
    const widthPct = Math.min(100, Math.max(4, (Math.abs(val) / maxAbs) * 100));
    const isPos = val >= 0;

    const row = createNode('div', 'grid grid-cols-[28px_112px_1fr_116px] items-center gap-3 py-2.5 text-xs');
    const rankEl = createNode(
      'span',
      `font-display tabular-nums font-bold text-center ${idx < 3 ? 'text-[#135d73]' : 'text-[#526a73]'}`,
      `#${idx + 1}`,
    );
    const symEl = createNode('span', 'font-display font-semibold text-[#102128] truncate', symbol.symbol);

    const track = createNode('div', 'h-2 rounded-sm bg-[#eaf0ee] overflow-hidden');
    const fill = createNode('div', 'h-full rounded-sm');
    fill.style.width = `${widthPct}%`;
    fill.style.backgroundColor = isPos ? '#0a7052' : '#bd2b2b';
    track.append(fill);

    const valEl = createNode(
      'span',
      `font-display tabular-nums font-semibold text-right ${isPos ? 'text-[#0a7052]' : 'text-[#bd2b2b]'}`,
      `${formatSignedUsd(val)} USDT`,
    );

    row.append(rankEl, symEl, track, valEl);
    return row;
  });
  container.replaceChildren(...rows);
}
