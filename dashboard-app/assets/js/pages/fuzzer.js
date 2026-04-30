/**
 * Fuzzer page.
 * Sections: metrics, root-cause mix bar chart, status & issue actions, recent anomalies.
 */
import { el, fragment } from '../dom.js';
import {
  safeObj, safeList, safeInt, safeStr,
  formatNumber, truncate,
} from '../utils.js';
import { chip } from '../components/chip.js';
import { metric, metricGrid } from '../components/metric.js';
import { panel } from '../components/panel.js';
import { table } from '../components/table.js';
import { externalLink } from '../components/link.js';
import { statGrid } from '../components/stat-grid.js';
import { openDrawer } from '../components/drawer.js';

function headerMetrics(dashboard) {
  const fuzzer = safeObj(dashboard.fuzzer);
  const statusCounts = safeObj(fuzzer.status_counts);
  const issueActions = safeObj(fuzzer.issue_action_counts);
  const scenarios = safeObj(fuzzer.scenario_counts);
  const anomalies = safeInt(statusCounts.anomalous);
  return metricGrid([
    metric('Runs seen', safeInt(fuzzer.runs_seen), { note: 'Current payload' }),
    metric('Runs analyzed', safeInt(fuzzer.runs_analyzed), { note: 'With classifier output' }),
    metric('Anomalies', anomalies, {
      note: 'Non-normal runs',
      tone: anomalies ? 'bad' : 'good',
    }),
    metric('Scenarios', Object.keys(scenarios).length, { note: 'Distinct paths' }),
    metric('Issues updated', safeInt(issueActions.updated) + safeInt(issueActions.created), {
      note: 'GitHub issue actions',
    }),
  ]);
}

function rootCauseBarChart(fuzzer) {
  const counts = safeObj(fuzzer.root_cause_counts);
  const entries = Object.entries(counts)
    .map(([name, count]) => [name, safeInt(count)])
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10);

  if (!entries.length) {
    return el('p', { class: 'empty' }, ['No root-cause buckets recorded.']);
  }

  const max = Math.max(...entries.map(([, n]) => n), 1);
  return el('div', { class: 'bar-chart', role: 'list' }, entries.map(([name, count]) => {
    const pct = (count / max) * 100;
    return el('div', { class: 'bar-row', role: 'listitem' }, [
      el('span', { class: 'bar-label' }, [name]),
      el('div', {
        class: 'bar-track',
        role: 'progressbar',
        'aria-valuenow': count,
        'aria-valuemin': 0,
        'aria-valuemax': max,
        'aria-label': name + ': ' + count,
      }, [
        el('div', { class: 'bar-fill', style: 'width: ' + pct + '%' }, []),
      ]),
      el('span', { class: 'bar-value' }, [formatNumber(count)]),
    ]);
  }));
}

function statusGridSection(fuzzer) {
  const statusCounts = safeObj(fuzzer.status_counts);
  const issueActions = safeObj(fuzzer.issue_action_counts);
  const statuses = Object.keys(statusCounts).sort().join(', ') || 'none';
  const actions = Object.keys(issueActions).sort().join(', ') || 'none';
  return statGrid([
    { label: 'Statuses', value: chip(statuses, { tone: 'info' }) },
    { label: 'Issue actions', value: chip(actions, { tone: 'info' }) },
    { label: 'Scenarios', value: formatNumber(Object.keys(safeObj(fuzzer.scenario_counts)).length) },
    { label: 'Runs seen', value: formatNumber(safeInt(fuzzer.runs_seen)) },
    { label: 'Analyzed', value: formatNumber(safeInt(fuzzer.runs_analyzed)) },
    { label: 'Anomalies', value: formatNumber(safeInt(statusCounts.anomalous)) },
  ]);
}

function anomalyDrawer(row) {
  const body = el('div', {}, [
    el('dl', { class: 'drawer-stats' }, [
      el('dt', {}, ['Run']),
      el('dd', {}, [
        row.run_url
          ? externalLink(row.run_id || 'Open run', row.run_url)
          : (row.run_id || 'unknown'),
      ]),
      el('dt', {}, ['Status']),
      el('dd', {}, [chip(row.status)]),
      el('dt', {}, ['Triage']),
      el('dd', {}, [chip(row.triage_verdict || 'unknown')]),
      el('dt', {}, ['Scenario']),
      el('dd', {}, [row.scenario_id || 'n/a']),
      el('dt', {}, ['Seed']),
      el('dd', {}, [row.seed || 'n/a']),
      el('dt', {}, ['Root cause']),
      el('dd', {}, [row.root_cause_category || 'n/a']),
      row.issue_url ? el('dt', {}, ['Issue']) : null,
      row.issue_url ? el('dd', {}, [
        externalLink(row.issue_action || 'issue', row.issue_url),
      ]) : null,
    ]),
    row.summary ? el('section', { class: 'drawer-section' }, [
      el('h3', {}, ['Summary']),
      el('p', {}, [row.summary]),
    ]) : null,
  ]);
  openDrawer({ title: 'Anomaly \u00B7 ' + (row.run_id || 'unknown'), body });
}

function anomaliesTable(dashboard) {
  const fuzzer = safeObj(dashboard.fuzzer);
  const items = safeList(fuzzer.recent_anomalies).map((a) => {
    const an = safeObj(a);
    return {
      run_id: safeStr(an.run_id),
      run_url: safeStr(an.run_url),
      status: safeStr(an.status),
      triage_verdict: safeStr(an.triage_verdict),
      scenario_id: safeStr(an.scenario_id),
      seed: safeStr(an.seed),
      root_cause_category: safeStr(an.root_cause_category),
      summary: safeStr(an.summary),
      issue_url: safeStr(an.issue_url),
      issue_action: safeStr(an.issue_action),
    };
  });

  if (!items.length) {
    return el('p', { class: 'empty' }, ['No anomalous or warning fuzzer runs in the current payload.']);
  }

  return table(
    [
      {
        key: 'run_id',
        label: 'Run',
        render: (row) =>
          row.run_url ? externalLink(row.run_id, row.run_url) : row.run_id,
      },
      { key: 'status', label: 'Status', render: (row) => chip(row.status) },
      {
        key: 'triage_verdict',
        label: 'Triage',
        render: (row) => chip(row.triage_verdict || 'unknown'),
      },
      { key: 'scenario_id', label: 'Scenario' },
      { key: 'seed', label: 'Seed' },
      { key: 'root_cause_category', label: 'Root cause' },
      {
        key: 'issue_url',
        label: 'Issue',
        sortable: false,
        render: (row) =>
          row.issue_url ? externalLink(row.issue_action || 'issue', row.issue_url) : 'n/a',
      },
      {
        key: 'summary',
        label: 'Summary',
        sortable: false,
        render: (row) => truncate(row.summary, 90),
      },
    ],
    items,
    {
      filter: { enabled: true, placeholder: 'Filter anomalies…' },
      empty: 'No anomalies match the filter.',
      onRowClick: anomalyDrawer,
    }
  );
}

export function render(container, dashboard, ctx = {}) {
  const fuzzer = safeObj(dashboard.fuzzer);
  const hero = el('header', { class: 'hero' }, [
    el('div', { class: 'hero-row' }, [
      el('div', {}, [
        el('h2', {}, ['Fuzzer']),
        el('p', {}, ['Anomalies, seeds, issue actions, and root-cause classifications.']),
      ]),
    ]),
  ]);

  container.replaceChildren(fragment([
    hero,
    headerMetrics(dashboard),
    panel({
      title: 'Root-cause mix',
      subtitle: 'Top anomalous root-cause categories across the current payload.',
      body: rootCauseBarChart(fuzzer),
    }),
    panel({
      title: 'Status and issue actions',
      subtitle: 'Fuzzer operators usually need the live anomaly mix before the full table.',
      body: statusGridSection(fuzzer),
    }),
    panel({
      title: 'Recent anomalies',
      subtitle: 'Click a row for the full summary. Run and issue links resolve to GitHub.',
      body: anomaliesTable(dashboard),
      wide: true,
    }),
  ]));
}
