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

export function updatePositionStatusKpi(positions) {
  const statusEl = document.getElementById('kpi-position-status');
  if (!state.ledgerAvailable || positions.length === 0) {
    statusEl.textContent = '暂无持仓';
    return;
  }
  const priced = positions.filter(p => Number.isFinite(p.estimated_unrealized_pnl));
  if (priced.length === 0) {
    statusEl.textContent = '价格采样中...';
    return;
  }
  const profitCount = priced.filter(p => p.estimated_unrealized_pnl > 0).length;
  const lossCount = priced.filter(p => p.estimated_unrealized_pnl < 0).length;
  statusEl.textContent = `${profitCount} 浮盈 · ${lossCount} 浮亏`;
  statusEl.className = `font-mono ${lossCount > 0 ? 'text-amber-300' : 'text-emerald-400'}`;
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
    showTableMessage(body, 4, 'py-6 text-center text-slate-500', '等待账本数据');
    return;
  }
  if (positions.length === 0) {
    countEl.textContent = '0 个持仓';
    showTableMessage(body, 4, 'py-6 text-center text-slate-500', '暂无在管持仓');
    return;
  }
  const pricedPositions = positions
    .filter(position => Number.isFinite(position.estimated_unrealized_pnl));
  const losses = pricedPositions
    .filter(position => position.estimated_unrealized_pnl < 0)
    .sort((a, b) => a.estimated_unrealized_pnl - b.estimated_unrealized_pnl);

  countEl.textContent = pricedPositions.length > 0
    ? `${losses.length} 个浮亏 · ${pricedPositions.length}/${positions.length} 已估值`
    : '等待价格采样';
  if (losses.length === 0) {
    const message = pricedPositions.length > 0
      ? '当前无浮亏持仓标的'
      : '持仓价格将在下一轮巡检后显示';
    showTableMessage(body, 4, 'py-6 text-center text-slate-500', message);
    return;
  }
  const rows = losses.slice(0, 6).map(position => {
    const sampledAt = position.price_captured_at_ms
      ? new Date(position.price_captured_at_ms).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : '—';
    const row = document.createElement('tr');
    row.append(
      createNode('td', 'py-2.5 text-white font-medium', position.symbol),
      createNode('td', 'py-2.5 text-slate-400', position.tier_label),
      createNode('td', 'py-2.5 text-right font-mono text-rose-400 font-semibold', formatSignedUsd(position.estimated_unrealized_pnl)),
      createNode('td', 'py-2.5 text-right font-mono text-slate-500', sampledAt),
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
    showMessage(container, 'text-xs text-slate-500', '暂无在管持仓');
    return;
  }
  countEl.textContent = `${exposure.priced_positions}/${exposure.total_positions} 已估值`;
  const tiers = [
    { key: 't1', label: 'T1 基础底仓' },
    { key: 't2', label: 'T2 加仓阶段' },
    { key: 't3', label: 'T3 深度补仓' },
  ];
  const rows = tiers.map(tier => {
    const amount = exposure[tier.key];
    const ratio = exposure.total > 0 ? amount / exposure.total * 100 : 0;
    const row = document.createElement('div');
    const labels = createNode('div', 'flex items-center justify-between text-xs mb-1.5');
    const amountLabel = createNode('span', 'font-mono text-slate-200');
    amountLabel.append(
      document.createTextNode(`${formatUsd(amount)} `),
      createNode('span', 'text-slate-500', `${ratio.toFixed(1)}%`),
    );
    labels.append(createNode('span', 'text-slate-300', tier.label), amountLabel);
    const track = createNode('div', 'h-1.5 rounded-full bg-slate-950 overflow-hidden');
    const barClass = tier.key === 't1' ? 'bg-blue-500' : tier.key === 't2' ? 'bg-amber-500' : 'bg-rose-500';
    const bar = createNode('div', `h-full rounded-full ${barClass}`);
    bar.style.width = `${ratio}%`;
    track.append(bar);
    row.append(labels, track);
    return row;
  });
  const total = createNode('div', 'flex justify-between border-t border-slate-800 pt-3 text-xs');
  total.append(
    createNode('span', 'text-slate-400', '已估值名义总敞口'),
    createNode('span', 'font-mono font-semibold text-white', formatUsd(exposure.total)),
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
    showMessage(rows, 'text-xs text-slate-500 py-2', '等待账本数据');
    return;
  }
  weeklyCommEl.textContent = formatUsd(costs.commission_usdt);
  weeklyCountEl.textContent = `${costs.fill_count} 笔`;
  const warnings = [];
  if (costs.other_fee_count > 0) warnings.push(`存在 ${costs.other_fee_count} 笔非 USDT 手续费抵扣（如 BNB 折扣），未折算入规费总额。`);
  if (costs.pending_close_fill_count > 0) warnings.push(`存在 ${costs.pending_close_fill_count} 笔平仓成交待对账，规费统计可能不完整。`);
  otherFeesNote.textContent = warnings.join(' ');
  otherFeesNote.classList.toggle('hidden', warnings.length === 0);
  const days = costs.days;
  if (days.length === 0) {
    showMessage(rows, 'text-xs text-slate-500 py-2', '最近 7 天暂无成交记录');
    return;
  }
  const dayRows = days.slice(-7).reverse().map(day => {
    const row = createNode('div', 'flex items-center justify-between text-xs');
    const date = createNode('time', 'text-slate-400 font-mono', day.day.slice(5));
    date.dateTime = day.day;
    row.append(
      date,
      createNode('span', 'text-slate-500', `${day.fill_count} 笔`),
      createNode('span', 'text-slate-200 font-mono', formatUsd(day.commission_usdt)),
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
    ORDER_REJECTED: '币安拒绝委托 (Rejected)',
    ORDER_CANCELED: '委托已撤销',
    ORDER_EXPIRED: '委托已过期 (Expired)',
  };

  if (!state.ledgerAvailable) {
    countEl.textContent = '等待账本';
    showMessage(container, 'text-xs text-slate-500 py-4 text-center', '等待账本数据');
    return;
  }
  countEl.textContent = `${issues.length} 项`;
  if (issues.length === 0) {
    showMessage(container, 'text-xs text-emerald-400/90 py-4 text-center', '系统运行正常，无待处理异常');
    return;
  }
  container.replaceChildren(...issues.map(issue => {
    const item = document.createElement('article');
    item.className = 'py-2.5 border-b border-slate-800/70';
    const header = document.createElement('div');
    header.className = 'flex items-center justify-between gap-2';
    header.append(
      createNode('span', 'text-xs font-semibold text-white', issue.symbol),
      createNode('span', 'text-[10px] text-amber-400', labels[issue.category] || issue.category),
    );
    const detail = createNode('p', 'text-[11px] text-slate-400 mt-1 break-words', issue.detail);
    const time = createNode('time', 'block text-[10px] text-slate-600 font-mono mt-1', new Date(issue.occurred_at_ms).toLocaleString('zh-CN'));
    time.dateTime = new Date(issue.occurred_at_ms).toISOString();
    item.append(header, detail, time);
    return item;
  }));
}

export function handleSort(key) {
  if (state.currentSortKey === key) {
    state.currentSortDirection = state.currentSortDirection === 'asc' ? 'desc' : 'asc';
  } else {
    state.currentSortKey = key;
    state.currentSortDirection = 'desc';
  }

  const sortKeys = ['symbol', 'tier', 'scan_price', 'entry_notional', 'estimated_unrealized_pnl'];
  sortKeys.forEach(k => {
    const th = document.getElementById('th-' + k);
    const indicator = document.getElementById('sort-' + k);
    if (k === state.currentSortKey) {
      th.setAttribute('aria-sort', state.currentSortDirection === 'asc' ? 'ascending' : 'descending');
      indicator.textContent = state.currentSortDirection === 'asc' ? '▲' : '▼';
    } else {
      th.removeAttribute('aria-sort');
      indicator.textContent = '↕';
    }
  });

  renderPositions();
}

export function filterTier(tier) {
  state.currentTierFilter = tier;
  ['all', 't1', 't2', 't3'].forEach(k => {
    const btn = document.getElementById('filter-' + k);
    if (k.toUpperCase() === tier || (k === 'all' && tier === 'ALL')) {
      btn.className = 'px-2.5 py-1 rounded-md bg-blue-600 text-white font-medium focus-visible:outline-2 focus-visible:outline-blue-500';
    } else {
      btn.className = 'px-2.5 py-1 rounded-md text-slate-400 hover:text-white focus-visible:outline-2 focus-visible:outline-blue-500';
    }
  });
  renderPositions();
}

export function renderPositions() {
  const tbody = document.getElementById('position-table-body');
  const summaryEl = document.getElementById('position-summary-text');

  if (!state.ledgerAvailable) {
    showTableMessage(tbody, 9, 'py-12 text-center text-slate-400 font-sans', '等待账本数据');
    summaryEl.textContent = '在管持仓: —';
    return;
  }
  if (state.allPositions.length === 0) {
    showTableMessage(tbody, 9, 'py-12 text-center text-slate-500 font-sans', '暂无在管持仓记录');
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

  filtered.sort((a, b) => {
    const valA = a[state.currentSortKey];
    const valB = b[state.currentSortKey];
    const missingA = valA === null || valA === undefined;
    const missingB = valB === null || valB === undefined;
    if (missingA && missingB) return 0;
    if (missingA) return 1;
    if (missingB) return -1;

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

    const unpnlCell = createNode('td', 'py-3 px-4 text-right');
    if (p.estimated_unrealized_pnl !== null) {
      const pnlVal = Number(p.estimated_unrealized_pnl);
      const pnlPct = (p.scan_price - p.avg_entry_price) / p.avg_entry_price * 100;
      const pnlClass = pnlVal > 0 ? 'text-emerald-400' : pnlVal < 0 ? 'text-rose-400' : 'text-slate-300';
      const sign = pnlPct > 0 ? '+' : '';
      unpnlCell.append(
        createNode('span', `font-bold ${pnlClass}`, formatSignedUsd(pnlVal)),
        createNode('span', `text-[10px] ml-1 opacity-80 font-normal ${pnlClass}`, `(${sign}${pnlPct.toFixed(2)}%)`),
      );
    } else {
      unpnlCell.textContent = '—';
      unpnlCell.className = 'py-3 px-4 text-right text-slate-500';
    }

    const row = createNode('tr', 'hover:bg-slate-800/50 transition-colors');
    const tierCell = createNode('td', 'py-3 px-3');
    tierCell.append(createNode('span', `${badgeClass} px-2 py-0.5 rounded text-[11px] font-sans`, p.tier_label));
    const riskCell = createNode('td', 'py-3 px-3 text-slate-400');
    if (p.writeoff_price) {
      riskCell.append(createNode('span', 'text-purple-400 font-semibold', `$${formatPrice(p.writeoff_price)} (出清)`));
    } else if (p.dca_price) {
      riskCell.append(createNode('span', 'text-amber-400/90 font-mono', `$${formatPrice(p.dca_price)} (-50%)`));
    } else {
      riskCell.textContent = '—';
    }
    row.append(
      createNode('td', 'py-3 px-4 font-bold text-white font-sans', p.symbol),
      tierCell,
      createNode('td', 'py-3 px-3 text-slate-300', `$${formatPrice(p.avg_entry_price)}`),
      createNode('td', 'py-3 px-3 text-slate-200 font-medium', scanPriceStr),
      createNode('td', 'py-3 px-3 text-emerald-400 font-bold', tpStr),
      riskCell,
      createNode('td', 'py-3 px-3 text-slate-300', p.qty),
      createNode('td', 'py-3 px-4 text-right text-slate-100 font-bold', `$${p.entry_notional.toFixed(2)}`),
      unpnlCell,
    );
    return row;
  });

  if (rows.length === 0) {
    showTableMessage(tbody, 9, 'py-8 text-center text-slate-500 font-sans', '无符合当前层级或搜索条件的持仓标的');
    summaryEl.textContent = `显示 0 / ${state.allPositions.length} 个在管标的`;
    return;
  }
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
    showMessage(list, 'text-slate-400 text-xs py-6 text-center', '等待账本数据');
    return;
  }
  countEl.textContent = `最近 ${fills.length} 笔`;
  if (fills.length === 0) {
    const emptyText = side === 'BUY' ? '暂无买入成交明细' : '暂无卖出成交明细';
    showMessage(list, 'text-slate-400 text-xs py-6 text-center', emptyText);
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
      ? 'text-slate-200'
      : !Number.isFinite(pnl)
        ? 'text-slate-400'
        : pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';

    const item = createNode('article', 'py-3 flex items-center justify-between gap-4 text-xs');
    const identity = createNode('div', 'min-w-0');
    identity.append(
      createNode('p', 'font-semibold text-white truncate', fill.symbol),
      createNode('p', 'text-slate-400 text-[11px] mt-1 font-mono tabular-nums', `单价: $${formatPrice(fill.price)} │ 数量: ${formatPrice(fill.qty)}`),
    );
    const details = createNode('div', 'shrink-0 text-right');
    const timeNode = createNode('time', 'block mt-1 text-[10px] text-slate-500 font-mono tabular-nums', time);
    timeNode.dateTime = tradedAt.toISOString();
    details.append(
      createNode('span', 'block text-[10px] text-slate-400', amountLabel),
      createNode('span', `block mt-0.5 font-mono font-semibold tabular-nums ${amountClass}`, amount),
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
    showMessage(container, 'text-slate-400 text-xs py-8 text-center', message);
    return;
  }
  if (symbols.length === 0) {
    showMessage(container, 'text-slate-500 text-xs py-8 text-center', '暂无收益排名记录');
    return;
  }
  const rows = symbols.map((symbol, idx) => {
    const row = createNode('div', 'flex items-center justify-between p-2.5 rounded-lg bg-slate-950/80 border border-slate-800 text-xs font-mono');
    const identity = createNode('div', 'flex items-center gap-3');
    identity.append(
      createNode('span', `w-5 text-center font-bold ${idx < 3 ? 'text-amber-400' : 'text-slate-500'}`, `#${idx + 1}`),
      createNode('span', 'font-sans font-bold text-white', symbol.symbol),
    );
    const pnlClass = symbol.realized_gross >= 0 ? 'text-emerald-400' : 'text-rose-400';
    row.append(identity, createNode('span', `${pnlClass} font-bold`, `${formatSignedUsd(symbol.realized_gross)} USDT`));
    return row;
  });
  container.replaceChildren(...rows);
}
