// High-Resolution Retina HTML5 Canvas Financial Equity Chart with Interactive Crosshair

import {
  PERIOD_LABELS,
  formatUsd,
  formatSignedUsd,
  formatBeijingHm,
  formatBeijingShortDateTime,
  formatBeijingDateHm,
} from './renderers.js?v=20261128-2';

let activeHoverIndex = null;
let lastTooltipIndex = null;
let lastRenderedPoints = [];
let lastRenderedPeriod = null;
let currentGeometry = null;
let boundCanvas = null;
let boundOutsideHandler = null;
let hoverRafId = null;
let pendingClientPos = null;
let pendingKeyIndex = null;
let staticCanvas = null;
let staticCacheMeta = null;

const CHART_FONT_FAMILY =
  '"Barlow", -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif';
const SINGLE_POINT_NOTE = '当前仅 1 个权益采样点（需至少 2 个采样点以绘制走势曲线）';

export function stepPrecision(step) {
  if (!Number.isFinite(step) || step <= 0) return 2;
  const mag = Math.floor(Math.log10(step));
  let decimals = Math.max(0, -mag);
  // 2.5x steps (e.g. 2.5, 0.25) require one extra decimal digit beyond -floor(log10(step))
  const scaled = step * Math.pow(10, decimals);
  if (Math.abs(scaled - Math.round(scaled)) > 1e-6) {
    decimals += 1;
  }
  return Math.min(20, decimals);
}

export function formatChartUsd(val, decimals, { signed = false, epsilon = 1e-12 } = {}) {
  const num = Number(val);
  if (!Number.isFinite(num)) return '—';
  const rawClean = Math.abs(num) < epsilon ? 0 : num;
  const factor = 10 ** Math.min(decimals, 8);
  const rounded = decimals <= 8 ? Math.round(rawClean * factor) / factor : rawClean;
  const cleanVal = rounded === 0 ? 0 : rawClean;

  if (decimals === 2) {
    if (signed) return formatSignedUsd(cleanVal);
    return formatUsd(cleanVal);
  }
  const sign = cleanVal < 0 ? '-' : (signed && cleanVal > 0 ? '+' : '');
  const formatted = Math.abs(cleanVal).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return `${sign}$${formatted}`;
}

export function computeNiceScale(rawMin, rawMax, targetTicks = 4) {
  let min = rawMin;
  let max = rawMax;
  if (min === max || !Number.isFinite(max - min) || max - min <= 0) {
    const pad = Math.max(Math.abs(min) * 0.04, 10);
    min -= pad;
    max += pad;
  }
  const range = max - min;
  const roughStep = range / targetTicks;
  const magnitude = Math.pow(10, Math.floor(Math.log10(roughStep)));
  const normalized = roughStep / magnitude;

  let niceNormalized = 1;
  if (normalized <= 1) niceNormalized = 1;
  else if (normalized <= 2) niceNormalized = 2;
  else if (normalized <= 2.5) niceNormalized = 2.5;
  else if (normalized <= 5) niceNormalized = 5;
  else niceNormalized = 10;

  const step = niceNormalized * magnitude;
  const niceMin = Math.floor((min - range * 0.06) / step) * step;
  const niceMax = Math.ceil((max + range * 0.06) / step) * step;

  const rawCount = Math.round((niceMax - niceMin) / step);
  const tickCount = Number.isFinite(rawCount) && rawCount > 0 ? Math.min(rawCount, 20) : 1;
  const effectiveMax = niceMin + tickCount * step;

  const ticks = [];
  for (let i = 0; i <= tickCount; i++) {
    const value = niceMin + i * step;
    ticks.push(Math.abs(value) < step * 1e-9 ? 0 : value);
  }
  return { minVal: niceMin, maxVal: effectiveMax, step, ticks };
}

function formatAxisLabel(val, step) {
  const decimals = stepPrecision(step);
  const epsilon = step > 0 ? step * 1e-9 : 1e-12;
  return formatChartUsd(val, decimals, { epsilon });
}

function formatTickTime(ms, spanMs) {
  if (spanMs <= 36 * 3_600_000) {
    return formatBeijingHm(ms);
  }
  return formatBeijingShortDateTime(ms);
}

function sanitizePoints(points) {
  if (!Array.isArray(points) || points.length === 0) return [];
  const clean = [];
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const equity = Number(p?.equity);
    const ts = Number(p?.captured_at_ms);
    if (Number.isFinite(equity) && Number.isFinite(ts)) {
      clean.push({ captured_at_ms: ts, equity });
    }
  }
  return clean;
}

function computeExtremesAndDrawdown(points) {
  let min = Infinity;
  let max = -Infinity;
  let minIdx = 0;
  let maxIdx = 0;
  let runningPeakIdx = 0;
  let ddPeakIdx = 0;
  let ddTroughIdx = 0;
  let maxDdPct = 0;

  for (let i = 0; i < points.length; i++) {
    const v = points[i].equity;
    if (v < min) {
      min = v;
      minIdx = i;
    }
    if (v > max) {
      max = v;
      maxIdx = i;
    }
    if (v > points[runningPeakIdx].equity) {
      runningPeakIdx = i;
    } else if (points[runningPeakIdx].equity > 0) {
      const ddPct = (points[runningPeakIdx].equity - v) / points[runningPeakIdx].equity;
      if (ddPct > maxDdPct) {
        maxDdPct = ddPct;
        ddPeakIdx = runningPeakIdx;
        ddTroughIdx = i;
      }
    }
  }
  return { min, max, minIdx, maxIdx, ddPeakIdx, ddTroughIdx, maxDdPct };
}

function boxesOverlap(a, b, gap = 4) {
  return !(
    a.x + a.w + gap <= b.x
    || b.x + b.w + gap <= a.x
    || a.y + a.h + gap <= b.y
    || b.y + b.h + gap <= a.y
  );
}

function drawPillTag(ctx, text, box, { bg = 'rgba(255, 255, 255, 0.94)', border = '#d4dfdc', color = '#3f5760' } = {}) {
  ctx.save();
  ctx.font = `600 12px ${CHART_FONT_FAMILY}`;
  ctx.fillStyle = bg;
  ctx.strokeStyle = border;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(box.x, box.y, box.w, box.h, 3);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = color;
  ctx.textAlign = 'left';
  ctx.fillText(text, box.x + 6, box.y + 14);
  ctx.restore();
}

function buildStaticLayer(points, w, h, dpr) {
  if (!staticCanvas) {
    staticCanvas = document.createElement('canvas');
  }
  const targetW = Math.round(w * dpr);
  const targetH = Math.round(h * dpr);
  if (staticCanvas.width !== targetW || staticCanvas.height !== targetH) {
    staticCanvas.width = targetW;
    staticCanvas.height = targetH;
  }

  const ctx = staticCanvas.getContext('2d');
  ctx.resetTransform();
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  if (points.length === 0) {
    ctx.fillStyle = '#526a73';
    ctx.font = `500 13px ${CHART_FONT_FAMILY}`;
    ctx.textAlign = 'center';
    ctx.fillText('暂无权益采样数据', w / 2, h / 2);
    return null;
  }

  const isSinglePoint = points.length === 1;
  const { min, max, minIdx, maxIdx, ddPeakIdx, ddTroughIdx, maxDdPct } = computeExtremesAndDrawdown(points);
  const { minVal, maxVal, step, ticks } = computeNiceScale(min, max, 4);

  const axisFont = `500 12px ${CHART_FONT_FAMILY}`;
  ctx.font = axisFont;
  const axisLabels = ticks.map(val => formatAxisLabel(val, step));
  const maxLabelWidth = axisLabels.reduce(
    (maxW, label) => Math.max(maxW, ctx.measureText(label).width),
    0,
  );
  const paddingLeft = Math.max(68, Math.ceil(maxLabelWidth) + 18);
  const paddingRight = 18;
  const paddingTop = 18;
  const paddingBottom = 30;
  const plotW = w - paddingLeft - paddingRight;
  const plotH = h - paddingTop - paddingBottom;
  if (plotW <= 0 || plotH <= 0) return null;

  const firstTimestamp = points[0].captured_at_ms;
  const lastTimestamp = points[points.length - 1].captured_at_ms;
  const rawTimeRange = lastTimestamp - firstTimestamp;
  const hasTimeSpan = Number.isFinite(rawTimeRange) && rawTimeRange > 0;
  const valSpan = maxVal - minVal;
  const getX = (idx) => {
    if (isSinglePoint) return paddingLeft + plotW / 2;
    return hasTimeSpan
      ? paddingLeft + ((points[idx].captured_at_ms - firstTimestamp) / rawTimeRange) * plotW
      : paddingLeft + (idx / (points.length - 1)) * plotW;
  };
  const getY = (val) => (
    valSpan > 0
      ? paddingTop + plotH - ((val - minVal) / valSpan) * plotH
      : paddingTop + plotH / 2
  );

  const startEquity = points[0].equity;
  const endEquity = points[points.length - 1].equity;
  const isPositive = endEquity >= startEquity;
  const lineColor = isPositive ? '#0a7052' : '#bd2b2b';
  const fillTop = isPositive ? 'rgba(10, 112, 82, 0.14)' : 'rgba(189, 43, 43, 0.13)';
  const fillBottom = isPositive ? 'rgba(10, 112, 82, 0.0)' : 'rgba(189, 43, 43, 0.0)';

  // Horizontal depth gridlines & clean round Y-axis labels
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#e5eeeb';
  ctx.fillStyle = '#526a73';
  ctx.font = axisFont;
  ctx.textAlign = 'right';

  for (let i = 0; i < ticks.length; i++) {
    const y = Math.round(getY(ticks[i])) + 0.5;
    ctx.beginPath();
    ctx.moveTo(paddingLeft, y);
    ctx.lineTo(w - paddingRight, y);
    ctx.stroke();
    ctx.fillText(axisLabels[i], paddingLeft - 10, y + 4);
  }

  // Vertical time guide lines with intermediate X-axis labels (or single-sample time anchor)
  if (isSinglePoint) {
    ctx.save();
    ctx.setLineDash([2, 4]);
    ctx.strokeStyle = '#e5eeeb';
    ctx.fillStyle = '#526a73';
    ctx.font = axisFont;
    ctx.textAlign = 'center';
    const singleX = Math.round(getX(0)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(singleX, paddingTop);
    ctx.lineTo(singleX, paddingTop + plotH);
    ctx.stroke();
    ctx.fillText(formatTickTime(firstTimestamp, 0), singleX, paddingTop + plotH + 18);
    ctx.restore();
  } else if (hasTimeSpan) {
    ctx.save();
    ctx.setLineDash([2, 4]);
    ctx.strokeStyle = '#e5eeeb';
    ctx.fillStyle = '#526a73';
    ctx.font = axisFont;
    ctx.textAlign = 'center';
    const xTickCount = w < 480 ? 2 : 3;
    let lastTickLabel = null;
    for (let i = 1; i <= xTickCount; i++) {
      const ratio = i / (xTickCount + 1);
      const tickX = Math.round(paddingLeft + plotW * ratio) + 0.5;
      ctx.beginPath();
      ctx.moveTo(tickX, paddingTop);
      ctx.lineTo(tickX, paddingTop + plotH);
      ctx.stroke();

      const tickTime = firstTimestamp + rawTimeRange * ratio;
      const tickLabel = formatTickTime(tickTime, rawTimeRange);
      if (tickLabel !== lastTickLabel) {
        ctx.fillText(tickLabel, tickX, paddingTop + plotH + 18);
        lastTickLabel = tickLabel;
      }
    }
    ctx.restore();
  }

  // Period-open baseline dashed rule
  const baseY = Math.round(getY(startEquity)) + 0.5;
  ctx.save();
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = '#8fa5a0';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(paddingLeft, baseY);
  ctx.lineTo(w - paddingRight, baseY);
  ctx.stroke();
  ctx.restore();

  const bottomY = paddingTop + plotH;
  if (!isSinglePoint && maxDdPct > 0.0005 && ddTroughIdx > ddPeakIdx) {
    const ddStartX = getX(ddPeakIdx);
    const ddEndX = getX(ddTroughIdx);
    if (ddEndX - ddStartX >= 2) {
      ctx.save();
      ctx.fillStyle = 'rgba(189, 43, 43, 0.045)';
      ctx.fillRect(ddStartX, paddingTop, ddEndX - ddStartX, plotH);
      ctx.restore();
    }
  }

  if (isSinglePoint) {
    // Place single-point hint cleanly below the center anchor dot so it never collides with top gridline
    ctx.save();
    ctx.fillStyle = '#526a73';
    ctx.font = `500 12px ${CHART_FONT_FAMILY}`;
    ctx.textAlign = 'center';
    const centerX = paddingLeft + plotW / 2;
    const noteY = Math.min(bottomY - 12, paddingTop + plotH * 0.78);
    if (ctx.measureText(SINGLE_POINT_NOTE).width <= plotW - 12) {
      ctx.fillText(SINGLE_POINT_NOTE, centerX, noteY);
    } else {
      const splitIdx = SINGLE_POINT_NOTE.indexOf('（');
      ctx.fillText(SINGLE_POINT_NOTE.slice(0, splitIdx), centerX, noteY - 14);
      ctx.fillText(SINGLE_POINT_NOTE.slice(splitIdx), centerX, noteY + 2);
    }
    ctx.restore();
  } else {
    // Crisp linear financial polyline + gradient wash (zero synthetic bezier loops)
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(points[0].equity));
    for (let i = 1; i < points.length; i++) {
      ctx.lineTo(getX(i), getY(points[i].equity));
    }

    ctx.lineWidth = 2;
    ctx.strokeStyle = lineColor;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.stroke();

    const lastX = getX(points.length - 1);
    const firstX = getX(0);
    ctx.lineTo(lastX, bottomY);
    ctx.lineTo(firstX, bottomY);
    ctx.closePath();

    const grad = ctx.createLinearGradient(0, paddingTop, 0, bottomY);
    grad.addColorStop(0, fillTop);
    grad.addColorStop(1, fillBottom);
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.restore();
  }

  // Latest sample anchor dot
  const curX = getX(points.length - 1);
  const curY = getY(endEquity);
  ctx.beginPath();
  ctx.arc(curX, curY, 3.75, 0, Math.PI * 2);
  ctx.fillStyle = lineColor;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = '#ffffff';
  ctx.stroke();

  // Draw period-open badge AFTER curve/dot, anchored on the left away from curX with crisp pill backdrop
  const tagDecimals = Math.max(2, stepPrecision(step));
  const openTag = `期初 ${formatChartUsd(startEquity, tagDecimals)}`;
  ctx.font = `600 12px ${CHART_FONT_FAMILY}`;
  const openTagWidth = ctx.measureText(openTag).width + 12;
  const tagHeight = 20;
  const openTagX = paddingLeft + 6;
  const preferAbove = baseY - tagHeight - 4 >= paddingTop + 2;
  const openTagY = preferAbove
    ? baseY - tagHeight - 4
    : Math.min(bottomY - tagHeight - 2, baseY + 4);
  const openBox = { x: openTagX, y: openTagY, w: openTagWidth, h: tagHeight };
  drawPillTag(ctx, openTag, openBox);

  const occupiedBoxes = [openBox];
  const hasDistinctExtremes = !isSinglePoint && maxVal > minVal && maxIdx !== minIdx && max > min;
  if (hasDistinctExtremes) {
    const extremeMarkers = [
      {
        idx: maxIdx,
        text: `高 ${formatChartUsd(max, tagDecimals)}`,
        dotColor: '#0a7052',
        border: '#a5dcc8',
        bg: 'rgba(230, 245, 239, 0.95)',
        color: '#0a7052',
        preferAbove: true,
      },
      {
        idx: minIdx,
        text: `低 ${formatChartUsd(min, tagDecimals)}`,
        dotColor: '#bd2b2b',
        border: '#f0b5b5',
        bg: 'rgba(252, 236, 236, 0.95)',
        color: '#bd2b2b',
        preferAbove: false,
      },
    ];

    for (const marker of extremeMarkers) {
      const mx = getX(marker.idx);
      const my = getY(points[marker.idx].equity);
      if (marker.idx !== points.length - 1) {
        ctx.beginPath();
        ctx.arc(mx, my, 3, 0, Math.PI * 2);
        ctx.fillStyle = marker.dotColor;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = '#ffffff';
        ctx.stroke();
      }

      const tagW = ctx.measureText(marker.text).width + 12;
      const minX = paddingLeft + 4;
      const maxX = Math.max(minX, w - paddingRight - tagW - 4);
      const tagX = Math.max(minX, Math.min(maxX, mx - tagW / 2));
      const aboveY = my - tagHeight - 6;
      const belowY = my + 6;
      const primaryY = marker.preferAbove
        ? (aboveY >= paddingTop + 2 ? aboveY : Math.min(bottomY - tagHeight - 2, belowY))
        : (belowY + tagHeight <= bottomY - 2 ? belowY : Math.max(paddingTop + 2, aboveY));
      const altY = primaryY === aboveY
        ? Math.min(bottomY - tagHeight - 2, belowY)
        : Math.max(paddingTop + 2, aboveY);

      let chosenBox = { x: tagX, y: primaryY, w: tagW, h: tagHeight };
      if (occupiedBoxes.some(b => boxesOverlap(chosenBox, b))) {
        const altBox = { x: tagX, y: altY, w: tagW, h: tagHeight };
        if (!occupiedBoxes.some(b => boxesOverlap(altBox, b))) {
          chosenBox = altBox;
        } else {
          chosenBox = null;
        }
      }
      if (chosenBox) {
        drawPillTag(ctx, marker.text, chosenBox, marker);
        occupiedBoxes.push(chosenBox);
      }
    }
  }

  return {
    width: w,
    height: h,
    paddingLeft,
    paddingRight,
    paddingTop,
    plotW,
    plotH,
    hasTimeSpan,
    getX,
    getY,
    startEquity,
    lineColor,
    step,
    minIdx: hasDistinctExtremes ? minIdx : null,
    maxIdx: hasDistinctExtremes ? maxIdx : null,
  };
}

function drawChartFrame(canvas, container, points, hoverIndex, { forceStatic = false } = {}) {
  const rect = container.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = rect.width;
  const h = rect.height;

  const targetW = Math.round(w * dpr);
  const targetH = Math.round(h * dpr);
  if (canvas.width !== targetW || canvas.height !== targetH) {
    canvas.width = targetW;
    canvas.height = targetH;
  }

  const staticNeedsRebuild = forceStatic
    || !staticCacheMeta
    || staticCacheMeta.points !== points
    || staticCacheMeta.w !== w
    || staticCacheMeta.h !== h
    || staticCacheMeta.dpr !== dpr;

  if (staticNeedsRebuild) {
    currentGeometry = buildStaticLayer(points, w, h, dpr);
    staticCacheMeta = { points, w, h, dpr };
  }

  const ctx = canvas.getContext('2d');
  ctx.resetTransform();
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  if (staticCanvas && staticCanvas.width > 0 && staticCanvas.height > 0) {
    ctx.drawImage(staticCanvas, 0, 0, w, h);
  }

  if (!currentGeometry || points.length === 0) {
    return currentGeometry;
  }

  // Interactive crosshair overlay
  if (hoverIndex !== null && hoverIndex >= 0 && hoverIndex < points.length) {
    const { paddingLeft, paddingRight, paddingTop, plotH, getX, getY, lineColor } = currentGeometry;
    const bottomY = paddingTop + plotH;
    const hx = getX(hoverIndex);
    const hy = getY(points[hoverIndex].equity);

    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = '#135d73';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(hx, paddingTop);
    ctx.lineTo(hx, bottomY);
    ctx.stroke();

    ctx.strokeStyle = 'rgba(19, 93, 115, 0.35)';
    ctx.beginPath();
    ctx.moveTo(paddingLeft, hy);
    ctx.lineTo(w - paddingRight, hy);
    ctx.stroke();
    ctx.restore();

    ctx.beginPath();
    ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.lineWidth = 2.25;
    ctx.strokeStyle = lineColor;
    ctx.stroke();
  }

  return currentGeometry;
}

function resolveActivePeriod(explicitPeriod = null) {
  if (explicitPeriod) return explicitPeriod;
  const activeBtn = document.querySelector('.period-btn[aria-pressed="true"]');
  return activeBtn?.dataset?.period || null;
}

function updateStaticChartDom(canvas, points, period, step) {
  const startEl = document.getElementById('chart-start-time');
  const latestEl = document.getElementById('chart-latest-time');
  const indicatorEl = document.getElementById('tooltip-indicator');
  const periodLabel = PERIOD_LABELS[period] || period || '当前周期';

  if (points.length === 0) {
    startEl.textContent = '—';
    startEl.removeAttribute('datetime');
    latestEl.textContent = '—';
    latestEl.removeAttribute('datetime');
    indicatorEl.style.backgroundColor = '#9eb9c0';
    canvas.setAttribute('aria-label', `账户权益走势图（${periodLabel}）：暂无权益采样数据`);
    return;
  }

  const firstTimestamp = points[0].captured_at_ms;
  const lastTimestamp = points[points.length - 1].captured_at_ms;
  startEl.textContent = formatBeijingDateHm(firstTimestamp);
  startEl.dateTime = new Date(firstTimestamp).toISOString();
  latestEl.textContent = formatBeijingDateHm(lastTimestamp);
  latestEl.dateTime = new Date(lastTimestamp).toISOString();

  const startEquity = points[0].equity;
  const endEquity = points[points.length - 1].equity;
  const isPositive = endEquity >= startEquity;
  indicatorEl.style.backgroundColor = isPositive ? '#34d399' : '#f87171';

  const decimals = Math.max(2, stepPrecision(step || 1));
  if (points.length === 1) {
    const valStr = formatChartUsd(startEquity, decimals);
    canvas.setAttribute(
      'aria-label',
      `账户权益走势图（${periodLabel}）：${SINGLE_POINT_NOTE}，当前权益 ${valStr}。聚焦后可使用方向键查看采样点详情。`,
    );
    return;
  }

  const diff = endEquity - startEquity;
  const sign = diff > 0 ? '+' : diff < 0 ? '-' : '';
  const pctStr = startEquity !== 0
    ? `（${sign}${Math.abs((diff / startEquity) * 100).toFixed(2)}%）`
    : '';
  canvas.setAttribute(
    'aria-label',
    `账户权益走势图（${periodLabel}）：共 ${points.length} 个采样点，期初 ${formatChartUsd(startEquity, decimals)}，最新 ${formatChartUsd(endEquity, decimals)}，较期初 ${formatChartUsd(diff, decimals, { signed: true })}${pctStr}。聚焦后可使用左右方向键逐点查看。`,
  );
}

function hideTooltip() {
  lastTooltipIndex = null;
  document.getElementById('chart-tooltip').classList.add('hidden');
}

function updateChartSrReadout(text) {
  const srEl = document.getElementById('chart-sr-status');
  if (srEl) {
    srEl.textContent = text;
  }
}

function updateTooltip(rect, geometry, points, index, { announceSr = false } = {}) {
  const tooltip = document.getElementById('chart-tooltip');
  const alreadyShown = lastTooltipIndex === index && !tooltip.classList.contains('hidden');

  const tooltipTime = document.getElementById('tooltip-time');
  const tooltipVal = document.getElementById('tooltip-val');
  const tooltipDelta = document.getElementById('tooltip-delta');

  const p = points[index];
  const { getX, getY, startEquity, step, minIdx, maxIdx } = geometry;
  const targetX = getX(index);
  const targetY = getY(p.equity);

  const extremeTag = index === maxIdx ? ' · 区间高点' : index === minIdx ? ' · 区间低点' : '';
  const timeText = `${formatBeijingShortDateTime(p.captured_at_ms)}${extremeTag}`;
  const valueDecimals = Math.max(2, stepPrecision(step));
  const valText = formatChartUsd(p.equity, valueDecimals);

  const diff = p.equity - startEquity;
  const sign = diff > 0 ? '+' : diff < 0 ? '-' : '';
  const pct = startEquity !== 0 ? ` (${sign}${Math.abs((diff / startEquity) * 100).toFixed(2)}%)` : '';
  const deltaText = `较期初 ${formatChartUsd(diff, valueDecimals, { signed: true })}${pct}`;

  if (!alreadyShown) {
    tooltipTime.textContent = timeText;
    tooltipVal.textContent = valText;
    tooltipDelta.textContent = deltaText;
    tooltipDelta.style.color = diff > 0 ? '#6ee7b7' : diff < 0 ? '#fca5a5' : '#9eb9c0';

    tooltip.classList.remove('hidden');
    const tooltipWidth = tooltip.offsetWidth || 160;
    const tooltipHeight = tooltip.offsetHeight || 68;
    const margin = 8;

    const rawLeft = targetX + 14 + tooltipWidth > rect.width - margin
      ? targetX - tooltipWidth - 14
      : targetX + 14;
    const leftPos = Math.max(margin, Math.min(Math.max(margin, rect.width - tooltipWidth - margin), rawLeft));

    const rawTop = targetY - tooltipHeight - 12 < margin
      ? targetY + 14
      : targetY - tooltipHeight - 12;
    const topPos = Math.max(margin, Math.min(Math.max(margin, rect.height - tooltipHeight - margin), rawTop));

    tooltip.style.left = `${leftPos}px`;
    tooltip.style.top = `${topPos}px`;
    lastTooltipIndex = index;
  }

  if (announceSr) {
    updateChartSrReadout(`第 ${index + 1}/${points.length} 点，${timeText}，权益 ${valText}，${deltaText}`);
  }
}

function cancelHoverFrame() {
  if (hoverRafId !== null) {
    cancelAnimationFrame(hoverRafId);
    hoverRafId = null;
  }
  pendingClientPos = null;
  pendingKeyIndex = null;
}

function clearHoverState(canvas, container) {
  hideTooltip();
  updateChartSrReadout('');
  if (activeHoverIndex !== null) {
    activeHoverIndex = null;
    if (lastRenderedPoints.length > 0) {
      currentGeometry = drawChartFrame(canvas, container, lastRenderedPoints, null);
    }
  }
}

function resolveIndexAtPointer(points, geometry, rect, clientX, clientY, pointerType) {
  const mouseX = clientX - rect.left;
  const mouseY = clientY - rect.top;
  const { paddingLeft, paddingRight, paddingTop, plotW, plotH, hasTimeSpan } = geometry;

  if (mouseX < paddingLeft || mouseX > rect.width - paddingRight) {
    return null;
  }
  if (pointerType === 'touch' && (mouseY < paddingTop - 12 || mouseY > paddingTop + plotH + 12)) {
    return null;
  }

  const ratio = Math.min(1, Math.max(0, (mouseX - paddingLeft) / plotW));
  if (!hasTimeSpan) {
    return Math.round(ratio * (points.length - 1));
  }

  const firstTimestamp = points[0].captured_at_ms;
  const lastTimestamp = points[points.length - 1].captured_at_ms;
  const targetTimestamp = firstTimestamp + ratio * (lastTimestamp - firstTimestamp);

  let low = 0;
  let high = points.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (points[middle].captured_at_ms < targetTimestamp) low = middle + 1;
    else high = middle;
  }
  const right = low;
  const left = Math.max(0, right - 1);
  return Math.abs(points[right].captured_at_ms - targetTimestamp) <
    Math.abs(points[left].captured_at_ms - targetTimestamp) ? right : left;
}

function scheduleHoverFrame(canvas, container) {
  if (hoverRafId !== null) return;
  hoverRafId = requestAnimationFrame(() => {
    hoverRafId = null;
    if (!lastRenderedPoints || lastRenderedPoints.length === 0 || !currentGeometry) {
      pendingClientPos = null;
      pendingKeyIndex = null;
      clearHoverState(canvas, container);
      return;
    }

    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      pendingClientPos = null;
      pendingKeyIndex = null;
      return;
    }

    let nextIndex = null;
    let announceSr = false;
    if (pendingClientPos !== null) {
      const { clientX, clientY, pointerType } = pendingClientPos;
      pendingClientPos = null;
      nextIndex = resolveIndexAtPointer(
        lastRenderedPoints,
        currentGeometry,
        rect,
        clientX,
        clientY,
        pointerType,
      );
      if (nextIndex === null) {
        clearHoverState(canvas, container);
        return;
      }
    } else if (pendingKeyIndex !== null) {
      nextIndex = Math.max(0, Math.min(lastRenderedPoints.length - 1, pendingKeyIndex));
      pendingKeyIndex = null;
      announceSr = true;
    } else {
      return;
    }

    if (activeHoverIndex !== nextIndex) {
      activeHoverIndex = nextIndex;
      currentGeometry = drawChartFrame(canvas, container, lastRenderedPoints, activeHoverIndex) || currentGeometry;
    }
    updateTooltip(rect, currentGeometry, lastRenderedPoints, activeHoverIndex, { announceSr });
  });
}

function bindCanvasInteractions(canvas, container) {
  if (boundCanvas === canvas) return;
  boundCanvas = canvas;

  function handlePointerUpdate(e) {
    if (!lastRenderedPoints || lastRenderedPoints.length === 0 || !currentGeometry) return;
    pendingClientPos = { clientX: e.clientX, clientY: e.clientY, pointerType: e.pointerType };
    pendingKeyIndex = null;
    scheduleHoverFrame(canvas, container);
  }

  canvas.addEventListener('pointermove', handlePointerUpdate);
  canvas.addEventListener('pointerdown', handlePointerUpdate);
  canvas.addEventListener('pointercancel', () => {
    cancelHoverFrame();
    clearHoverState(canvas, container);
  });
  canvas.addEventListener('pointerleave', (e) => {
    if (e.pointerType === 'touch') return;
    cancelHoverFrame();
    clearHoverState(canvas, container);
  });
  canvas.addEventListener('blur', () => {
    cancelHoverFrame();
    clearHoverState(canvas, container);
  });

  if (boundOutsideHandler) {
    document.removeEventListener('pointerdown', boundOutsideHandler);
  }
  boundOutsideHandler = (e) => {
    if (activeHoverIndex !== null && !container.contains(e.target)) {
      cancelHoverFrame();
      clearHoverState(canvas, container);
    }
  };
  document.addEventListener('pointerdown', boundOutsideHandler);

  canvas.addEventListener('keydown', (e) => {
    const n = lastRenderedPoints.length;
    if (n === 0 || !currentGeometry) {
      if (e.key === 'Escape') {
        cancelHoverFrame();
        clearHoverState(canvas, container);
      }
      return;
    }

    let nextIndex = null;
    if (e.key === 'ArrowRight') {
      nextIndex = activeHoverIndex === null ? 0 : Math.min(n - 1, activeHoverIndex + 1);
    } else if (e.key === 'ArrowLeft') {
      nextIndex = activeHoverIndex === null ? n - 1 : Math.max(0, activeHoverIndex - 1);
    } else if (e.key === 'Home') {
      nextIndex = 0;
    } else if (e.key === 'End') {
      nextIndex = n - 1;
    } else if (e.key === 'Escape') {
      if (activeHoverIndex !== null) {
        e.preventDefault();
        cancelHoverFrame();
        clearHoverState(canvas, container);
      }
      return;
    } else {
      return;
    }

    e.preventDefault();
    pendingClientPos = null;
    pendingKeyIndex = nextIndex;
    scheduleHoverFrame(canvas, container);
  });
}

export function setChartVisualState(mode, message = '') {
  const container = document.getElementById('canvas-container');
  const overlay = document.getElementById('chart-empty-overlay');
  if (!container || !overlay) return;

  container.classList.toggle('opacity-60', mode === 'switching');
  if (mode === 'loading' || mode === 'error') {
    overlay.textContent = message;
    overlay.className = `absolute inset-0 flex items-center justify-center text-xs px-4 text-center pointer-events-none ${
      mode === 'error' ? 'text-[#bd2b2b] font-medium' : 'text-[#526a73]'
    }`;
    overlay.classList.remove('hidden');
  } else {
    overlay.classList.add('hidden');
  }
}

export function renderRetinaChart(points, period = null) {
  const canvas = document.getElementById('equity-canvas');
  const container = document.getElementById('canvas-container');
  const safePoints = sanitizePoints(points);
  const resolvedPeriod = resolveActivePeriod(period);

  cancelHoverFrame();
  bindCanvasInteractions(canvas, container);
  setChartVisualState('ready');

  if (lastRenderedPeriod !== null && resolvedPeriod !== lastRenderedPeriod) {
    activeHoverIndex = null;
  }
  lastRenderedPeriod = resolvedPeriod;
  lastRenderedPoints = safePoints;
  lastTooltipIndex = null;

  if (
    activeHoverIndex === null ||
    safePoints.length === 0 ||
    activeHoverIndex < 0 ||
    activeHoverIndex >= safePoints.length
  ) {
    activeHoverIndex = null;
  }

  currentGeometry = drawChartFrame(canvas, container, safePoints, activeHoverIndex, { forceStatic: true });
  updateStaticChartDom(canvas, safePoints, resolvedPeriod, currentGeometry?.step ?? 1);

  if (currentGeometry && activeHoverIndex !== null) {
    updateTooltip(
      { width: currentGeometry.width, height: currentGeometry.height },
      currentGeometry,
      safePoints,
      activeHoverIndex,
    );
  } else {
    hideTooltip();
  }
}

export function initChartResizeObserver(containerId, getPoints) {
  const containerEl = document.getElementById(containerId);
  let frameId = null;
  const observer = new ResizeObserver(() => {
    if (frameId !== null) {
      cancelAnimationFrame(frameId);
    }
    frameId = requestAnimationFrame(() => {
      frameId = null;
      renderRetinaChart(getPoints());
    });
  });
  observer.observe(containerEl);
  return {
    disconnect() {
      if (frameId !== null) {
        cancelAnimationFrame(frameId);
        frameId = null;
      }
      observer.disconnect();
    },
  };
}
