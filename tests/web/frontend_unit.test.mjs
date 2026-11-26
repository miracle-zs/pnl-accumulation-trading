import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.resolve(__dirname, '../../src/pnl_trader/web/static');

class FakeClassList {
  constructor(el) {
    this.el = el;
    this._set = new Set();
  }
  _syncFromAttr(val) {
    this._set = new Set(String(val || '').split(/\s+/).filter(Boolean));
  }
  _write() {
    this.el._className = [...this._set].join(' ');
  }
  add(...tokens) {
    tokens.forEach(t => t && this._set.add(t));
    this._write();
  }
  remove(...tokens) {
    tokens.forEach(t => this._set.delete(t));
    this._write();
  }
  toggle(token, force) {
    if (force === undefined) {
      if (this._set.has(token)) this._set.delete(token);
      else this._set.add(token);
    } else if (force) {
      this._set.add(token);
    } else {
      this._set.delete(token);
    }
    this._write();
    return this._set.has(token);
  }
  contains(token) {
    return this._set.has(token);
  }
}

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this._className = '';
    this.classList = new FakeClassList(this);
    this._textContent = '';
    this.value = '';
    this.tabIndex = undefined;
    this.hidden = false;
    this.disabled = false;
    this.scrollWidth = 600;
    this.clientWidth = 600;
    this.scrollLeft = 0;
    this.scrollHeight = 100;
    this.clientHeight = 100;
    this.scrollTop = 0;
    this.listeners = new Map();
  }
  get className() {
    return this._className;
  }
  set className(val) {
    this._className = String(val || '');
    this.classList._syncFromAttr(this._className);
  }
  get id() {
    return this.attributes.get('id') || '';
  }
  set id(val) {
    this.setAttribute('id', val);
  }
  get textContent() {
    if (this.children.length === 0) return this._textContent;
    return this.children.map(c => c.textContent).join('');
  }
  set textContent(val) {
    this.children = [];
    this._textContent = val === null || val === undefined ? '' : String(val);
  }
  get firstElementChild() {
    return this.children.find(c => c instanceof FakeElement) || null;
  }
  get childElementCount() {
    return this.children.filter(c => c instanceof FakeElement).length;
  }
  get rows() {
    return this.children.filter(c => c.tagName === 'TR');
  }
  setAttribute(name, val) {
    const str = String(val);
    this.attributes.set(name, str);
    if (name === 'class') this.className = str;
    if (name === 'id') this.ownerDocument._byId.set(str, this);
    if (name.startsWith('data-')) {
      const key = name.slice(5);
      this.dataset[key] = str;
    }
  }
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === 'id') this.ownerDocument._byId.delete(this.id);
  }
  append(...nodes) {
    this._textContent = '';
    for (const node of nodes) {
      if (typeof node === 'string') {
        this.children.push({ textContent: node, parentNode: this });
      } else if (node) {
        if (node.parentNode && node.parentNode !== this) {
          const idx = node.parentNode.children.indexOf(node);
          if (idx >= 0) node.parentNode.children.splice(idx, 1);
        }
        node.parentNode = this;
        this.children.push(node);
      }
    }
  }
  appendChild(node) {
    this.append(node);
    return node;
  }
  moveBefore(node, refNode) {
    const idx = this.children.indexOf(node);
    if (idx >= 0) this.children.splice(idx, 1);
    if (refNode === null || refNode === undefined) {
      this.children.push(node);
    } else {
      const refIdx = this.children.indexOf(refNode);
      this.children.splice(refIdx >= 0 ? refIdx : this.children.length, 0, node);
    }
    node.parentNode = this;
  }
  replaceChildren(...nodes) {
    this.children = [];
    this._textContent = '';
    this.append(...nodes);
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this.listeners.get(type);
    if (!list) return;
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector) {
    const results = [];
    const visit = (el) => {
      for (const child of el.children) {
        if (!(child instanceof FakeElement)) continue;
        if (matchesSelector(child, selector)) results.push(child);
        visit(child);
      }
    };
    visit(this);
    return results;
  }
}

function matchesSelector(el, selector) {
  if (selector.startsWith('.')) {
    return el.classList.contains(selector.slice(1));
  }
  if (selector.startsWith('#positions-table thead th[id^="th-"]')) {
    return el.tagName === 'TH' && el.id.startsWith('th-');
  }
  if (selector === '.period-btn') return el.classList.contains('period-btn');
  if (selector === '.table-shell-wrap') return el.classList.contains('table-shell-wrap');
  if (selector === '.table-shell') return el.classList.contains('table-shell');
  return false;
}

function createFakeDomFromHtml(htmlText) {
  const doc = {
    _byId: new Map(),
    body: null,
    createElement(tag) {
      return new FakeElement(tag, doc);
    },
    createTextNode(text) {
      return { textContent: String(text), parentNode: null };
    },
    getElementById(id) {
      return doc._byId.get(id) || null;
    },
    querySelector(sel) {
      return doc.body.querySelector(sel);
    },
    querySelectorAll(sel) {
      return doc.body.querySelectorAll(sel);
    },
    addEventListener() {},
    removeEventListener() {},
  };
  doc.body = new FakeElement('body', doc);

  const ids = [
    'system-status-badge', 'system-status-text', 'last-sync-label', 'slot-window-label',
    'countdown', 'slot-progress-fill', 'trading-uptime-divider', 'trading-uptime-wrap',
    'trading-uptime', 'btn-refresh', 'refresh-icon', 'refresh-text', 'dashboard-error-banner',
    'error-banner-message', 'error-banner-meta', 'btn-retry-sync', 'equity-change-badge',
    'kpi-equity', 'kpi-available', 'kpi-fill-count', 'kpi-realized-cash', 'kpi-realized-note',
    'kpi-today-pnl', 'kpi-today-note', 'kpi-active-symbols', 'kpi-unrealized-pnl',
    'kpi-position-status', 'kpi-actual-leverage', 'kpi-notional', 'kpi-margin-cushion',
    'equity-snapshot-status', 'canvas-container', 'equity-canvas', 'chart-empty-overlay',
    'chart-sr-status', 'chart-tooltip', 'tooltip-time', 'tooltip-indicator', 'tooltip-val',
    'tooltip-delta', 'chart-start-time', 'chart-latest-time', 'drawdown-period',
    'max-drawdown', 'max-drawdown-pct', 'current-drawdown-pct', 'execution-issue-count',
    'execution-issues', 'execution-issues-hint', 'tier-total-badge', 'exposure-priced-count',
    'bar-t1', 'bar-t2', 'bar-t3', 'bar-t31', 'tier-cnt-t1', 'tier-cnt-t2', 'tier-cnt-t3',
    'tier-cnt-t31', 'tier-exposure-list', 'losers-count', 'losers-list', 'weekly-fill-count',
    'weekly-commission', 'cost-days', 'other-fees-note', 'tab-positions', 'tab-fills',
    'tab-ranking', 'positions-toolbar', 'tier-filter-group', 'filter-all', 'filter-t1',
    'filter-t2', 'filter-t3', 'symbol-search', 'view-positions', 'positions-sort-status',
    'positions-table', 'position-table-body', 'view-fills', 'buy-fills-count',
    'buy-fills-list', 'sell-fills-count', 'sell-fills-list', 'view-ranking',
    'ranking-container', 'position-footer', 'position-summary-text', 'table-scroll-hint',
  ];
  for (const id of ids) {
    const tag = id.includes('list') && id !== 'tier-exposure-list' && id !== 'losers-list' ? 'div'
      : id === 'losers-list' || id === 'position-table-body' ? 'tbody'
      : id === 'positions-table' ? 'table'
      : id === 'symbol-search' ? 'input'
      : 'div';
    const el = doc.createElement(tag);
    el.setAttribute('id', id);
    doc.body.append(el);
  }

  const wrap = doc.createElement('div');
  wrap.className = 'table-shell-wrap';
  const shell = doc.createElement('div');
  shell.className = 'table-shell';
  wrap.append(shell);
  doc.body.append(wrap);

  const thead = doc.createElement('thead');
  const headRow = doc.createElement('tr');
  for (const [k, t] of [
    ['symbol', 'string'],
    ['tier', 'number'],
    ['scan_price', 'number'],
    ['entry_notional', 'number'],
    ['estimated_unrealized_pnl', 'number'],
  ]) {
    const th = doc.createElement('th');
    th.setAttribute('id', `th-${k}`);
    th.setAttribute('data-type', t);
    th.textContent = k;
    headRow.append(th);
  }
  thead.append(headRow);
  doc.getElementById('positions-table').append(thead);
  doc.getElementById('positions-table').append(doc.getElementById('position-table-body'));

  for (const p of ['24h', '7d', '30d']) {
    const btn = doc.createElement('button');
    btn.className = 'seg-btn period-btn';
    btn.setAttribute('data-period', p);
    btn.setAttribute('aria-pressed', String(p === '7d'));
    doc.body.append(btn);
  }

  // Sanity check that every ID we mocked actually exists in index.html
  for (const id of ids) {
    assert.ok(htmlText.includes(`id="${id}"`), `Missing id="${id}" in index.html`);
  }
  return doc;
}

const htmlText = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
globalThis.document = createFakeDomFromHtml(htmlText);
globalThis.window = { devicePixelRatio: 1 };

const renderers = await import(path.join(staticDir, 'js/renderers.js'));
const chart = await import(path.join(staticDir, 'js/chart.js'));

test('financial formatters avoid -$0.00, -0.00%, $NaN, and $—', () => {
  assert.equal(renderers.formatSignedUsd(-0.004), '$0.00');
  assert.equal(renderers.formatSignedUsd(-0.006), '-$0.01');
  assert.equal(renderers.formatSignedUsd(1234.5), '+$1,234.50');
  assert.equal(renderers.formatSignedUsd(Number.NaN), '—');
  assert.equal(renderers.formatUsd(-0.004), '$0.00');
  assert.equal(renderers.formatUsd(null), '—');
  assert.equal(renderers.formatSignedPercent(-0.004), '0.00%');
  assert.equal(renderers.formatSignedPercent(0.006), '+0.01%');
  assert.equal(renderers.formatUsdPrice(null), '—');
  assert.equal(renderers.formatUsdPrice(undefined), '—');
  assert.equal(renderers.formatUsdPrice(0.000123456), '$0.000123456');
  assert.equal(renderers.formatUsdPrice(0.0001234567), '$0.000123457');
  assert.equal(chart.formatChartUsd(Number.NaN, 0), '—');
  assert.equal(chart.formatChartUsd(-0.004, 2, { signed: true }), '$0.00');
});

test('Beijing time formatter renders UTC+8 regardless of local timezone', () => {
  // 2026-11-26T00:05:30Z => 2026-11-26 08:05:30 in Asia/Shanghai
  const ms = Date.UTC(2026, 10, 26, 0, 5, 30);
  assert.equal(renderers.formatBeijingTime(ms), '08:05:30');
  assert.equal(renderers.formatBeijingHm(ms), '08:05');
  assert.match(renderers.formatBeijingDateHm(ms), /2026\/11\/26 08:05/);
});

test('compareValues treats NaN and null as missing and sorts deterministically', () => {
  assert.equal(renderers.compareValues(Number.NaN, 10, 'number', 'asc'), 1);
  assert.equal(renderers.compareValues(10, Number.NaN, 'number', 'desc'), -1);
  assert.equal(renderers.compareValues(null, undefined, 'number', 'asc'), 0);
  assert.ok(renderers.compareValues('BTC2USDT', 'BTC10USDT', 'string', 'asc') < 0);
});

test('computeNiceScale & stepPrecision handle flat and fractional ranges', () => {
  const flat = chart.computeNiceScale(1000, 1000, 4);
  assert.ok(flat.ticks.length >= 2);
  assert.ok(flat.maxVal > flat.minVal);
  assert.equal(chart.stepPrecision(2.5), 1);
  assert.equal(chart.stepPrecision(0.25), 2);
  assert.equal(chart.stepPrecision(10), 0);
});

test('renderDrawdown uses muted slate for missing/zero values and warning colors only > 0', () => {
  renderers.renderDrawdown({
    period: '7d',
    points: [{ captured_at_ms: 1, equity: 100 }],
    max_drawdown: null,
    max_drawdown_pct: null,
    current_drawdown_pct: null,
  });
  assert.equal(document.getElementById('max-drawdown').textContent, '—');
  assert.ok(document.getElementById('max-drawdown').className.includes('text-[#526a73]'));
  assert.ok(!document.getElementById('max-drawdown').className.includes('text-[#bd2b2b]'));

  renderers.renderDrawdown({
    period: '30d',
    points: [
      { captured_at_ms: 1, equity: 100 },
      { captured_at_ms: 2, equity: 90 },
    ],
    max_drawdown: 10,
    max_drawdown_pct: 10,
    current_drawdown_pct: 10,
  });
  assert.equal(document.getElementById('drawdown-period').textContent, '最近 30 天');
  assert.ok(document.getElementById('max-drawdown').className.includes('text-[#bd2b2b]'));
  assert.ok(document.getElementById('current-drawdown-pct').className.includes('text-[#985506]'));
});

test('renderEquitySummary prefixes period on badge and handles missing leverage safely', () => {
  renderers.renderEquitySummary({
    period: '30d',
    points: [{ captured_at_ms: 1700000000000, equity: 1000 }],
    latest: {
      captured_at_ms: 1700000000000,
      is_current_slot: true,
      equity: 1000,
      available_balance: 500,
      position_notional: 2000,
      unrealized_pnl: 25,
      leverage: 2,
    },
    change: 50,
    change_pct: 5,
  });
  assert.equal(document.getElementById('equity-change-badge').textContent, '近30天 +$50.00 (+5.00%)');
  assert.equal(document.getElementById('kpi-actual-leverage').textContent, '实际杠杆 2.00x');
});

test('renderPositions distinguishes CLOSING badges, formats missing prices cleanly, and preserves row identity across polls & sorts', () => {
  renderers.state.ledgerAvailable = true;
  renderers.state.currentTierFilter = 'ALL';
  renderers.state.searchQuery = '';
  renderers.state.currentSortKey = 'tier';
  renderers.state.currentSortDirection = 'desc';
  renderers.state.allPositions = [
    {
      symbol: 'ADAUSDT',
      tier: 3,
      lifecycle: 'CLOSING',
      tier_label: 'Tier 3 · 待全平',
      completed_batches: 0,
      avg_entry_price: 0.6,
      scan_price: 0.58,
      tp_price: null,
      dca_price: null,
      writeoff_price: null,
      qty: 100,
      entry_notional: 60,
      estimated_unrealized_pnl: -2,
    },
    {
      symbol: 'BTCUSDT',
      tier: 1,
      lifecycle: 'OPEN',
      tier_label: 'Tier 1',
      completed_batches: 0,
      avg_entry_price: 60000,
      scan_price: null,
      tp_price: 63000,
      dca_price: 54000,
      writeoff_price: null,
      qty: 0.001,
      entry_notional: 60,
      estimated_unrealized_pnl: null,
    },
  ];

  renderers.renderPositions();
  const tbody = document.getElementById('position-table-body');
  assert.equal(tbody.rows.length, 2);
  const firstRow = tbody.rows[0];
  assert.equal(firstRow.dataset.symbol, 'ADAUSDT');
  assert.ok(firstRow.children[1].children[0].classList.contains('badge-closing'));
  assert.equal(firstRow.children[4].textContent, '—');
  assert.ok(!firstRow.children[4].className.includes('text-[#0a7052]'));
  assert.equal(tbody.rows[1].children[3].textContent, '—');

  // Re-rendering with updated price updates row in place without replacing <tr> node
  renderers.state.allPositions[0].estimated_unrealized_pnl = -3.5;
  renderers.renderPositions();
  assert.equal(tbody.rows[0], firstRow);
  assert.ok(firstRow.children[8].textContent.includes('-$3.50'));

  // Sorting reorders existing rows cleanly and keeps renderCache coherent
  renderers.handleSort('symbol');
  assert.equal(tbody.rows[0].dataset.symbol, 'ADAUSDT');
  renderers.handleSort('symbol'); // desc
  assert.equal(tbody.rows[0].dataset.symbol, 'BTCUSDT');

  // Empty filtered state offers a reset button
  renderers.state.searchQuery = 'NONEXISTENT';
  renderers.renderPositions();
  assert.ok(tbody.querySelector('[data-reset-filters]') || tbody.rows[0].children[0].children[0].children[1].dataset.resetFilters === 'true');
});

test('renderExecutionIssues places critical issues before informational ones and renderRanking uses flat hairline rows', () => {
  renderers.state.ledgerAvailable = true;
  renderers.renderExecutionIssues([
    { symbol: 'DOGEUSDT', category: 'PRICE_MISSING', detail: 'missing price', occurred_at_ms: 2000 },
    { symbol: 'ORDIUSDT', category: 'ORDER_REJECTED', detail: 'rejected', occurred_at_ms: 1000 },
  ]);
  const issues = document.getElementById('execution-issues');
  assert.ok(issues.children[0].textContent.includes('ORDIUSDT'));
  assert.ok(issues.children[1].textContent.includes('DOGEUSDT'));

  renderers.renderRanking([{ symbol: '1000PEPEUSDT', realized_gross: 42.5 }], 0);
  const ranking = document.getElementById('ranking-container');
  const rowClass = ranking.children[0].className;
  assert.ok(!rowClass.includes('rounded border'));
  assert.ok(rowClass.includes('border-b'));
});
