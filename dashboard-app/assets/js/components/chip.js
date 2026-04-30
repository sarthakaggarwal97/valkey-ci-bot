/** Status chip with automatic tone detection. */
import { el } from '../dom.js';
import { safeStr, toneForStatus } from '../utils.js';

export function chip(label, { tone } = {}) {
  const text = safeStr(label) || 'unknown';
  const resolvedTone = tone || toneForStatus(text);
  return el('span', { class: 'chip chip-' + resolvedTone }, [text]);
}
