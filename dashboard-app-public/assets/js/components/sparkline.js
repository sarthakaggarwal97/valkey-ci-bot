/**
 * Sparkline SVG with hover tooltips.
 *
 * values: [number, number, ...]
 * labels: optional array of same length used for tooltip "X-axis" labels
 * options: { color, width, height, percent? }
 */
import { el } from '../dom.js';
import { safeList, safeStr } from '../utils.js';

const NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    node.setAttribute(k, String(v));
  }
  for (const child of children) {
    if (child == null) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function sparkline(values, { color = 'var(--accent)', width = 220, height = 56, labels = [], percent = false } = {}) {
  const vals = safeList(values).map((v) => Number(v) || 0);
  if (!vals.length) {
    return el('p', { class: 'empty-inline' }, ['Not enough history.']);
  }
  if (vals.length === 1) vals.push(vals[0]);

  const minimum = Math.min(...vals);
  const maximum = Math.max(...vals);
  const spread = Math.max(maximum - minimum, 0.0001);
  const step = width / Math.max(vals.length - 1, 1);
  const points = vals.map((v, i) => {
    const x = Math.round(i * step * 100) / 100;
    const y = Math.round((height - ((v - minimum) / spread) * (height - 12) - 6) * 100) / 100;
    return [x, y];
  });

  const linePoints = points.map(([x, y]) => x + ',' + y).join(' ');
  const areaPoints = '0,' + height + ' ' + linePoints + ' ' + width + ',' + height;

  const fmt = (v) => percent ? (Math.round(v * 100) + '%') : String(v);

  const wrap = el('div', { class: 'sparkline-wrap' }, []);
  const tooltip = el('div', { class: 'sparkline-tooltip', hidden: true, role: 'status' }, []);

  const circles = points.map(([x, y], i) => {
    const label = labels[i] ? String(labels[i]) : String(i + 1);
    const dot = svgEl('circle', {
      cx: x, cy: y, r: 4,
      fill: color,
      'data-idx': i,
      'aria-label': label + ': ' + fmt(vals[i]),
      tabindex: 0,
      class: 'sparkline-dot',
    });
    const show = () => {
      tooltip.replaceChildren(
        el('strong', {}, [fmt(vals[i])]),
        labels[i] ? el('span', {}, [' \u00B7 ' + labels[i]]) : null,
      );
      tooltip.hidden = false;
      tooltip.style.left = (x / width * 100) + '%';
    };
    const hide = () => { tooltip.hidden = true; };
    dot.addEventListener('mouseenter', show);
    dot.addEventListener('mouseleave', hide);
    dot.addEventListener('focus', show);
    dot.addEventListener('blur', hide);
    return dot;
  });

  const svg = svgEl('svg', {
    class: 'sparkline',
    viewBox: '0 0 ' + width + ' ' + height,
    preserveAspectRatio: 'none',
    role: 'img',
    'aria-label': 'Trend sparkline with ' + vals.length + ' points. Range: ' + fmt(minimum) + ' to ' + fmt(maximum) + '.',
  }, [
    svgEl('polygon', { points: areaPoints, fill: color, opacity: 0.14 }),
    svgEl('polyline', {
      points: linePoints,
      fill: 'none',
      stroke: color,
      'stroke-width': 2.5,
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
    }),
    ...circles,
  ]);

  wrap.appendChild(svg);
  wrap.appendChild(tooltip);
  return wrap;
}

export function trendCard({ title, values, labels = [], note = '', color = 'var(--accent)', percent = false }) {
  return el('article', { class: 'trend-card' }, [
    el('h3', { class: 'trend-card-title' }, [title]),
    sparkline(values, { color, labels, percent }),
    note ? el('p', { class: 'trend-card-note' }, [note]) : null,
  ]);
}
