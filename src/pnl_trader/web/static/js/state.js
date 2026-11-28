// Centralized UI State, Shared Formatters, Render Cache, and Core DOM Helpers

export const PERIOD_LABELS = { '24h': '24 小时', '7d': '7 天', '30d': '30 天' };
export const DRAWDOWN_PERIOD_LABELS = { '24h': '最近 24 小时', '7d': '最近 7 天', '30d': '最近 30 天' };

export const state = {
  allPositions: [],
  currentTierFilter: 'ALL',
  currentPnlFilter: 'ALL',
  currentPeriod: '7d',
  displayedPeriod: '7d',
  lastSyncedAtMs: null,
  lastSnapshotCapturedAtMs: null,
  lastSnapshotIsCurrentSlot: true,
  chartHistoryPoints: [],
  ledgerAvailable: false,
  tradingUptimeBaseSeconds: null,
  tradingUptimeUpdatedAt: 0,
  currentView: 'positions',
  rawBuyFills: [],
  rawSellFills: [],
  fillsLimit: 12,
  currentSortKey: 'tier',
  currentSortDirection: 'desc',
  searchQuery: '',
  highlightedSymbol: null,
};

const BEIJING_TIMEZONE = 'Asia/Shanghai';

const beijingDateTimeFmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIMEZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const beijingDateHmFmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIMEZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const beijingShortDateTimeFmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIMEZONE,
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const beijingTimeFmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIMEZONE,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const beijingHmFmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIMEZONE,
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

export function formatBeijingDateTime(epochMs) {
  return beijingDateTimeFmt.format(new Date(epochMs));
}

export function formatBeijingDateHm(epochMs) {
  return beijingDateHmFmt.format(new Date(epochMs));
}

export function formatBeijingShortDateTime(epochMs) {
  return beijingShortDateTimeFmt.format(new Date(epochMs));
}

export function formatBeijingTime(epochMs) {
  return beijingTimeFmt.format(new Date(epochMs));
}

export function formatBeijingHm(epochMs) {
  return beijingHmFmt.format(new Date(epochMs));
}

export const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const renderCache = new WeakMap();

export function isRenderUnchanged(container, key) {
  const cached = renderCache.get(container);
  return cached !== undefined
    && cached.key === key
    && container.firstElementChild === cached.firstChild
    && container.childElementCount === cached.childCount;
}

export function saveRenderSnapshot(container, key) {
  renderCache.set(container, {
    key,
    firstChild: container.firstElementChild,
    childCount: container.childElementCount,
  });
}

export function normalizeRoundedZero(amount, decimals = 2) {
  const factor = 10 ** decimals;
  const rounded = Math.round(amount * factor) / factor;
  return rounded === 0 ? 0 : rounded;
}

export function formatSignedUsd(value) {
  if (value === null || value === undefined || value === '') return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  const clean = normalizeRoundedZero(amount, 2);
  const sign = clean > 0 ? '+' : clean < 0 ? '-' : '';
  return `${sign}$${Math.abs(clean).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatUsd(value) {
  if (value === null || value === undefined || value === '') return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  const clean = normalizeRoundedZero(amount, 2);
  const sign = clean < 0 ? '-' : '';
  return `${sign}$${Math.abs(clean).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatSignedPercent(value, decimals = 2) {
  if (value === null || value === undefined || value === '') return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  const clean = normalizeRoundedZero(amount, decimals);
  const sign = clean > 0 ? '+' : clean < 0 ? '-' : '';
  return `${sign}${Math.abs(clean).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}%`;
}

export function formatPrice(value) {
  if (value === null || value === undefined || value === '') return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  if (amount === 0) return '0';
  const magnitude = Math.abs(amount);
  if (magnitude < 1e-20) return amount.toExponential(6);
  const fractionDigits = Math.min(20, Math.max(4, 5 - Math.floor(Math.log10(magnitude))));
  return amount.toLocaleString('en-US', { maximumFractionDigits: fractionDigits });
}

export function formatUsdPrice(value) {
  const formatted = formatPrice(value);
  return formatted === '—' ? '—' : `$${formatted}`;
}

export function formatQty(value) {
  return formatPrice(value);
}

export function formatCommission(value, asset) {
  if (value === null || value === undefined || value === '') return null;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return null;
  if (asset === 'USDT') {
    const clean = normalizeRoundedZero(amount, 4);
    const sign = clean < 0 ? '-' : '';
    return `${sign}$${Math.abs(clean).toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    })}`;
  }
  const formattedQty = formatPrice(amount);
  return asset ? `${formattedQty} ${asset}` : formattedQty;
}

export function pnlColorClass(value, { zeroClass = 'text-[#3f5760]', nullClass = 'text-[#526a73]' } = {}) {
  if (value === null || value === undefined || value === '') return nullClass;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return nullClass;
  const clean = normalizeRoundedZero(amount, 2);
  if (clean > 0) return 'text-[#0a7052]';
  if (clean < 0) return 'text-[#bd2b2b]';
  return zeroClass;
}

export function createNode(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

export function createTimeNode(epochMs, className, formatter) {
  const node = createNode('time', className, formatter(epochMs));
  node.dateTime = new Date(epochMs).toISOString();
  return node;
}

export function showTableMessage(container, columns, className, message, { withResetBtn = false } = {}) {
  const cell = createNode('td', className);
  cell.colSpan = columns;
  if (withResetBtn) {
    const wrap = createNode('div', 'inline-flex flex-wrap items-center justify-center gap-2');
    wrap.append(createNode('span', '', message));
    const resetBtn = createNode(
      'button',
      'min-h-6 px-2.5 py-1 rounded border border-[#d4dfdc] bg-white text-xs font-semibold text-[#135d73] hover:bg-[#f2f7f5] transition-colors',
      '重置筛选',
    );
    resetBtn.type = 'button';
    resetBtn.dataset.resetFilters = 'true';
    wrap.append(resetBtn);
    cell.append(wrap);
  } else {
    cell.textContent = message;
  }
  const row = document.createElement('tr');
  row.append(cell);
  container.replaceChildren(row);
}

export function showMessage(container, className, message, { withResetBtn = false } = {}) {
  if (!withResetBtn) {
    container.replaceChildren(createNode('div', className, message));
    return;
  }
  const wrap = createNode('div', className);
  wrap.append(createNode('span', 'mr-2', message));
  const resetBtn = createNode(
    'button',
    'min-h-6 px-2.5 py-1 rounded border border-[#d4dfdc] bg-white text-xs font-semibold text-[#135d73] hover:bg-[#f2f7f5] transition-colors',
    '重置筛选',
  );
  resetBtn.type = 'button';
  resetBtn.dataset.resetFilters = 'true';
  wrap.append(resetBtn);
  container.replaceChildren(wrap);
}

export function isClosingPosition(position) {
  return position?.lifecycle === 'CLOSING'
    || (typeof position?.tier_label === 'string' && position.tier_label.includes('待全平'));
}

export function isReducingPosition(position) {
  return !isClosingPosition(position)
    && (position?.tier === 2 || position?.tier === 3)
    && Number(position?.completed_batches) > 0;
}

export function tierBadgeClass(position) {
  if (isClosingPosition(position)) return 'badge-closing';
  if (position.tier === 2) return position.completed_batches > 0 ? 'badge-t21' : 'badge-t2';
  if (position.tier === 3) return position.completed_batches > 0 ? 'badge-t31' : 'badge-t3';
  return 'badge-t1';
}

export function buildTierBadgeNode(position, extraClass = '') {
  const badge = createNode(
    'span',
    `tier-badge ${extraClass} ${tierBadgeClass(position)}`.trim(),
  );
  if (isClosingPosition(position)) {
    const dot = createNode('span', 'w-1.5 h-1.5 rounded-full bg-[#985506] shrink-0');
    dot.setAttribute('aria-hidden', 'true');
    badge.append(dot);
  }
  badge.append(document.createTextNode(position.tier_label));
  return badge;
}

export function buildSymbolLocateButton(symbol, className, contextText = '在工作台中定位') {
  const btn = createNode('button', `symbol-locate-btn ${className}`.trim(), symbol);
  btn.type = 'button';
  btn.dataset.locateSymbol = symbol;
  btn.setAttribute('aria-label', `${contextText} ${symbol}`);
  btn.title = `点击在工作台中定位 ${symbol}`;
  return btn;
}
