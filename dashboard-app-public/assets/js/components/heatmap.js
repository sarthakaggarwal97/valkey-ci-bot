/**
 * Failure heatmap with drill-down drawer.
 *
 * Reads `daily_health` payload structure from dashboard.json:
 *   {
 *     repo, workflows[], workflow_reports[],
 *     heatmap: [{ name, days_failed, total_days, cells: [{date, count, has_run, job_ids?, run_id?}] }],
 *     tests: { [failureName]: { timeline: { [date]: { errors[], jobs[] } } } },
 *     failure_jobs: { [failureName]: { [date]: [jobId, ...] } },
 *     runs: [...]
 *   }
 *
 * Features:
 *   - multi-workflow tab switcher when workflow_reports has >1 entry
 *   - hover tooltip: date, count, errors, job names
 *   - click/Enter cell opens drawer with full context + GitHub job links
 *   - arrow keys navigate between cells in the drawer
 *   - color legend shows the intensity scale
 *   - horizontally scrollable with sticky first column
 *   - aria-label on each cell with full context
 */
import { el } from '../dom.js';
import { safeObj, safeList, safeInt, safeStr, truncate } from '../utils.js';
import { openDrawer } from './drawer.js';
import { externalLink } from './link.js';

function workflowLabel(name) {
  let s = safeStr(name);
  if (s.endsWith('.yml') || s.endsWith('.yaml')) s = s.replace(/\.ya?ml$/, '');
  s = s.replace(/[-_]/g, ' ').trim();
  return s ? s.replace(/\b\w/g, (c) => c.toUpperCase()) : 'Unknown';
}

function shortDate(date) {
  // 2026-04-29 -> 04-29
  const s = safeStr(date);
  return s.length >= 10 ? s.slice(5) : s;
}

function maxCount(report) {
  let max = 1;
  for (const row of safeList(report.heatmap)) {
    for (const c of safeList(safeObj(row).cells)) {
      const n = safeInt(safeObj(c).count);
      if (n > max) max = n;
    }
  }
  return max;
}

function runStatusByDate(runs, workflow) {
  const byDate = {};
  const wf = safeStr(workflow).toLowerCase();
  for (const run of safeList(runs)) {
    const r = safeObj(run);
    const d = safeStr(r.date);
    if (!d) continue;
    if (wf && safeStr(r.workflow).toLowerCase() !== wf) continue;
    const s = safeStr(r.status).toLowerCase();
    if (s === 'failure' || s === 'error') {
      byDate[d] = 'failure';
    } else if (!byDate[d]) {
      byDate[d] = s;
    }
  }
  return byDate;
}

function colorLegend() {
  const steps = [0.2, 0.4, 0.6, 0.8, 1.0];
  return el('div', { class: 'heat-legend', role: 'presentation' }, [
    el('span', { class: 'heat-legend-label' }, ['Failures']),
    el('span', { class: 'heat-legend-label heat-legend-end' }, ['Fewer']),
    ...steps.map((a) =>
      el('span', {
        class: 'heat-legend-cell',
        style: '--heat-alpha: ' + a,
        'aria-hidden': 'true',
      }, [])
    ),
    el('span', { class: 'heat-legend-label heat-legend-end' }, ['More']),
  ]);
}

function openCellDrawer(ctx) {
  // ctx: { repo, failureName, date, count, jobIds, errors, jobNames, runId, workflow, neighbours, cellIndex, focusSibling }
  const body = el('div', {}, [
    el('dl', { class: 'drawer-stats' }, [
      el('dt', {}, ['Failure']),
      el('dd', {}, [ctx.failureName]),
      el('dt', {}, ['Date']),
      el('dd', {}, [ctx.date]),
      el('dt', {}, ['Workflow']),
      el('dd', {}, [workflowLabel(ctx.workflow)]),
      el('dt', {}, ['Hits']),
      el('dd', {}, [String(ctx.count)]),
    ]),
    ctx.errors && ctx.errors.length ? el('section', { class: 'drawer-section' }, [
      el('h3', {}, ['Error signatures']),
      el('ul', {},
        ctx.errors.slice(0, 5).map((e) => el('li', {}, [String(e)]))
      ),
    ]) : null,
    ctx.jobIds && ctx.jobIds.length && ctx.repo ? el('section', { class: 'drawer-section' }, [
      el('h3', {}, ['Failed jobs']),
      el('ul', { class: 'drawer-link-list' },
        ctx.jobIds.map((jid, i) => el('li', {}, [
          externalLink(
            'Job ' + (i + 1) + ' (#' + jid + ')',
            'https://github.com/' + ctx.repo + '/actions/runs/' + (ctx.runId || '') + '/job/' + jid
          ),
        ]))
      ),
    ]) : null,
    ctx.jobNames && ctx.jobNames.length ? el('section', { class: 'drawer-section' }, [
      el('h3', {}, ['Job names']),
      el('p', {}, [ctx.jobNames.join(', ')]),
    ]) : null,
    ctx.runId && ctx.repo ? el('section', { class: 'drawer-section' }, [
      externalLink('View workflow run', 'https://github.com/' + ctx.repo + '/actions/runs/' + ctx.runId),
    ]) : null,
    el('p', { class: 'drawer-hint' }, ['Use \u2190 and \u2192 to move between cells.']),
  ]);

  const drawer = openDrawer({
    title: ctx.failureName + ' \u00B7 ' + ctx.date,
    body,
  });

  // Arrow-key navigation between neighbouring cells
  const onKey = (ev) => {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    const idx = ctx.cellIndex;
    const delta = ev.key === 'ArrowRight' ? 1 : -1;
    const next = ctx.focusSibling && ctx.focusSibling(idx + delta);
    if (next) {
      drawer.close();
      setTimeout(() => next(), 130);
    }
  };
  document.addEventListener('keydown', onKey);
  const originalClose = drawer.close;
  drawer.close = () => {
    document.removeEventListener('keydown', onKey);
    originalClose();
  };
  return drawer;
}

function renderOneReport(report, { repo, tests, failureJobs, runs, workflow }) {
  const rows = safeList(report.heatmap).slice(0, 30);
  const dates = safeList(report.dates);
  if (!rows.length || !dates.length) {
    return el('p', { class: 'empty' }, ['No heatmap data for this workflow.']);
  }
  const max = maxCount(report);
  const statusByDate = runStatusByDate(runs, workflow);

  // Build table — sticky first column, cells with data-* for drill-down
  const head = el('tr', {}, [
    el('th', { scope: 'col', class: 'heat-sticky' }, ['Failure']),
    el('th', { scope: 'col', class: 'heat-sticky heat-sticky-2' }, ['Freq']),
    ...dates.map((d) => el('th', { scope: 'col' }, [shortDate(d)])),
  ]);

  // Status row (shows run success/failure per date)
  const statusRow = Object.keys(statusByDate).length
    ? el('tr', { class: 'heat-status-row' }, [
        el('th', { scope: 'row', class: 'heat-sticky' }, ['Run status']),
        el('td', { class: 'heat-sticky heat-sticky-2' }, []),
        ...dates.map((d) => {
          const s = statusByDate[d] || '';
          if (!s) return el('td', { class: 'heat-cell heat-cell-missing' }, ['\u2014']);
          if (s === 'success' || s === 'completed') {
            return el('td', { class: 'heat-cell heat-status-good', 'aria-label': d + ': success' }, ['\u2713']);
          }
          if (s === 'in_progress' || s === 'queued' || s === 'pending') {
            return el('td', { class: 'heat-cell heat-status-neutral', 'aria-label': d + ': ' + s }, ['\u22EF']);
          }
          return el('td', { class: 'heat-cell heat-status-bad', 'aria-label': d + ': ' + s }, ['\u2717']);
        }),
      ])
    : null;

  const bodyRows = rows.map((row) => {
    const r = safeObj(row);
    const failureName = safeStr(r.name);
    const daysFailed = safeInt(r.days_failed);
    const totalDays = Math.max(safeInt(r.total_days), 1);
    const cells = safeList(r.cells);
    const testInfo = safeObj(safeObj(tests)[failureName]);
    const timeline = safeObj(testInfo.timeline);
    const jobsMap = safeObj(safeObj(failureJobs)[failureName]);

    const focusableCells = [];

    const cellNodes = cells.map((cell, idx) => {
      const c = safeObj(cell);
      const count = safeInt(c.count);
      const hasRun = c.has_run !== false;
      const cellDate = safeStr(c.date);
      const alpha = count ? (0.2 + (count / max) * 0.8) : 0;
      const dayInfo = safeObj(timeline[cellDate]);
      const errors = safeList(dayInfo.errors);
      const jobNames = safeList(dayInfo.jobs);
      const jobIds = safeList(c.job_ids).length ? safeList(c.job_ids) : safeList(jobsMap[cellDate]);
      const runId = safeStr(c.run_id);

      let cls = 'heat-cell';
      let content = '';
      let ariaLabel = cellDate + ': ';
      if (!hasRun) {
        cls += ' heat-cell-missing';
        content = '\u2014';
        ariaLabel += 'no run data';
      } else if (count) {
        cls += ' heat-cell-hit';
        content = String(count);
        ariaLabel += count + ' failure' + (count === 1 ? '' : 's');
      } else {
        ariaLabel += 'no failures';
      }

      const attrs = {
        class: cls,
        tabindex: (hasRun && count) ? 0 : -1,
        'aria-label': ariaLabel,
      };
      if (count) attrs.style = '--heat-alpha: ' + alpha.toFixed(2);
      if (errors.length || jobNames.length) {
        attrs.title = errors.length ? truncate(errors[0], 120) : jobNames.join(', ');
      }

      const node = el('td', attrs, [content]);

      if (hasRun && count) {
        focusableCells.push({ node, idx, ctx: {
          repo, failureName, date: cellDate, count, jobIds, errors, jobNames,
          runId, workflow, cellIndex: idx,
        }});
        const open = () => {
          const ctxEntry = focusableCells.find((fc) => fc.idx === idx);
          if (!ctxEntry) return;
          const ctx = { ...ctxEntry.ctx };
          ctx.focusSibling = (nextIdx) => {
            const sibling = focusableCells.find((fc) => fc.idx === nextIdx);
            if (!sibling) return null;
            return () => {
              sibling.node.focus();
              openCellDrawer(sibling.ctx);
            };
          };
          openCellDrawer(ctx);
        };
        node.addEventListener('click', open);
        node.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            open();
          }
        });
      }

      return node;
    });

    const nameCell = el('th', { scope: 'row', class: 'heat-sticky' }, [
      el('span', { class: 'heat-row-name' }, [failureName]),
      daysFailed >= totalDays && totalDays
        ? el('span', { class: 'chip chip-bad heat-row-badge' }, ['daily'])
        : null,
    ]);

    return el('tr', {}, [
      nameCell,
      el('td', { class: 'heat-sticky heat-sticky-2' }, [daysFailed + '/' + totalDays]),
      ...cellNodes,
    ]);
  });

  return el('div', { class: 'heatmap-wrap' }, [
    colorLegend(),
    el('div', { class: 'heatmap-scroll' }, [
      el('table', { class: 'heatmap-table' }, [
        el('thead', {}, [head]),
        el('tbody', {}, [statusRow, ...bodyRows].filter(Boolean)),
      ]),
    ]),
  ]);
}

export function heatmap(dailyHealth) {
  const dh = safeObj(dailyHealth);
  const reports = safeList(dh.workflow_reports);
  const repo = safeStr(dh.repo);
  const tests = safeObj(dh.tests);
  const failureJobs = safeObj(dh.failure_jobs);
  const runs = safeList(dh.runs);

  // Fall back to a single "main" report using top-level fields.
  if (reports.length === 0) {
    if (safeList(dh.heatmap).length) {
      return renderOneReport(dh, { repo, tests, failureJobs, runs, workflow: safeStr(dh.workflow) });
    }
    return el('p', { class: 'empty' }, ['No heatmap data available.']);
  }

  if (reports.length === 1) {
    return renderOneReport(reports[0], { repo, tests, failureJobs, runs, workflow: safeStr(reports[0].workflow) });
  }

  // Multi-workflow: tab switcher
  const tabList = el('div', { class: 'heat-tabs', role: 'tablist', 'aria-label': 'Workflow heatmaps' }, []);
  const tabPanel = el('div', { class: 'heat-tab-panel' }, []);

  const tabs = reports.map((report, i) => {
    const wf = safeStr(report.workflow);
    const id = 'heat-tab-' + i;
    const tab = el('button', {
      type: 'button',
      class: 'heat-tab',
      role: 'tab',
      id: id,
      'aria-selected': i === 0 ? 'true' : 'false',
      'aria-controls': 'heat-tab-panel',
      tabindex: i === 0 ? 0 : -1,
    }, [workflowLabel(wf)]);
    tab.addEventListener('click', () => activate(i));
    tab.addEventListener('keydown', (ev) => {
      if (ev.key === 'ArrowRight') { ev.preventDefault(); activate((i + 1) % reports.length); }
      else if (ev.key === 'ArrowLeft') { ev.preventDefault(); activate((i - 1 + reports.length) % reports.length); }
    });
    tabList.appendChild(tab);
    return tab;
  });

  function activate(i) {
    tabs.forEach((t, j) => {
      t.setAttribute('aria-selected', j === i ? 'true' : 'false');
      t.setAttribute('tabindex', j === i ? '0' : '-1');
    });
    tabs[i].focus();
    const report = reports[i];
    tabPanel.replaceChildren(renderOneReport(report, {
      repo, tests, failureJobs, runs, workflow: safeStr(report.workflow),
    }));
  }

  activate(0);

  return el('div', { class: 'heatmap-root' }, [
    tabList,
    el('div', { id: 'heat-tab-panel', role: 'tabpanel' }, [tabPanel]),
  ]);
}
