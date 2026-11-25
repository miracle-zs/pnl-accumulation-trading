// Risk, Exposure, Costs, and Execution Analytics Renderers

import { state } from './state.js';
import {
  formatUsd,
  formatSignedUsd,
  createNode,
  showTableMessage,
  showMessage,
} from './formatters.js';

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
  const periodEl = document.getElementById('drawdown-period');
  const maxDdEl = document.getElementById('max-drawdown');
  const maxDdPctEl = document.getElementById('max-drawdown-pct');
  const curDdPctEl = document.getElementById('current-drawdown-pct');

  periodEl.textContent = periodNames[equity.period];
  const hasSamples = equity.points.length > 0;
  maxDdEl.textContent = hasSamples && equity.max_drawdown !== null
    ? formatUsd(equity.max_drawdown) : '—';
  maxDdPctEl.textContent = hasSamples && equity.max_drawdown_pct !== null
    ? `${equity.max_drawdown_pct.toFixed(2)}%` : '采样不足';
  curDdPctEl.textContent = hasSamples && equity.current_drawdown_pct !== null
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
  const otherFees = costs.other_fee_count;
  const pendingCloseFills = costs.pending_close_fill_count;
  const warnings = [];
  if (otherFees > 0) warnings.push(`存在 ${otherFees} 笔非 USDT 手续费抵扣（如 BNB 折扣），未折算入规费总额。`);
  if (pendingCloseFills > 0) warnings.push(`存在 ${pendingCloseFills} 笔平仓成交待对账，规费统计可能不完整。`);
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
    const symbol = document.createElement('span');
    symbol.className = 'text-xs font-semibold text-white';
    symbol.textContent = issue.symbol;
    const category = document.createElement('span');
    category.className = 'text-[10px] text-amber-400';
    category.textContent = labels[issue.category] || issue.category;
    header.append(symbol, category);
    const detail = document.createElement('p');
    detail.className = 'text-[11px] text-slate-400 mt-1 break-words';
    detail.textContent = issue.detail;
    const time = document.createElement('time');
    time.className = 'block text-[10px] text-slate-600 font-mono mt-1';
    time.dateTime = new Date(issue.occurred_at_ms).toISOString();
    time.textContent = new Date(issue.occurred_at_ms).toLocaleString('zh-CN');
    item.append(header, detail, time);
    return item;
  }));
}
