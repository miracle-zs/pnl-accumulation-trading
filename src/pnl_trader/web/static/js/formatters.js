// Number, Price, and Currency Formatting Utilities

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

export function createNode(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

export function showTableMessage(container, columns, className, message) {
  const cell = createNode('td', className, message);
  cell.colSpan = columns;
  const row = document.createElement('tr');
  row.append(cell);
  container.replaceChildren(row);
}

export function showMessage(container, className, message) {
  container.replaceChildren(createNode('div', className, message));
}

export function announce(message) {
  const el = document.getElementById('sr-announcer');
  if (el) el.textContent = message;
}
