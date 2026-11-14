// Fills Feed and Realized Gross Profit Rankings Renderers

import { state } from './state.js';
import {
  formatPrice,
  formatUsd,
  formatSignedUsd,
  createNode,
  showMessage,
} from './formatters.js';

export function renderFills(buyFills, sellFills) {
  const searchInput = document.getElementById('symbol-search');
  const query = searchInput ? searchInput.value.trim().toUpperCase() : '';
  const filteredBuys = query ? buyFills.filter(f => f.symbol.includes(query)) : buyFills;
  const filteredSells = query ? sellFills.filter(f => f.symbol.includes(query)) : sellFills;

  renderFillGroup(
    'buy-fills-list',
    'buy-fills-count',
    filteredBuys,
    'BUY',
  );
  renderFillGroup(
    'sell-fills-list',
    'sell-fills-count',
    filteredSells,
    'SELL',
  );
}

export function renderFillGroup(listId, countId, fills, side) {
  const list = document.getElementById(listId);
  const countEl = document.getElementById(countId);
  if (!list) return;

  if (!state.ledgerAvailable) {
    if (countEl) countEl.textContent = '—';
    showMessage(list, 'text-slate-400 text-xs py-6 text-center', '等待账本数据');
    return;
  }
  if (countEl) countEl.textContent = `最近 ${fills.length} 笔`;
  if (!fills || fills.length === 0) {
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
  if (!container) return;

  if (!state.ledgerAvailable || pendingCloseFills > 0) {
    const message = !state.ledgerAvailable ? '等待账本数据' : '平仓流水待对账，收益排名暂不可用';
    showMessage(container, 'text-slate-400 text-xs py-8 text-center', message);
    return;
  }
  if (!symbols || symbols.length === 0) {
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
