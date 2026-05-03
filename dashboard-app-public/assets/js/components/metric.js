/** Metric card: label / value / optional note with tone. */
import { el } from '../dom.js';
import { formatNumber } from '../utils.js';

export function metric(label, value, { note = '', tone = 'accent' } = {}) {
  return el('article', { class: 'metric metric-' + tone }, [
    el('p', { class: 'metric-label' }, [label]),
    el('strong', { class: 'metric-value' }, [formatNumber(value)]),
    note ? el('span', { class: 'metric-note' }, [note]) : null,
  ]);
}

export function metricGrid(metrics) {
  return el('section', { class: 'hero-metrics' }, metrics.filter(Boolean));
}
