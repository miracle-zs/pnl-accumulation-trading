// Dashboard Summary Panels: Header Telemetry, KPIs, Drawdown, Diagnostics & Ranking

import {
  state,
  PERIOD_LABELS,
  DRAWDOWN_PERIOD_LABELS,
  formatBeijingDateTime,
  formatBeijingHm,
  isRenderUnchanged,
  saveRenderSnapshot,
  normalizeRoundedZero,
  formatSignedUsd,
  formatUsd,
  formatSignedPercent,
  pnlColorClass,
  createNode,
  createTimeNode,
  showTableMessage,
  showMessage,
  buildTierBadgeNode,
  buildSymbolLocateButton,
} from './state.js?v=20261128-1';

const CRITICAL_ISSUE_CATEGORIES = new Set([
  'REVIEW_RUN_ERROR',
  'REVIEW_ERROR',
  'ORDER_REJECTED',
  'ORDER_UNKNOWN',
  'ORDER_PARTIALLY_FILLED',
  'INTENT_CONFLICT',
  'LEVERAGE_UNAVAILABLE',
]);

let uptimeRefs = null;
const lastUptimeState = { visible: null, text: null };

export function renderTradingUptime() {
  if (!uptimeRefs) {
    uptimeRefs = {
      wrapEl: document.getElementById('trading-uptime-wrap'),
      dividerEl: document.getElementById('trading-uptime-divider'),
      uptimeEl: document.getElementById('trading-uptime'),
    };
  }
  const { wrapEl, dividerEl, uptimeEl } = uptimeRefs;

  if (state.tradingUptimeBaseSeconds === null) {
    if (lastUptimeState.visible !== false) {
      wrapEl.className = 'hidden flex-col text-xs leading-tight shrink-0';
      dividerEl.className = 'h-6 w-px bg-[#255362] hidden';
      lastUptimeState.visible = false;
    }
    if (lastUptimeState.text !== '—') {
      uptimeEl.textContent = '—';
      lastUptimeState.text = '—';
    }
    return;
  }

  if (lastUptimeState.visible !== true) {
    wrapEl.className = 'hidden sm:flex flex-col text-xs leading-tight shrink-0';
    dividerEl.className = 'h-6 w-px bg-[#255362] hidden sm:block';
    lastUptimeState.visible = true;
  }

  const elapsedSeconds = state.tradingUptimeBaseSeconds +
    Math.floor((performance.now() - state.tradingUptimeUpdatedAt) / 1000);
  const totalMinutes = Math.floor(elapsedSeconds / 60);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  const text = `${days}天 ${hours}时 ${minutes}分`;
  if (lastUptimeState.text !== text) {
    uptimeEl.textContent = text;
    lastUptimeState.text = text;
  }
}

export function updatePositionStatusKpi(positions) {
  const statusEl = document.getElementById('kpi-position-status');
  if (!state.ledgerAvailable || positions.length === 0) {
    statusEl.textContent = '暂无持仓';
    statusEl.className = 'font-display font-medium tabular-nums text-[#526a73]';
    return;
  }
  const priced = positions.filter(p => Number.isFinite(Number(p.estimated_unrealized_pnl)) && p.estimated_unrealized_pnl !== null);
  if (priced.length === 0) {
    statusEl.textContent = '价格采样中';
    statusEl.className = 'font-display font-medium tabular-nums text-[#526a73]';
    return;
  }
  const profitCount = priced.filter(p => Number(p.estimated_unrealized_pnl) > 0).length;
  const lossCount = priced.filter(p => Number(p.estimated_unrealized_pnl) < 0).length;
  statusEl.textContent = `${profitCount} 盈 / ${lossCount} 亏`;
  const toneClass = lossCount > 0 ? 'text-[#985506]' : profitCount > 0 ? 'text-[#0a7052]' : 'text-[#3f5760]';
  statusEl.className = `font-display font-semibold tabular-nums ${toneClass}`;
}

export function renderSnapshotStatusFromState() {
  const snapshotStatus = document.getElementById('equity-snapshot-status');
  snapshotStatus.setAttribute('aria-busy', 'false');
  const pointCount = state.chartHistoryPoints?.length ?? 0;
  if (state.lastSnapshotCapturedAtMs === null) {
    snapshotStatus.textContent = '等待账户净值采样';
    snapshotStatus.className = 'text-xs mt-0.5 tabular-nums text-[#526a73]';
    return;
  }
  const isWarn = !state.lastSnapshotIsCurrentSlot;
  snapshotStatus.replaceChildren(
    document.createTextNode('采样：'),
    createTimeNode(state.lastSnapshotCapturedAtMs, 'font-mono tabular-nums', formatBeijingDateTime),
  );
  if (isWarn) {
    snapshotStatus.append(document.createTextNode(' · 等待本轮快照'));
  }
  if (pointCount === 1) {
    snapshotStatus.append(document.createTextNode(' · 仅 1 个采样点（需至少 2 点绘线）'));
  }
  snapshotStatus.className = `text-xs mt-0.5 tabular-nums ${isWarn ? 'text-[#985506] font-medium' : 'text-[#526a73]'}`;
}

export function renderEquitySummary(eq) {
  if (eq.latest && Number.isFinite(Number(eq.latest.captured_at_ms))) {
    state.lastSnapshotCapturedAtMs = Number(eq.latest.captured_at_ms);
    state.lastSnapshotIsCurrentSlot = Boolean(eq.latest.is_current_slot);
  } else {
    state.lastSnapshotCapturedAtMs = null;
    state.lastSnapshotIsCurrentSlot = true;
  }
  renderSnapshotStatusFromState();

  const kpiEquity = document.getElementById('kpi-equity');
  const kpiAvailable = document.getElementById('kpi-available');
  const kpiNotional = document.getElementById('kpi-notional');
  const kpiLeverage = document.getElementById('kpi-actual-leverage');
  const kpiUnrealized = document.getElementById('kpi-unrealized-pnl');
  const kpiCushion = document.getElementById('kpi-margin-cushion');

  const kpiBaseClass = 'font-display text-[22px] xl:text-[28px] leading-none font-bold tabular-nums tracking-tight min-w-0';

  if (eq.latest) {
    kpiEquity.textContent = formatUsd(eq.latest.equity);
    kpiAvailable.textContent = formatUsd(eq.latest.available_balance);
    kpiNotional.textContent = formatUsd(eq.latest.position_notional);
    const lev = Number(eq.latest.leverage);
    kpiLeverage.textContent = eq.latest.leverage !== null && Number.isFinite(lev)
      ? `实际杠杆 ${lev.toFixed(2)}x`
      : '实际杠杆 —';

    const unpnl = eq.latest.unrealized_pnl;
    kpiUnrealized.textContent = formatSignedUsd(unpnl);
    kpiUnrealized.className = `${kpiBaseClass} ${pnlColorClass(unpnl)}`;

    const eqNum = Number(eq.latest.equity);
    const availNum = Number(eq.latest.available_balance);
    if (Number.isFinite(eqNum) && eqNum > 0 && Number.isFinite(availNum)) {
      const cushionPct = (availNum / eqNum) * 100;
      let cushionStatus = '充裕';
      let cushionColor = 'text-[#0a7052]';
      if (cushionPct < 25) {
        cushionStatus = '告警';
        cushionColor = 'text-[#bd2b2b]';
      } else if (cushionPct < 45) {
        cushionStatus = '偏紧';
        cushionColor = 'text-[#985506]';
      }
      kpiCushion.textContent = `${cushionPct.toFixed(1)}% · ${cushionStatus}`;
      kpiCushion.className = `font-display font-semibold tabular-nums ${cushionColor}`;
    } else {
      kpiCushion.textContent = '—';
      kpiCushion.className = 'font-display font-medium tabular-nums text-[#526a73]';
    }
  } else {
    kpiEquity.textContent = '—';
    kpiAvailable.textContent = '—';
    kpiNotional.textContent = '—';
    kpiLeverage.textContent = '实际杠杆 —';
    kpiUnrealized.textContent = '—';
    kpiUnrealized.className = `${kpiBaseClass} text-[#526a73]`;
    kpiCushion.textContent = '—';
    kpiCushion.className = 'font-display font-medium tabular-nums text-[#526a73]';
  }

  const badge = document.getElementById('equity-change-badge');
  const periodPrefix = PERIOD_LABELS[eq.period] ? `近${PERIOD_LABELS[eq.period].replace(' ', '')} ` : '';
  if (eq.change !== null && eq.change !== undefined && Number.isFinite(Number(eq.change))) {
    const changeVal = normalizeRoundedZero(Number(eq.change), 2);
    const percentage = eq.change_pct === null || eq.change_pct === undefined || !Number.isFinite(Number(eq.change_pct))
      ? ''
      : ` (${formatSignedPercent(eq.change_pct)})`;
    badge.textContent = `${periodPrefix}${formatSignedUsd(changeVal)}${percentage}`;
    const badgeTone = changeVal > 0
      ? 'bg-[#e6f5ef] text-[#0a7052]'
      : changeVal < 0
        ? 'bg-[#fcecec] text-[#bd2b2b]'
        : 'bg-[#eaf0ee] text-[#3f5760]';
    badge.className = `font-display tabular-nums text-xs font-semibold px-1.5 py-0.5 rounded ${badgeTone}`;
  } else {
    badge.textContent = periodPrefix ? `${periodPrefix.trim()} —` : '—';
    badge.className = 'font-display tabular-nums text-xs font-semibold px-1.5 py-0.5 rounded bg-[#eaf0ee] text-[#3f5760]';
  }
}

export function renderAccountKpis(ov, an) {
  const pendingCloseFills = ov.pending_close_fill_count ?? 0;
  const realizedCash = state.ledgerAvailable ? ov.realized_cash : null;
  const realizedEl = document.getElementById('kpi-realized-cash');
  const realizedNoteEl = document.getElementById('kpi-realized-note');
  realizedEl.textContent = formatSignedUsd(realizedCash);
  realizedEl.className = `font-display text-[22px] xl:text-[28px] leading-none font-bold tabular-nums tracking-tight min-w-0 ${
    pnlColorClass(realizedCash)
  }`;

  const realizedCaveats = [];
  if (pendingCloseFills > 0) {
    realizedCaveats.push(`含 ${pendingCloseFills} 笔平仓待补录`);
  }
  if ((ov.other_fee_count ?? 0) > 0) {
    realizedCaveats.push(`含 ${ov.other_fee_count} 笔非 USDT 手续费`);
  }
  if (!state.ledgerAvailable) {
    realizedNoteEl.textContent = '等待账本 · 已扣手续费，未计资金费用';
    realizedNoteEl.className = 'text-xs text-[#526a73] leading-snug mt-1.5';
  } else if (realizedCaveats.length > 0) {
    const detailText = `${realizedCaveats.join('，')}，净盈亏暂缓统计（未计资金费用）`;
    realizedNoteEl.textContent = detailText;
    realizedNoteEl.className = 'text-xs text-[#985506] font-medium leading-snug mt-1.5';
  } else {
    realizedNoteEl.textContent = '已扣手续费 · 未计资金费用';
    realizedNoteEl.className = 'text-xs text-[#526a73] leading-snug mt-1.5';
  }

  document.getElementById('kpi-fill-count').textContent = state.ledgerAvailable ? `${ov.fill_count} 笔成交订单` : '—';
  document.getElementById('kpi-active-symbols').textContent = state.ledgerAvailable ? `${ov.active_positions_count} 个标的` : '—';

  const todayPnl = document.getElementById('kpi-today-pnl');
  const todayNoteEl = document.getElementById('kpi-today-note');
  const todayPendingCloseFills = an.today.pending_close_fill_count ?? 0;
  const realizedNet = state.ledgerAvailable ? an.today.realized_net : null;
  todayPnl.textContent = formatSignedUsd(realizedNet);
  todayPnl.className = `font-display font-semibold tabular-nums ${pnlColorClass(realizedNet)}`;

  const todayCaveats = [];
  if (todayPendingCloseFills > 0) {
    todayCaveats.push(`今日 ${todayPendingCloseFills} 笔平仓待补录`);
  }
  if ((an.today.other_fee_count ?? 0) > 0) {
    todayCaveats.push(`今日 ${an.today.other_fee_count} 笔非 USDT 费用`);
  }
  if (state.ledgerAvailable && todayCaveats.length > 0) {
    const todayDetail = `${todayCaveats.join('，')}，暂不统计`;
    todayNoteEl.textContent = todayDetail;
    todayNoteEl.classList.remove('hidden');
  } else {
    todayNoteEl.textContent = '';
    todayNoteEl.classList.add('hidden');
  }
}

export function renderTierBars(td) {
  if (state.ledgerAvailable) {
    document.getElementById('tier-cnt-t1').textContent = `${td.t1} 币`;
    document.getElementById('tier-cnt-t2').textContent = `${td.t2 + td.t21} 币`;
    document.getElementById('tier-cnt-t3').textContent = `${td.t3} 币`;
    document.getElementById('tier-cnt-t31').textContent = `${td.t31 + td.t32} 币`;
    document.getElementById('tier-total-badge').textContent = `${td.total} 个标的`;

    const percentagePerSymbol = td.total > 0 ? 100 / td.total : 0;
    document.getElementById('bar-t1').style.width = `${td.t1 * percentagePerSymbol}%`;
    document.getElementById('bar-t2').style.width = `${(td.t2 + td.t21) * percentagePerSymbol}%`;
    document.getElementById('bar-t3').style.width = `${td.t3 * percentagePerSymbol}%`;
    document.getElementById('bar-t31').style.width = `${(td.t31 + td.t32) * percentagePerSymbol}%`;
  } else {
    ['tier-cnt-t1', 'tier-cnt-t2', 'tier-cnt-t3', 'tier-cnt-t31'].forEach(id => {
      document.getElementById(id).textContent = '—';
    });
    document.getElementById('tier-total-badge').textContent = '— 个标的';
    ['bar-t1', 'bar-t2', 'bar-t3', 'bar-t31'].forEach(id => {
      document.getElementById(id).style.width = '0%';
    });
  }
}

export function renderDrawdown(equity) {
  const periodText = DRAWDOWN_PERIOD_LABELS[equity.period] || equity.period || '—';
  document.getElementById('drawdown-period').textContent = periodText;

  const points = Array.isArray(equity.points) ? equity.points : [];
  const hasSamples = points.length >= 2;
  const maxDd = Number(equity.max_drawdown);
  const maxDdPct = Number(equity.max_drawdown_pct);
  const curDdPct = Number(equity.current_drawdown_pct);

  const maxDdEl = document.getElementById('max-drawdown');
  const maxDdPctEl = document.getElementById('max-drawdown-pct');
  const curDdPctEl = document.getElementById('current-drawdown-pct');

  if (hasSamples && equity.max_drawdown !== null && Number.isFinite(maxDd)) {
    maxDdEl.textContent = formatUsd(maxDd);
    maxDdEl.className = `font-display text-xl font-bold tabular-nums mt-1 ${
      maxDd > 0 ? 'text-[#bd2b2b]' : 'text-[#3f5760]'
    }`;
  } else {
    maxDdEl.textContent = '—';
    maxDdEl.className = 'font-display text-xl font-bold tabular-nums text-[#526a73] mt-1';
  }

  if (hasSamples && equity.max_drawdown_pct !== null && Number.isFinite(maxDdPct)) {
    maxDdPctEl.textContent = `${normalizeRoundedZero(maxDdPct, 2).toFixed(2)}%`;
    maxDdPctEl.className = `font-display text-xs font-medium tabular-nums mt-0.5 ${
      maxDdPct > 0 ? 'text-[#3f5760]' : 'text-[#526a73]'
    }`;
  } else {
    maxDdPctEl.textContent = '采样不足';
    maxDdPctEl.className = 'font-display text-xs font-medium tabular-nums text-[#526a73] mt-0.5';
  }

  if (hasSamples && equity.current_drawdown_pct !== null && Number.isFinite(curDdPct)) {
    curDdPctEl.textContent = `${normalizeRoundedZero(curDdPct, 2).toFixed(2)}%`;
    curDdPctEl.className = `font-display text-xl font-bold tabular-nums mt-1 ${
      curDdPct > 0 ? 'text-[#985506]' : 'text-[#3f5760]'
    }`;
  } else {
    curDdPctEl.textContent = '—';
    curDdPctEl.className = 'font-display text-xl font-bold tabular-nums text-[#526a73] mt-1';
  }
}

export function renderLosingPositions(positions) {
  const body = document.getElementById('losers-list');
  const countEl = document.getElementById('losers-count');

  if (!state.ledgerAvailable) {
    const key = 'WAIT';
    if (isRenderUnchanged(body, key)) return;
    countEl.textContent = '等待账本';
    showTableMessage(body, 4, 'py-8 text-center text-[#526a73]', '等待账本数据');
    saveRenderSnapshot(body, key);
    return;
  }
  if (positions.length === 0) {
    const key = 'EMPTY';
    if (isRenderUnchanged(body, key)) return;
    countEl.textContent = '0 持仓';
    showTableMessage(body, 4, 'py-8 text-center text-[#526a73]', '暂无托管持仓');
    saveRenderSnapshot(body, key);
    return;
  }
  const pricedPositions = positions
    .filter(position => position.estimated_unrealized_pnl !== null
      && position.estimated_unrealized_pnl !== undefined
      && Number.isFinite(Number(position.estimated_unrealized_pnl)));
  const losses = pricedPositions
    .filter(position => Number(position.estimated_unrealized_pnl) < 0)
    .sort((a, b) => Number(a.estimated_unrealized_pnl) - Number(b.estimated_unrealized_pnl));

  const topLosses = losses.slice(0, 6);
  const key = JSON.stringify([
    positions.length,
    pricedPositions.length,
    losses.length,
    topLosses.map(p => [
      p.symbol,
      p.tier,
      p.lifecycle,
      p.tier_label,
      p.completed_batches,
      p.estimated_unrealized_pnl,
      p.price_captured_at_ms,
    ]),
  ]);
  if (isRenderUnchanged(body, key)) return;

  countEl.textContent = pricedPositions.length > 0
    ? `${losses.length} 浮亏 · ${pricedPositions.length}/${positions.length} 已估值`
    : '等待价格采样';

  if (losses.length === 0) {
    const message = pricedPositions.length > 0
      ? '当前无浮亏持仓'
      : '持仓价格将在下一轮巡检后显示';
    showTableMessage(body, 4, 'py-8 text-center text-[#0a7052]', message);
    saveRenderSnapshot(body, key);
    return;
  }

  const rows = topLosses.map(position => {
    const row = createNode('tr', 'hover:bg-[#f2f7f5] transition-colors');
    const symbolCell = createNode('td', 'py-2 pr-1.5 sm:pr-2 whitespace-nowrap');
    symbolCell.append(
      buildSymbolLocateButton(
        position.symbol,
        'font-display font-semibold text-[#102128]',
        '在持仓工作台中定位',
      ),
    );
    const tierCell = createNode('td', 'py-2 px-1.5 sm:px-2');
    tierCell.append(buildTierBadgeNode(position));

    const timeCell = createNode('td', 'py-2 pl-1.5 sm:pl-2 text-right font-mono tabular-nums text-[#526a73] whitespace-nowrap');
    if (Number.isFinite(Number(position.price_captured_at_ms))) {
      timeCell.append(createTimeNode(Number(position.price_captured_at_ms), '', formatBeijingHm));
    } else {
      timeCell.textContent = '—';
    }

    row.append(
      symbolCell,
      tierCell,
      createNode('td', 'py-2 px-1.5 sm:px-2 text-right font-display tabular-nums font-semibold text-[#bd2b2b] whitespace-nowrap', formatSignedUsd(position.estimated_unrealized_pnl)),
      timeCell,
    );
    return row;
  });
  body.replaceChildren(...rows);
  saveRenderSnapshot(body, key);
}

export function renderTierExposure(exposure) {
  const container = document.getElementById('tier-exposure-list');
  const countEl = document.getElementById('exposure-priced-count');

  if (!state.ledgerAvailable || exposure.total_positions === 0) {
    const key = state.ledgerAvailable ? 'EMPTY' : 'WAIT';
    if (isRenderUnchanged(container, key)) return;
    countEl.textContent = state.ledgerAvailable ? '暂无持仓' : '等待账本';
    showMessage(container, 'text-xs text-[#526a73] py-4', '暂无托管持仓');
    saveRenderSnapshot(container, key);
    return;
  }

  const key = JSON.stringify([
    exposure.priced_positions,
    exposure.total_positions,
    exposure.t1,
    exposure.t2,
    exposure.t3,
    exposure.total,
  ]);
  if (isRenderUnchanged(container, key)) return;

  countEl.textContent = `${exposure.priced_positions}/${exposure.total_positions} 已估值`;

  const tiers = [
    { key: 't1', label: 'T1 敞口', barColor: '#1a7692' },
    { key: 't2', label: 'T2 敞口 (含半仓)', barColor: '#b86c0e' },
    { key: 't3', label: 'T3 敞口 (含减仓)', barColor: '#cc3b3b' },
  ];

  const rows = tiers.map(tier => {
    const amount = Number(exposure[tier.key]) || 0;
    const ratio = exposure.total > 0 ? (amount / exposure.total) * 100 : 0;
    const row = document.createElement('div');
    const labels = createNode('div', 'flex items-center justify-between gap-2 text-xs mb-1.5');
    const amountLabel = createNode('span', 'font-display tabular-nums text-[#102128] font-semibold whitespace-nowrap');
    amountLabel.append(
      document.createTextNode(`${formatUsd(amount)} `),
      createNode('span', 'text-[#526a73] font-medium', `${ratio.toFixed(1)}%`),
    );
    labels.append(createNode('span', 'text-[#3f5760]', tier.label), amountLabel);

    const track = createNode('div', 'viz-bar-track h-2 rounded-sm bg-[#eaf0ee] overflow-hidden');
    const bar = createNode('div', 'viz-bar-fill h-full rounded-sm');
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
  saveRenderSnapshot(container, key);
}

export function renderCosts(costs) {
  const rows = document.getElementById('cost-days');
  const weeklyCommEl = document.getElementById('weekly-commission');
  const weeklyCountEl = document.getElementById('weekly-fill-count');
  const otherFeesNote = document.getElementById('other-fees-note');

  if (!state.ledgerAvailable) {
    const key = 'WAIT';
    if (isRenderUnchanged(rows, key)) return;
    weeklyCommEl.textContent = '—';
    weeklyCountEl.textContent = '—';
    otherFeesNote.classList.add('hidden');
    showMessage(rows, 'text-xs text-[#526a73] py-2', '等待账本数据');
    saveRenderSnapshot(rows, key);
    return;
  }

  const days = costs.days;
  const recentDays = days.slice(-7).reverse();
  const key = JSON.stringify([
    costs.commission_usdt,
    costs.fill_count,
    costs.other_fee_count,
    costs.pending_close_fill_count,
    recentDays.map(d => [d.day, d.commission_usdt, d.fill_count]),
  ]);
  if (isRenderUnchanged(rows, key)) return;

  weeklyCommEl.textContent = formatUsd(costs.commission_usdt);
  weeklyCountEl.textContent = `${costs.fill_count} 笔订单`;

  const warnings = [];
  if (costs.other_fee_count > 0) warnings.push(`含 ${costs.other_fee_count} 笔非 USDT 规费（如 BNB），未折算入总额。`);
  if (costs.pending_close_fill_count > 0) warnings.push(`含 ${costs.pending_close_fill_count} 笔待对账平仓，规费统计可能未完结。`);
  otherFeesNote.textContent = warnings.join(' ');
  otherFeesNote.classList.toggle('hidden', warnings.length === 0);

  if (days.length === 0) {
    showMessage(rows, 'text-xs text-[#526a73] py-2', '近 7 日暂无成交');
    saveRenderSnapshot(rows, key);
    return;
  }

  const maxDayCost = Math.max(...recentDays.map(d => Number(d.commission_usdt) || 0), 0.0001);

  const dayRows = recentDays.map(day => {
    const ratio = Math.min(100, (Number(day.commission_usdt) / maxDayCost) * 100);
    const row = createNode('div', 'grid grid-cols-[44px_1fr_auto] items-center gap-2.5 text-xs');
    const date = createNode('time', 'text-[#526a73] font-mono tabular-nums', day.day.slice(5));
    date.dateTime = day.day;

    const barWrap = createNode('div', 'flex items-center gap-2 min-w-0');
    const track = createNode('div', 'viz-bar-track flex-1 h-1.5 rounded-sm bg-[#eaf0ee] overflow-hidden');
    const fill = createNode('div', 'viz-bar-fill h-full rounded-sm bg-[#1a7692]');
    fill.style.width = `${day.commission_usdt > 0 ? Math.max(6, ratio) : 0}%`;
    track.append(fill);
    barWrap.append(track, createNode('span', 'text-xs tabular-nums text-[#526a73] w-9 text-right shrink-0', `${day.fill_count}笔`));

    row.append(
      date,
      barWrap,
      createNode('span', 'text-[#102128] font-display tabular-nums font-semibold text-right min-w-[3.5rem] w-auto shrink-0', formatUsd(day.commission_usdt)),
    );
    return row;
  });
  rows.replaceChildren(...dayRows);
  saveRenderSnapshot(rows, key);
}

export function updateExecutionIssuesScrollHint() {
  const container = document.getElementById('execution-issues');
  const hint = document.getElementById('execution-issues-hint');
  if (!container || !hint) return;
  const hasOverflow = container.scrollHeight > container.clientHeight + 2;
  const atBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 2;
  hint.classList.toggle('hidden', !hasOverflow || atBottom);
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
    const key = 'WAIT';
    if (isRenderUnchanged(container, key)) return;
    countEl.textContent = '等待账本';
    countEl.className = 'text-xs tabular-nums px-2 py-0.5 rounded-full bg-[#eaf0ee] text-[#3f5760]';
    showMessage(container, 'text-xs text-[#526a73] py-6 text-center', '等待账本数据');
    saveRenderSnapshot(container, key);
    updateExecutionIssuesScrollHint();
    return;
  }

  const sortedIssues = issues.toSorted((a, b) => {
    const aCrit = CRITICAL_ISSUE_CATEGORIES.has(a.category) ? 0 : 1;
    const bCrit = CRITICAL_ISSUE_CATEGORIES.has(b.category) ? 0 : 1;
    if (aCrit !== bCrit) return aCrit - bCrit;
    return Number(b.occurred_at_ms) - Number(a.occurred_at_ms);
  });

  const key = JSON.stringify(
    sortedIssues.map(i => [i.symbol, i.category, i.detail, i.occurred_at_ms]),
  );
  if (isRenderUnchanged(container, key)) {
    updateExecutionIssuesScrollHint();
    return;
  }

  if (sortedIssues.length === 0) {
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
    saveRenderSnapshot(container, key);
    updateExecutionIssuesScrollHint();
    return;
  }

  const criticalCount = sortedIssues.filter(i => CRITICAL_ISSUE_CATEGORIES.has(i.category)).length;
  countEl.textContent = criticalCount > 0 ? `${sortedIssues.length} 项 (${criticalCount} 高优)` : `${sortedIssues.length} 项提示`;
  countEl.className = criticalCount > 0
    ? 'text-xs tabular-nums font-semibold px-2 py-0.5 rounded-full bg-[#fcecec] text-[#bd2b2b] border border-[#f0b5b5]'
    : 'text-xs tabular-nums font-semibold px-2 py-0.5 rounded-full bg-[#fdf4e3] text-[#985506] border border-[#ebcc94]';

  container.replaceChildren(...sortedIssues.map(issue => {
    const isCritical = CRITICAL_ISSUE_CATEGORIES.has(issue.category);
    const item = createNode('article', 'py-3 first:pt-2.5 last:pb-1');
    const header = createNode('div', 'flex items-center justify-between gap-2');

    const titleWrap = createNode('div', 'flex items-center gap-1.5 min-w-0');
    const iconWrap = createNode('span', isCritical ? 'text-[#bd2b2b] shrink-0' : 'text-[#985506] shrink-0');
    iconWrap.setAttribute('aria-hidden', 'true');
    iconWrap.innerHTML = isCritical
      ? '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>'
      : '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>';
    const isBatchSymbol = issue.category === 'REVIEW_RUN_ERROR' || issue.symbol === 'GLOBAL' || issue.symbol === 'SYSTEM';
    const symbolEl = isBatchSymbol
      ? createNode('span', 'font-display text-xs font-bold text-[#102128] truncate', issue.symbol)
      : buildSymbolLocateButton(issue.symbol, 'font-display text-xs font-bold text-[#102128] truncate', '在工作台中定位');
    titleWrap.append(iconWrap, symbolEl);

    const badgeClass = isCritical
      ? 'text-xs font-semibold px-1.5 py-0.5 rounded bg-[#fcecec] text-[#bd2b2b] border border-[#f0b5b5] shrink-0 whitespace-nowrap'
      : 'text-xs font-medium px-1.5 py-0.5 rounded bg-[#eaf0ee] text-[#3f5760] border border-[#d4dfdc] shrink-0 whitespace-nowrap';
    header.append(titleWrap, createNode('span', badgeClass, labels[issue.category] || issue.category));

    const detail = createNode('p', 'text-xs text-[#3f5760] mt-1.5 break-words leading-relaxed', issue.detail);
    const time = createTimeNode(issue.occurred_at_ms, 'block text-xs text-[#526a73] font-mono tabular-nums mt-1', formatBeijingDateTime);
    item.append(header, detail, time);
    return item;
  }));
  saveRenderSnapshot(container, key);
  updateExecutionIssuesScrollHint();
}

export function renderRanking(symbols, pendingCloseFills) {
  const container = document.getElementById('ranking-container');

  if (!state.ledgerAvailable || pendingCloseFills > 0) {
    const key = !state.ledgerAvailable ? 'WAIT' : 'PENDING';
    if (isRenderUnchanged(container, key)) return;
    const message = !state.ledgerAvailable ? '等待账本数据' : '平仓流水待对账，收益排名暂不可用';
    showMessage(container, 'col-span-full text-[#526a73] text-xs py-8 text-center', message);
    saveRenderSnapshot(container, key);
    return;
  }
  if (symbols.length === 0) {
    const key = 'EMPTY';
    if (isRenderUnchanged(container, key)) return;
    showMessage(container, 'col-span-full text-[#526a73] text-xs py-8 text-center', '暂无收益排名记录');
    saveRenderSnapshot(container, key);
    return;
  }

  const key = JSON.stringify(symbols.map(s => [s.symbol, s.realized_gross]));
  if (isRenderUnchanged(container, key)) return;

  const maxAbs = Math.max(...symbols.map(s => Math.abs(Number(s.realized_gross) || 0)), 0.01);

  const rows = symbols.map((symbol, idx) => {
    const val = Number(symbol.realized_gross);
    const hasMagnitude = Number.isFinite(val) && val !== 0;
    const widthPct = hasMagnitude ? Math.min(100, Math.max(4, (Math.abs(val) / maxAbs) * 100)) : 0;

    const row = createNode(
      'div',
      'grid grid-cols-[28px_minmax(0,1fr)_auto] sm:grid-cols-[32px_minmax(104px,140px)_minmax(80px,1fr)_auto] items-center gap-x-2.5 gap-y-1.5 sm:gap-3 px-2 py-2.5 border-b border-[#eaf0ee] hover:bg-[#f2f7f5] transition-colors text-xs',
    );
    const rankEl = createNode(
      'span',
      `font-display tabular-nums font-bold text-center ${idx < 3 ? 'text-[#135d73]' : 'text-[#526a73]'}`,
      `#${idx + 1}`,
    );
    const symEl = buildSymbolLocateButton(
      symbol.symbol,
      'font-display font-semibold text-[#102128] min-w-0 truncate text-left',
      '在工作台中查看',
    );

    const track = createNode('div', 'viz-bar-track col-span-full sm:col-span-1 order-last sm:order-none h-2 rounded-sm bg-[#eaf0ee] overflow-hidden');
    const fill = createNode('div', 'viz-bar-fill h-full rounded-sm');
    fill.style.width = `${widthPct}%`;
    fill.style.backgroundColor = val > 0 ? '#0a7052' : val < 0 ? '#bd2b2b' : '#768d88';
    track.append(fill);

    const valEl = createNode(
      'span',
      `font-display tabular-nums font-semibold text-right whitespace-nowrap ${pnlColorClass(val)}`,
      `${formatSignedUsd(val)} USDT`,
    );

    row.append(rankEl, symEl, track, valEl);
    return row;
  });
  container.replaceChildren(...rows);
  saveRenderSnapshot(container, key);
}
