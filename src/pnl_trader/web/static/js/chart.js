// Dynamic High-Resolution Retina HTML5 Canvas Chart with Crosshair

export function renderRetinaChart(points) {
  const canvas = document.getElementById('equity-canvas');
  const container = document.getElementById('canvas-container');
  if (!canvas || !container) return;

  const rect = container.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return;
  const dpr = window.devicePixelRatio || 1;

  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;

  const ctx = canvas.getContext('2d');
  ctx.resetTransform();
  ctx.scale(dpr, dpr);

  const w = rect.width;
  const h = rect.height;

  ctx.clearRect(0, 0, w, h);

  if (!points || points.length < 2) {
    ctx.fillStyle = '#64748b';
    ctx.font = '13px -apple-system, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('暂无足够权益采样数据', w / 2, h / 2);
    const startEl = document.getElementById('chart-start-time');
    const latestEl = document.getElementById('chart-latest-time');
    const tooltip = document.getElementById('chart-tooltip');
    if (startEl) startEl.textContent = '—';
    if (latestEl) latestEl.textContent = '—';
    if (tooltip) tooltip.classList.add('hidden');
    canvas.onpointermove = null;
    canvas.onpointerleave = null;
    canvas.onpointerdown = null;
    canvas.onpointerup = null;
    return;
  }

  const paddingLeft = 65;
  const paddingRight = 20;
  const paddingTop = 20;
  const paddingBottom = 30;
  const plotW = w - paddingLeft - paddingRight;
  const plotH = h - paddingTop - paddingBottom;
  if (plotW <= 0 || plotH <= 0) return;

  const values = points.map(p => p.equity);
  let minVal = Math.min(...values);
  let maxVal = Math.max(...values);
  if (minVal === maxVal) {
    const padding = Math.max(Math.abs(minVal) * 0.05, 1);
    minVal -= padding;
    maxVal += padding;
  }
  const valRange = maxVal - minVal;
  minVal -= valRange * 0.08;
  maxVal += valRange * 0.08;

  const firstTimestamp = points[0].captured_at_ms;
  const timeRange = points[points.length - 1].captured_at_ms - firstTimestamp;
  const getX = (idx) =>
    paddingLeft + ((points[idx].captured_at_ms - firstTimestamp) / timeRange) * plotW;
  const getY = (val) => paddingTop + plotH - ((val - minVal) / (maxVal - minVal)) * plotH;

  const isPositive = points[points.length - 1].equity >= points[0].equity;
  const themeColor = isPositive ? '#10b981' : '#f43f5e';
  const shadowColor = isPositive ? 'rgba(16, 185, 129, 0.4)' : 'rgba(244, 63, 94, 0.4)';
  const gradColorTop = isPositive ? 'rgba(16, 185, 129, 0.22)' : 'rgba(244, 63, 94, 0.22)';
  const gradColorBottom = isPositive ? 'rgba(16, 185, 129, 0.0)' : 'rgba(244, 63, 94, 0.0)';

  const tooltipIndicator = document.getElementById('tooltip-indicator');
  if (tooltipIndicator) {
    tooltipIndicator.className = `w-2 h-2 rounded-full ${isPositive ? 'bg-emerald-400' : 'bg-rose-400'}`;
  }

  // Draw horizontal grid lines & labels
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(30, 41, 59, 0.7)';
  ctx.fillStyle = '#64748b';
  ctx.font = '11px ui-monospace, monospace';
  ctx.textAlign = 'right';

  const gridTicks = 4;
  for (let i = 0; i <= gridTicks; i++) {
    const val = minVal + (maxVal - minVal) * (i / gridTicks);
    const y = getY(val);

    ctx.beginPath();
    ctx.moveTo(paddingLeft, y);
    ctx.lineTo(w - paddingRight, y);
    ctx.stroke();

    ctx.fillText('$' + val.toFixed(0), paddingLeft - 10, y + 4);
  }

  // Draw vertical time grid lines (3 ticks)
  ctx.save();
  ctx.setLineDash([2, 4]);
  ctx.strokeStyle = 'rgba(30, 41, 59, 0.5)';
  for (let i = 1; i <= 3; i++) {
    const tickX = paddingLeft + (plotW * i) / 4;
    ctx.beginPath();
    ctx.moveTo(tickX, paddingTop);
    ctx.lineTo(tickX, paddingTop + plotH);
    ctx.stroke();
  }
  ctx.restore();

  // Smooth Curve using Bezier
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(getX(0), getY(points[0].equity));

  for (let i = 0; i < points.length - 1; i++) {
    const x0 = getX(i);
    const y0 = getY(points[i].equity);
    const x1 = getX(i + 1);
    const y1 = getY(points[i + 1].equity);
    const mx = (x0 + x1) / 2;
    ctx.bezierCurveTo(mx, y0, mx, y1, x1, y1);
  }

  // Stroke Line
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = themeColor;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.shadowColor = shadowColor;
  ctx.shadowBlur = 8;
  ctx.stroke();

  // Area Fill
  const lastX = getX(points.length - 1);
  const firstX = getX(0);
  const bottomY = paddingTop + plotH;
  ctx.lineTo(lastX, bottomY);
  ctx.lineTo(firstX, bottomY);
  ctx.closePath();

  const grad = ctx.createLinearGradient(0, paddingTop, 0, bottomY);
  grad.addColorStop(0, gradColorTop);
  grad.addColorStop(1, gradColorBottom);
  ctx.fillStyle = grad;
  ctx.shadowBlur = 0;
  ctx.fill();
  ctx.restore();

  // Current Point Pulsing Indicator
  const curX = getX(points.length - 1);
  const curY = getY(points[points.length - 1].equity);
  ctx.beginPath();
  ctx.arc(curX, curY, 4.5, 0, Math.PI * 2);
  ctx.fillStyle = themeColor;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = '#ffffff';
  ctx.stroke();

  // Update range times
  const d0 = new Date(points[0].captured_at_ms);
  const d1 = new Date(points[points.length - 1].captured_at_ms);
  const startEl = document.getElementById('chart-start-time');
  const latestEl = document.getElementById('chart-latest-time');
  if (startEl) startEl.textContent = `${d0.toLocaleDateString('zh-CN')} ${d0.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`;
  if (latestEl) latestEl.textContent = `${d1.toLocaleDateString('zh-CN')} ${d1.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`;

  // Attach crosshair listener with Pointer Events (Supports Touch, Pen and Mouse)
  setupCanvasHover(canvas, points, getX, getY);
}

export function setupCanvasHover(canvas, points, getX, getY) {
  const tooltip = document.getElementById('chart-tooltip');
  if (!tooltip) return;

  function handlePointerMove(e) {
    const rect = canvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const paddingLeft = 65;
    const paddingRight = 20;
    const plotW = rect.width - paddingLeft - paddingRight;

    if (mouseX < paddingLeft || mouseX > rect.width - paddingRight) {
      tooltip.classList.add('hidden');
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
    const p = points[index];

    const targetX = getX(index);
    const targetY = getY(p.equity);

    tooltip.classList.remove('hidden');
    tooltip.style.left = `${Math.min(rect.width - 150, Math.max(10, targetX - 50))}px`;

    if (targetY < 65) {
      tooltip.style.top = `${targetY + 16}px`;
    } else {
      tooltip.style.top = `${targetY - 55}px`;
    }

    const tooltipTime = document.getElementById('tooltip-time');
    const tooltipVal = document.getElementById('tooltip-val');
    if (tooltipTime) tooltipTime.textContent = new Date(p.captured_at_ms).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    if (tooltipVal) tooltipVal.textContent = `$${p.equity.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
  }

  function handlePointerLeave() {
    tooltip.classList.add('hidden');
  }

  canvas.onpointermove = handlePointerMove;
  canvas.onpointerleave = handlePointerLeave;
  canvas.onpointerdown = handlePointerMove;
  canvas.onpointerup = handlePointerLeave;
}

export function initChartResizeObserver(containerId, getPoints) {
  const containerEl = document.getElementById(containerId);
  if (!containerEl) return;
  const observer = new ResizeObserver(() => {
    renderRetinaChart(getPoints());
  });
  observer.observe(containerEl);
  return observer;
}
