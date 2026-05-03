/**
 * Sortable + filterable table.
 *
 * columns: [{ key, label, sortable?, sortValue?, render? }]
 *   - key: stable identifier for the column, used for URL state
 *   - label: text shown in header
 *   - sortable: default true
 *   - sortValue(row): returns the raw value used for sorting (defaults to row[key])
 *   - render(row): returns a Node or string (the cell body). Defaults to row[key] as text.
 *
 * rows: array of plain objects
 *
 * options:
 *   - filter: { enabled, placeholder, getHaystack(row) -> string }
 *   - empty: message to show when no rows
 *   - id: optional DOM id (for URL state)
 *   - defaultSort: { key, direction: 'asc'|'desc' }
 *   - onRowClick(row): handler for keyboard/mouse row interaction
 */
import { el } from '../dom.js';
import { safeStr } from '../utils.js';

function compareValues(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  // Numeric sort when both are numbers
  const na = typeof a === 'number' ? a : Number(a);
  const nb = typeof b === 'number' ? b : Number(b);
  if (Number.isFinite(na) && Number.isFinite(nb)) {
    return na - nb;
  }
  return String(a).localeCompare(String(b));
}

function applyFilterAndSort(rows, columns, { query, sortKey, direction }) {
  let result = rows;
  if (query) {
    const q = query.trim().toLowerCase();
    if (q) {
      result = result.filter((row) => {
        const hay = Object.values(row).map(safeStr).join(' ').toLowerCase();
        return hay.includes(q);
      });
    }
  }
  if (sortKey) {
    const col = columns.find((c) => c.key === sortKey);
    if (col) {
      const getValue = col.sortValue || ((row) => row[col.key]);
      result = [...result].sort((a, b) => {
        const cmp = compareValues(getValue(a), getValue(b));
        return direction === 'desc' ? -cmp : cmp;
      });
    }
  }
  return result;
}

export function table(columns, rows, options = {}) {
  const {
    filter = { enabled: false },
    empty = 'No data available.',
    id,
    defaultSort,
    onRowClick,
  } = options;

  let state = {
    query: '',
    sortKey: defaultSort?.key || '',
    direction: defaultSort?.direction || 'asc',
  };

  const attrs = { class: 'table-wrap' };
  if (id) attrs.id = id;
  const wrap = el('div', attrs, []);

  function render() {
    const filtered = applyFilterAndSort(rows, columns, state);
    wrap.replaceChildren();

    // Filter input
    if (filter.enabled) {
      const input = el('input', {
        type: 'search',
        class: 'table-filter',
        placeholder: filter.placeholder || 'Filter…',
        'aria-label': filter.placeholder || 'Filter',
        value: state.query,
      }, []);
      input.addEventListener('input', debounced((ev) => {
        state.query = ev.target.value;
        render();
      }, 150));
      wrap.appendChild(el('div', { class: 'table-toolbar' }, [input]));
    }

    if (!filtered.length) {
      wrap.appendChild(el('p', { class: 'empty' }, [empty]));
      return;
    }

    const headers = columns.map((col) => {
      const isSortable = col.sortable !== false;
      const isActive = state.sortKey === col.key;
      const ariaSort = isActive
        ? (state.direction === 'desc' ? 'descending' : 'ascending')
        : 'none';
      const thAttrs = { scope: 'col' };
      if (isActive) thAttrs['aria-sort'] = ariaSort;
      if (!isSortable) {
        return el('th', thAttrs, [col.label]);
      }
      const button = el('button', {
        type: 'button',
        class: 'th-sort',
        'aria-label': 'Sort by ' + col.label,
      }, [
        col.label,
        el('span', { class: 'th-arrow', 'aria-hidden': 'true' }, [
          isActive ? (state.direction === 'desc' ? '\u25BC' : '\u25B2') : '\u2195',
        ]),
      ]);
      button.addEventListener('click', () => {
        if (state.sortKey === col.key) {
          state.direction = state.direction === 'asc' ? 'desc' : 'asc';
        } else {
          state.sortKey = col.key;
          state.direction = 'asc';
        }
        render();
      });
      return el('th', thAttrs, [button]);
    });

    const body = filtered.map((row) => {
      const cells = columns.map((col) => {
        const val = col.render ? col.render(row) : safeStr(row[col.key]);
        return el('td', {}, [val instanceof Node ? val : String(val ?? '')]);
      });
      const trAttrs = {};
      if (onRowClick) {
        trAttrs.tabindex = 0;
        trAttrs.role = 'button';
        trAttrs.class = 'row-clickable';
      }
      const tr = el('tr', trAttrs, cells);
      if (onRowClick) {
        tr.addEventListener('click', () => onRowClick(row));
        tr.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            onRowClick(row);
          }
        });
      }
      return tr;
    });

    const tableEl = el('table', { class: 'data-table' }, [
      el('thead', {}, [el('tr', {}, headers)]),
      el('tbody', {}, body),
    ]);
    wrap.appendChild(tableEl);
  }

  render();
  return wrap;
}

function debounced(fn, ms) {
  let t = 0;
  return function (...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}
