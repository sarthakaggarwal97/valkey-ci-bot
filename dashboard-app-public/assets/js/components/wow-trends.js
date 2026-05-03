/**
 * Week-over-week trend comparison. Reads dashboard.wow_trends.
 * Shows: 3 stat cards, new/resolved failures, top movers.
 * When has_data is false, renders a graceful empty state.
 */
import { el } from '../dom.js';
import { safeObj, safeList, safeInt, formatNumber } from '../utils.js';

function statCard(label, primary, detail, toneClass = '') {
  return el('div', { class: 'wow-stat ' + toneClass }, [
    el('span', {}, [label]),
    el('strong', {}, [primary]),
    detail ? el('small', {}, [detail]) : null,
  ]);
}

export function wowTrends(wow) {
  const data = safeObj(wow);
  if (!data.has_data) {
    return el('p', { class: 'empty-inline' }, [
      'Not enough history for week-over-week comparison yet.',
    ]);
  }

  const tw = safeObj(data.this_week);
  const lw = safeObj(data.last_week);
  const delta = safeInt(data.delta);
  const pct = Number(data.pct_change) || 0;
  const deltaStr = (delta > 0 ? '+' : '') + delta;
  const pctStr = (pct > 0 ? '+' : '') + pct + '%';
  const toneClass = delta > 0 ? 'wow-delta-bad' : (delta < 0 ? 'wow-delta-good' : 'wow-delta-accent');

  const stats = el('div', { class: 'wow-stats' }, [
    statCard(
      'This week',
      formatNumber(safeInt(tw.total_failure_hits)),
      formatNumber(safeInt(tw.unique_failures)) + ' unique \u00B7 ' +
      formatNumber(safeInt(tw.failed_runs)) + '/' + formatNumber(safeInt(tw.total_runs)) + ' runs failed',
    ),
    statCard(
      'Last week',
      formatNumber(safeInt(lw.total_failure_hits)),
      formatNumber(safeInt(lw.unique_failures)) + ' unique \u00B7 ' +
      formatNumber(safeInt(lw.failed_runs)) + '/' + formatNumber(safeInt(lw.total_runs)) + ' runs failed',
    ),
    statCard('WoW change', deltaStr, pctStr, toneClass),
  ]);

  const newFailures = safeList(data.new_failures);
  const resolved = safeList(data.resolved_failures);

  const movement = el('div', { class: 'wow-movement' }, [
    newFailures.length ? el('div', { class: 'wow-list wow-new' }, [
      el('h4', {}, ['\uD83D\uDD34 New this week (' + newFailures.length + ')']),
      el('ul', {}, newFailures.map((n) => el('li', {}, [String(n)]))),
    ]) : null,
    resolved.length ? el('div', { class: 'wow-list wow-resolved' }, [
      el('h4', {}, ['\u2705 Resolved (' + resolved.length + ')']),
      el('ul', {}, resolved.map((n) => el('li', {}, [String(n)]))),
    ]) : null,
    !newFailures.length && !resolved.length
      ? el('p', { class: 'empty-inline' }, ['No new or resolved failures between weeks.'])
      : null,
  ]);

  const movers = safeList(data.top_movers);
  let moversBlock = null;
  if (movers.length) {
    const rows = movers.slice(0, 8).map((m) => {
      const m_ = safeObj(m);
      const change = safeInt(m_.change);
      const arrow = change > 0 ? '\u2191' : '\u2193';
      const changeClass = change > 0 ? 'wow-change-bad' : 'wow-change-good';
      return el('tr', {}, [
        el('td', {}, [String(m_.name ?? '')]),
        el('td', {}, [formatNumber(safeInt(m_.last_week))]),
        el('td', {}, [formatNumber(safeInt(m_.this_week))]),
        el('td', { class: changeClass }, [arrow + ' ' + Math.abs(change)]),
      ]);
    });
    moversBlock = el('div', { class: 'wow-movers' }, [
      el('h4', {}, ['Top movers']),
      el('table', { class: 'wow-table' }, [
        el('thead', {}, [el('tr', {}, [
          el('th', { scope: 'col' }, ['Failure']),
          el('th', { scope: 'col' }, ['Last wk']),
          el('th', { scope: 'col' }, ['This wk']),
          el('th', { scope: 'col' }, ['\u0394']),
        ])]),
        el('tbody', {}, rows),
      ]),
    ]);
  }

  return el('div', { class: 'wow-root' }, [stats, movement, moversBlock]);
}
