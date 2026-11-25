// High-Resolution Retina HTML5 Canvas Financial Equity Chart with Interactive Crosshair

let activeHoverIndex = null;
let lastRenderedPoints = [];

function computeNiceScale(rawMin, rawMax, targetTicks = 4) {
  let min = rawMin;
  let max = rawMax;
  if (min === max) {
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

  const ticks = [];
  for (let v = niceMin; v <= niceMax + step * 0.001; v += step) {
    ticks.push(Number(v.toFixed(6)));
  }
  return { minVal: niceMin, maxVal: niceMax, ticks };
}

function formatAxisLabel(val) {
  if (Math.abs(val) >= 100) {
    return `$${Math.round(val).toLocaleString('en-US')}`;
  }
  return `$${val.toFixed(1)}`;
}

function formatTickTime(ms, spanMs) {
  const d = new Date(ms);
  if (spanMs <= 36 * 3_600_000) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:00`;
}

function drawChartFrame(canvas, container, points, hoverIndex) {
  const rect = container.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const dpr = window.devicePixelRatio || 1;

  const targetW = Math.round(rect.width * dpr);
  const targetH = Math.round(rect.height * dpr);
  if (canvas.width !== targetW || canvas.height !== targetH) {
    canvas.width = targetW;
    canvas.height = targetH;
  }

  const ctx = canvas.getContext('2d');
  ctx.resetTransform();
  ctx.scale(dpr, dpr);

  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);

  if (points.length < 2) {
    ctx.fillStyle = '#526a73';
    ctx.font = '500 13px "Barlow", -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('暂无足够权益采样数据', w / 2, h / 2);
    document.getElementById('chart-start-time').textContent = '—';
    document.getElementById('chart-latest-time').textContent = '—';
    document.getElementById('chart-tooltip').classList.add('hidden');
    return null;
  }

  const paddingLeft = 68;
  const paddingRight = 18;
  const paddingTop = 18;
  const paddingBottom = 30;
  const plotW = w - paddingLeft - paddingRight;
  const plotH = h - paddingTop - paddingBottom;
  if (plotW <= 0 || plotH <= 0) return null;

  const values = points.map(p => p.equity);
  const { minVal, maxVal, ticks } = computeNiceScale(Math.min(...values), Math.max(...values), 4);

  const firstTimestamp = points[0].captured_at_ms;
  const lastTimestamp = points[points.length - 1].captured_at_ms;
  const timeRange = Math.max(1, lastTimestamp - firstTimestamp);
  const getX = (idx) =>
    paddingLeft + ((points[idx].captured_at_ms - firstTimestamp) / timeRange) * plotW;
  const getY = (val) => paddingTop + plotH - ((val - minVal) / (maxVal - minVal)) * plotH;

  const startEquity = points[0].equity;
  const endEquity = points[points.length - 1].equity;
  const isPositive = endEquity >= startEquity;
  const lineColor = isPositive ? '#0a7052' : '#bd2b2b';
  const fillTop = isPositive ? 'rgba(10, 112, 82, 0.14)' : 'rgba(189, 43, 43, 0.13)';
  const fillBottom = isPositive ? 'rgba(10, 112, 82, 0.0)' : 'rgba(189, 43, 43, 0.0)';

  document.getElementById('tooltip-indicator').style.backgroundColor = isPositive ? '#34d399' : '#f87171';

  // Horizontal depth gridlines & clean round Y-axis labels
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#e5eeeb';
  ctx.fillStyle = '#526a73';
  ctx.font = '500 11px "Barlow", ui-monospace, sans-serif';
  ctx.textAlign = 'right';

  for (const val of ticks) {
    const y = Math.round(getY(val)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(paddingLeft, y);
    ctx.lineTo(w - paddingRight, y);
    ctx.stroke();
    ctx.fillText(formatAxisLabel(val), paddingLeft - 10, y + 3.5);
  }

  // Vertical time guide lines with intermediate X-axis labels
  ctx.save();
  ctx.setLineDash([2, 4]);
  ctx.strokeStyle = '#e5eeeb';
  ctx.fillStyle = '#526a73';
  ctx.font = '500 11px "Barlow", ui-monospace, sans-serif';
  ctx.textAlign = 'center';
  const xTickCount = w < 480 ? 2 : 3;
  for (let i = 1; i <= xTickCount; i++) {
    const ratio = i / (xTickCount + 1);
    const tickX = Math.round(paddingLeft + plotW * ratio) + 0.5;
    ctx.beginPath();
    ctx.moveTo(tickX, paddingTop);
    ctx.lineTo(tickX, paddingTop + plotH);
    ctx.stroke();

    const tickTime = firstTimestamp + timeRange * ratio;
    ctx.fillText(formatTickTime(tickTime, timeRange), tickX, paddingTop + plotH + 18);
  }
  ctx.restore();

  // Period-open baseline rule with subtle inline value tag
  const baseY = Math.round(getY(startEquity)) + 0.5;
  ctx.save();
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = '#8fa5a0';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(paddingLeft, baseY);
  ctx.lineTo(w - paddingRight, baseY);
  ctx.stroke();

  const openTag = `期初 ${formatAxisLabel(startEquity)}`;
  ctx.font = '600 10.5px "Barlow", "PingFang SC", sans-serif';
  const tagWidth = ctx.measureText(openTag).width + 10;
  const tagX = w - paddingRight - tagWidth;
  const tagY = Math.max(paddingTop + 2, Math.min(paddingTop + plotH - 18, baseY - 17));
  ctx.setLineDash([]);
  ctx.fillStyle = 'rgba(243, 246, 245, 0.92)';
  ctx.fillRect(tagX, tagY, tagWidth, 16);
  ctx.fillStyle = '#3f5760';
  ctx.textAlign = 'right';
  ctx.fillText(openTag, w - paddingRight - 5, tagY + 11.5);
  ctx.restore();

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
  const bottomY = paddingTop + plotH;
  ctx.lineTo(lastX, bottomY);
  ctx.lineTo(firstX, bottomY);
  ctx.closePath();

  const grad = ctx.createLinearGradient(0, paddingTop, 0, bottomY);
  grad.addColorStop(0, fillTop);
  grad.addColorStop(1, fillBottom);
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.restore();

  // Interactive crosshair
  if (hoverIndex !== null && hoverIndex >= 0 && hoverIndex < points.length) {
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

  const d0 = new Date(firstTimestamp);
  const d1 = new Date(lastTimestamp);
  document.getElementById('chart-start-time').textContent =
    `${d0.toLocaleDateString('zh-CN')} ${d0.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`;
  document.getElementById('chart-latest-time').textContent =
    `${d1.toLocaleDateString('zh-CN')} ${d1.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`;

  return { paddingLeft, paddingRight, plotW, getX, getY, startEquity };
}

export function renderRetinaChart(points) {
  const canvas = document.getElementById('equity-canvas');
  const container = document.getElementById('canvas-container');
  lastRenderedPoints = points;
  activeHoverIndex = null;

  const geometry = drawChartFrame(canvas, container, points, null);
  if (!geometry) {
    canvas.onpointermove = null;
    canvas.onpointerleave = null;
    canvas.onpointerdown = null;
    canvas.onpointerup = null;
    return;
  }

  setupCanvasHover(canvas, container, points, geometry);
}

function setupCanvasHover(canvas, container, points, geometry) {
  const tooltip = document.getElementById('chart-tooltip');
  const tooltipTime = document.getElementById('tooltip-time');
  const tooltipVal = document.getElementById('tooltip-val');
  const tooltipDelta = document.getElementById('tooltip-delta');

  function handlePointerMove(e) {
    const rect = canvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const { paddingLeft, paddingRight, plotW, getX, getY, startEquity } = geometry;

    if (mouseX < paddingLeft || mouseX > rect.width - paddingRight) {
      handlePointerLeave();
      return;
    }

    const ratio = (mouseX - paddingLeft) / plotW;
    const targetTimestamp = points[0].captured_at_ms + ratio *
      (points[points.length - 1].captured_at_ms - points[0].captured_at_ms);

    let low = 0;
    let high = points.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (points[middle].captured_at_ms < targetTimestamp) low = middle + 1;
      else high = middle;
    }
    const right = low;
    const left = Math.max(0, right - 1);
    const index = Math.abs(points[right].captured_at_ms - targetTimestamp) <
      Math.abs(points[left].captured_at_ms - targetTimestamp) ? right : left;

    if (activeHoverIndex !== index) {
      activeHoverIndex = index;
      drawChartFrame(canvas, container, lastRenderedPoints, activeHoverIndex);
    }

    const p = points[index];
    const targetX = getX(index);
    const targetY = getY(p.equity);

    tooltip.classList.remove('hidden');
    const tooltipWidth = tooltip.offsetWidth || 160;
    const leftPos = targetX + 14 + tooltipWidth > rect.width
      ? Math.max(8, targetX - tooltipWidth - 14)
      : Math.max(8, targetX + 14);
    const topPos = targetY < 72 ? targetY + 14 : targetY - 64;
    tooltip.style.left = `${leftPos}px`;
    tooltip.style.top = `${topPos}px`;

    tooltipTime.textContent = new Date(p.captured_at_ms).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
    tooltipVal.textContent = `$${p.equity.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;

    const diff = p.equity - startEquity;
    const sign = diff > 0 ? '+' : diff < 0 ? '-' : '';
    const pct = startEquity !== 0 ? ` (${sign}${Math.abs((diff / startEquity) * 100).toFixed(2)}%)` : '';
    tooltipDelta.textContent = `较期初 ${sign}$${Math.abs(diff).toFixed(2)}${pct}`;
    tooltipDelta.style.color = diff > 0 ? '#6ee7b7' : diff < 0 ? '#fca5a5' : '#9eb9c0';
  }

  function handlePointerLeave() {
    if (activeHoverIndex !== null) {
      activeHoverIndex = null;
      drawChartFrame(canvas, container, lastRenderedPoints, null);
    }
    tooltip.classList.add('hidden');
  }

  canvas.onpointermove = handlePointerMove;
  canvas.onpointerleave = handlePointerLeave;
  canvas.onpointerdown = handlePointerMove;
  canvas.onpointerup = handlePointerLeave;
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
  return observer;
}
