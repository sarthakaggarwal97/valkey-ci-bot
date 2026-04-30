/** Stat grid: key-value pair list rendered as a grid. */
import { el } from '../dom.js';

export function statGrid(rows) {
  // rows: [{ label: string, value: Node | string }]
  return el(
    'div',
    { class: 'summary-grid' },
    rows.map(({ label, value }) =>
      el('div', { class: 'summary-cell' }, [
        el('span', {}, [label]),
        el('strong', {}, [value instanceof Node ? value : String(value ?? '')]),
      ])
    )
  );
}
