/**
 * Diagnostics page.
 * Sections: metrics, data coverage, incident queue, event stream,
 * monitor watermarks, AI reliability, input warnings.
 */
import { el, fragment } from '../dom.js';
import {
  safeObj, safeList, safeInt, safeStr,
  formatNumber, formatRate, formatPercent, truncate,
  pullUrl, commitUrl, runUrl,
} from '../utils.js';
import { chip } from '../components/chip.js';
import { metric, metricGrid } from '../components/metric.js';
import { panel } from '../components/panel.js';
import { table } from '../components/table.js';
import { externalLink } from '../components/link.js';
import { statGrid } from '../components/stat-grid.js';

function headerMetrics(dashboard) {
  const ci = safeObj(dashboard.ci_failures);
  const outcomes = safeObj(dashboard.agent_outcomes);
  const health = safeObj(dashboard.state_health);
  const ai = safeObj(dashboard.ai_reliability);
  const schemaCalls = safeInt(ai.schema_calls);
  const schemaSuccess = safeInt(ai.schema_successes);

  return metricGrid([
    metric('Incidents', safeInt(ci.failure_incidents), { note: 'Recent entries' }),
    metric('Queued failures', safeInt(ci.queued_failures), {
      note: 'Awaiting action',
      tone: safeInt(ci.queued_failures) ? 'warn' : 'good',
    }),
    metric('Ledger events', safeInt(outcomes.events), { note: 'Append-only stream' }),
    metric('Watermarks', safeInt(health.monitor_watermarks), { note: 'Monitor checkpoints' }),
    metric('Schema success', schemaCalls ? formatRate(schemaSuccess, schemaCalls) : 'n/a', {
      note: 'AI reliability',
    }),
  ]);
}

function coverageTable(dashboard) {
  const daily = safeObj(dashboard.daily_health);
  const flaky = safeObj(dashboard.flaky_tests);
  const reviews = safeObj(dashboard.pr_reviews);
  const acceptance = safeObj(dashboard.acceptance);
  const fuzzer = safeObj(dashboard.fuzzer);
  const outcomes = safeObj(dashboard.agent_outcomes);
  const health = safeObj(dashboard.state_health);
  const ai = safeObj(dashboard.ai_reliability);
  const warnings = safeList(health.input_warnings);

  function resolve(label, href, { present, partial, detail }) {
    let status, tone;
    if (present && !partial) { status = 'available'; tone = 'good'; }
    else if (present && partial) { status = 'partial'; tone = 'warn'; }
    else { status = 'missing'; tone = 'bad'; }
    return { label, href, status, tone, detail };
  }

  const items = [
    resolve('Daily health', '#/daily', {
      present: Boolean(safeList(daily.runs).length || safeList(daily.dates).length),
      partial: !safeList(daily.heatmap).length,
      detail: safeInt(daily.total_runs) + ' runs, ' + safeInt(daily.failed_runs) + ' failed',
    }),
    resolve('Flaky campaigns', '#/daily/campaigns', {
      present: Boolean(safeList(flaky.recent_campaigns).length || Object.keys(safeObj(flaky.status_counts)).length),
      partial: false,
      detail: safeInt(flaky.active_campaigns) + ' active, ' + safeInt(flaky.campaigns) + ' total',
    }),
    resolve('PR review state', '#/prs', {
      present: Boolean(safeInt(reviews.tracked_prs) || safeList(reviews.recent_reviews).length),
      partial: !safeInt(reviews.review_comments),
      detail: safeInt(reviews.tracked_prs) + ' PRs, ' + safeInt(reviews.review_comments) + ' comments',
    }),
    resolve('Replay acceptance', '#/prs/replay', {
      present: Boolean(safeInt(acceptance.payloads_seen) || safeList(acceptance.recent_review_results).length),
      partial: !safeList(acceptance.recent_review_results).length,
      detail: safeInt(acceptance.review_cases) + ' review cases, ' + safeInt(acceptance.workflow_cases) + ' workflow cases',
    }),
    resolve('Fuzzer analysis', '#/fuzzer', {
      present: Boolean(safeInt(fuzzer.runs_seen) || safeInt(fuzzer.result_files)),
      partial: safeInt(fuzzer.runs_analyzed) < safeInt(fuzzer.runs_seen),
      detail: safeInt(fuzzer.runs_analyzed) + '/' + safeInt(fuzzer.runs_seen) + ' runs analyzed',
    }),
    resolve('Event ledger', '#/diagnostics/events', {
      present: Boolean(safeInt(outcomes.events)),
      partial: false,
      detail: safeInt(outcomes.events) + ' events recorded',
    }),
    resolve('Monitor state', '#/diagnostics/watermarks', {
      present: Boolean(safeInt(health.monitor_watermarks) || warnings.length),
      partial: warnings.length > 0,
      detail: safeInt(health.monitor_watermarks) + ' watermarks' +
        (warnings.length ? ', ' + warnings.length + ' warnings' : ''),
    }),
    resolve('AI reliability', '#/diagnostics/ai-reliability', {
      present: Boolean(Object.keys(safeObj(ai.ai_metrics)).length || safeInt(ai.token_usage)),
      partial: !safeInt(ai.prompt_safety_checked),
      detail: safeInt(ai.schema_calls) + ' schema calls, ' +
        formatPercent(Number(ai.prompt_safety_coverage) || 0) + ' safety coverage',
    }),
  ];

  return table(
    [
      { key: 'label', label: 'Source' },
      { key: 'status', label: 'Status', render: (row) => chip(row.status, { tone: row.tone }) },
      { key: 'detail', label: 'Detail' },
      {
        key: 'href',
        label: 'Page',
        sortable: false,
        render: (row) => {
          const link = el('a', { href: row.href, class: 'link link-compact' }, [
            row.href.replace(/^#\//, '').split('/')[0] || 'index',
          ]);
          return link;
        },
      },
    ],
    items,
    { empty: 'No source-coverage details available.' }
  );
}

function incidentsTable(dashboard) {
  const ci = safeObj(dashboard.ci_failures);
  const items = safeList(ci.recent_incidents).map((i) => {
    const inc = safeObj(i);
    return {
      failure_identifier: safeStr(inc.failure_identifier),
      status: safeStr(inc.status),
      file_path: safeStr(inc.file_path),
      pr_url: safeStr(inc.pr_url),
      updated_at: safeStr(inc.updated_at),
    };
  });

  if (!items.length) {
    return el('p', { class: 'empty' }, ['No recent incidents.']);
  }

  return table(
    [
      { key: 'failure_identifier', label: 'Failure' },
      { key: 'status', label: 'Status', render: (row) => chip(row.status) },
      { key: 'file_path', label: 'Path' },
      {
        key: 'pr_url',
        label: 'PR',
        sortable: false,
        render: (row) => row.pr_url ? externalLink('PR', row.pr_url) : 'n/a',
      },
      { key: 'updated_at', label: 'Updated' },
    ],
    items,
    {
      filter: { enabled: true, placeholder: 'Filter incidents…' },
      defaultSort: { key: 'updated_at', direction: 'desc' },
      empty: 'No incidents match the filter.',
    }
  );
}

function eventSubjectLink(event, repoFallback) {
  const attributes = safeObj(event.attributes);
  const subject = safeStr(event.subject);
  // Attribute URL fields take precedence
  for (const key of ['pr_url', 'issue_url', 'run_url', 'proof_url', 'url']) {
    const url = safeStr(attributes[key]);
    if (url) return externalLink(subject, url);
  }
  // Subject is an absolute URL
  if (subject.startsWith('http://') || subject.startsWith('https://')) {
    return externalLink(subject, subject);
  }
  // Patterns: owner/repo#123, owner/repo:workflow:run, owner/repo@sha, #123
  const prMatch = subject.match(/^([^#\s]+\/[^#\s]+)#(\d+)$/);
  if (prMatch) return externalLink(subject, pullUrl(prMatch[1], prMatch[2]));

  const runMatch = subject.match(/^([^:\s]+\/[^:\s]+):[^:]+:(\d+)$/);
  if (runMatch) return externalLink(subject, runUrl(runMatch[1], runMatch[2]));

  const commitMatch = subject.match(/^([^@\s]+\/[^@\s]+)@([0-9a-fA-F]{7,40})$/);
  if (commitMatch) return externalLink(subject, commitUrl(commitMatch[1], commitMatch[2]));

  const shortPr = subject.match(/^#(\d+)$/);
  if (shortPr && repoFallback) return externalLink(subject, pullUrl(repoFallback, shortPr[1]));

  return el('span', {}, [subject]);
}

function eventAttributesSummary(attributes) {
  const attrs = safeObj(attributes);
  if (!Object.keys(attrs).length) return 'n/a';
  const parts = [];
  for (const [key, value] of Object.entries(attrs).sort()) {
    if (key.endsWith('_url')) continue;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      parts.push(key + '=' + value);
    } else if (Array.isArray(value)) {
      const rendered = value.slice(0, 3).map(String).join(', ');
      parts.push(key + '=[' + rendered + (value.length > 3 ? ', \u2026' : '') + ']');
    } else if (value && typeof value === 'object') {
      parts.push(key + '=\u2026');
    }
  }
  if (!parts.length) return 'link-only';
  return truncate(parts.join(', '), 140);
}

function eventsTable(dashboard) {
  const outcomes = safeObj(dashboard.agent_outcomes);
  const daily = safeObj(dashboard.daily_health);
  const repoFallback = safeStr(daily.repo) || 'valkey-io/valkey';
  const items = safeList(outcomes.recent_events).slice(0, 15).map((e) => {
    const ev = safeObj(e);
    return {
      created_at: safeStr(ev.created_at),
      event_type: safeStr(ev.event_type),
      subject: safeStr(ev.subject),
      attributes: ev.attributes,
    };
  });

  if (!items.length) {
    return el('p', { class: 'empty' }, ['No recent ledger events.']);
  }

  return table(
    [
      { key: 'created_at', label: 'Time' },
      { key: 'event_type', label: 'Event', render: (row) => chip(row.event_type) },
      {
        key: 'subject',
        label: 'Subject',
        render: (row) => eventSubjectLink(row, repoFallback),
      },
      {
        key: 'attributes',
        label: 'Detail',
        sortable: false,
        render: (row) => eventAttributesSummary(row.attributes),
      },
    ],
    items,
    { defaultSort: { key: 'created_at', direction: 'desc' } }
  );
}

function watermarksTable(dashboard) {
  const health = safeObj(dashboard.state_health);
  const items = safeList(health.recent_watermarks).map((w) => {
    const wm = safeObj(w);
    return {
      key: safeStr(wm.key),
      last_seen_run_id: safeStr(wm.last_seen_run_id),
      target_repo: safeStr(wm.target_repo),
      workflow_file: safeStr(wm.workflow_file),
      updated_at: safeStr(wm.updated_at),
    };
  });

  if (!items.length) {
    return el('p', { class: 'empty' }, ['No monitor watermarks supplied.']);
  }

  return table(
    [
      { key: 'key', label: 'Key' },
      {
        key: 'last_seen_run_id',
        label: 'Last run',
        render: (row) => externalLink(row.last_seen_run_id, runUrl(row.target_repo, row.last_seen_run_id)),
      },
      { key: 'target_repo', label: 'Repo' },
      { key: 'workflow_file', label: 'Workflow' },
      { key: 'updated_at', label: 'Updated' },
    ],
    items,
    { defaultSort: { key: 'updated_at', direction: 'desc' } }
  );
}

function aiReliabilitySection(dashboard) {
  const ai = safeObj(dashboard.ai_reliability);
  const rows = [
    { label: 'Token usage', value: formatNumber(safeInt(ai.token_usage)) },
    { label: 'Schema calls', value: formatNumber(safeInt(ai.schema_calls)) },
    { label: 'Schema successes', value: formatNumber(safeInt(ai.schema_successes)) },
    { label: 'Tool-loop calls', value: formatNumber(safeInt(ai.tool_loop_calls)) },
    { label: 'Terminal rejections', value: formatNumber(safeInt(ai.terminal_validation_rejections)) },
    { label: 'Prompt safety', value: formatPercent(Number(ai.prompt_safety_coverage) || 0) },
    { label: 'Retries', value: formatNumber(safeInt(ai.bedrock_retries)) },
    { label: 'Retry exhausted', value: formatNumber(safeInt(ai.retry_exhaustions)) },
  ];
  const gaps = safeList(ai.instrumentation_gaps).map(safeStr).filter(Boolean);

  return el('div', {}, [
    statGrid(rows),
    gaps.length
      ? el('div', { class: 'gap-list' }, [
          el('h3', { class: 'gap-list-title' }, ['Instrumentation gaps']),
          el('ul', { class: 'bullet-list' }, gaps.map((g) => el('li', {}, [g]))),
        ])
      : el('p', { class: 'empty-inline' }, ['No instrumentation gaps recorded.']),
  ]);
}

function warningsSection(dashboard) {
  const health = safeObj(dashboard.state_health);
  const warnings = safeList(health.input_warnings).map(safeStr).filter(Boolean);
  if (!warnings.length) {
    return el('p', { class: 'empty-inline' }, ['No input warnings recorded.']);
  }
  return el('ul', { class: 'bullet-list' }, warnings.map((w) => el('li', {}, [w])));
}

export function render(container, dashboard, ctx = {}) {
  const hero = el('header', { class: 'hero' }, [
    el('div', { class: 'hero-row' }, [
      el('div', {}, [
        el('h2', {}, ['Diagnostics']),
        el('p', {}, ['Source coverage, incident queue, event ledger, watermarks, and AI counters.']),
      ]),
    ]),
  ]);

  container.replaceChildren(fragment([
    hero,
    headerMetrics(dashboard),
    panel({
      title: 'Data coverage',
      subtitle: 'Missing and partial sources are called out here first so empty panels never look healthy by accident.',
      body: coverageTable(dashboard),
      wide: true,
    }),
    panel({
      title: 'Incident queue',
      subtitle: 'Recent failure store entries and their remediation status.',
      body: incidentsTable(dashboard),
      wide: true,
    }),
    panel({
      title: 'Event stream',
      subtitle: 'Recent entries from the append-only ledger.',
      anchor: 'events',
      body: eventsTable(dashboard),
      wide: true,
    }),
    panel({
      title: 'Monitor watermarks',
      subtitle: 'Workflow-run checkpoints used by the monitors.',
      anchor: 'watermarks',
      body: watermarksTable(dashboard),
      wide: true,
    }),
    panel({
      title: 'AI reliability',
      subtitle: 'Bedrock reliability counters. Helps catch drift or regressions in the model layer.',
      anchor: 'ai-reliability',
      body: aiReliabilitySection(dashboard),
    }),
    panel({
      title: 'Input warnings',
      subtitle: 'Raw loader warnings from missing or unreadable input files.',
      body: warningsSection(dashboard),
    }),
  ]));

  if (ctx.sub) {
    const target = document.getElementById(ctx.sub);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}
