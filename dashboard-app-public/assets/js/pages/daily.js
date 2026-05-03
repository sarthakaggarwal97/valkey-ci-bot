/**
 * Daily CI page — public site variant.
 * Sections: header metrics, heatmap, WoW trends, recent runs, active campaigns,
 * curated agent activity feed.
 * Removed: signal cards, generated-at meta pill.
 */
import { el, fragment } from '../dom.js';
import {
  safeObj, safeList, safeInt, safeStr,
  formatNumber, formatRate, shortSha,
  commitUrl, runUrl, pullUrl,
} from '../utils.js';
import { chip } from '../components/chip.js';
import { metric, metricGrid } from '../components/metric.js';
import { panel } from '../components/panel.js';
import { table } from '../components/table.js';
import { externalLink } from '../components/link.js';
import { heatmap } from '../components/heatmap.js';
import { wowTrends } from '../components/wow-trends.js';

function workflowLabel(name) {
  let s = safeStr(name);
  if (s.endsWith('.yml') || s.endsWith('.yaml')) s = s.replace(/\.ya?ml$/, '');
  s = s.replace(/[-_]/g, ' ').trim();
  return s ? s.replace(/\b\w/g, (c) => c.toUpperCase()) : 'Unknown';
}

function headerMetrics(dashboard) {
  const daily = safeObj(dashboard.daily_health);
  const flaky = safeObj(dashboard.flaky_tests);
  const reports = safeList(daily.workflow_reports);
  const totalRuns = safeInt(daily.total_runs);
  const failedRuns = safeInt(daily.failed_runs);

  const metrics = [
    metric('Tracked days', safeInt(daily.days_with_runs), {
      note: safeInt(daily.days_with_runs) + '/' + safeList(daily.dates).length + ' with data',
    }),
    metric('Total runs', totalRuns, { note: 'Latest window' }),
    metric('Failed runs', failedRuns, {
      note: formatRate(failedRuns, totalRuns),
      tone: failedRuns ? 'bad' : 'good',
    }),
  ];

  if (reports.length > 1) {
    for (const r of reports) {
      const report = safeObj(r);
      const wfTotal = safeInt(report.total_runs);
      const wfFailed = safeInt(report.failed_runs);
      metrics.push(
        metric(workflowLabel(report.workflow) + ' failures', wfFailed, {
          note: formatRate(wfFailed, wfTotal),
          tone: wfFailed ? 'bad' : 'good',
        })
      );
    }
  } else {
    metrics.push(
      metric('Unique failures', safeInt(daily.unique_failures), {
        note: 'Distinct names',
        tone: safeInt(daily.unique_failures) ? 'warn' : 'good',
      })
    );
  }

  metrics.push(
    metric('Active campaigns', safeInt(flaky.active_campaigns), {
      note: 'Remediation loops',
    })
  );

  return metricGrid(metrics);
}

function runsTable(dashboard) {
  const daily = safeObj(dashboard.daily_health);
  const repo = safeStr(daily.repo) || 'valkey-io/valkey';
  const runs = safeList(daily.runs).map((r) => {
    const run = safeObj(r);
    return {
      date: safeStr(run.date),
      workflow: safeStr(run.workflow),
      status: safeStr(run.status),
      commit_sha: safeStr(run.commit_sha),
      full_sha: safeStr(run.full_sha),
      commit_message: safeStr(run.commit_message),
      unique_failures: safeInt(run.unique_failures),
      failed_jobs: safeInt(run.failed_jobs),
      failed_job_names: safeList(run.failed_job_names),
      run_id: safeStr(run.run_id),
      run_url: safeStr(run.run_url),
      commits_since_prev: safeList(run.commits_since_prev),
    };
  });

  if (!runs.length) {
    return el('p', { class: 'empty' }, ['No monitored runs in the current window.']);
  }

  return table(
    [
      { key: 'date', label: 'Date' },
      {
        key: 'workflow',
        label: 'Run type',
        render: (row) => chip(workflowLabel(row.workflow), { tone: 'info' }),
      },
      {
        key: 'status',
        label: 'Status',
        render: (row) => chip(row.status || 'unknown'),
      },
      {
        key: 'commit_sha',
        label: 'Commit',
        render: (row) => {
          const url = commitUrl(repo, row.full_sha || row.commit_sha);
          const label = shortSha(row.full_sha || row.commit_sha);
          const link = externalLink(label, url);
          if (row.commit_message && link instanceof HTMLElement) {
            link.setAttribute('title', row.commit_message);
          }
          return link;
        },
      },
      { key: 'unique_failures', label: 'Unique failures' },
      {
        key: 'failed_jobs',
        label: 'Failed jobs',
        render: (row) => {
          if (row.failed_job_names.length) {
            return row.failed_job_names.slice(0, 3).join(', ') +
              (row.failed_job_names.length > 3 ? ' +' + (row.failed_job_names.length - 3) : '');
          }
          return String(row.failed_jobs || 0);
        },
      },
      {
        key: 'commits_since_prev',
        label: 'Commits since prev',
        sortable: false,
        render: (row) => {
          const commits = row.commits_since_prev;
          if (!commits.length) return el('span', { class: 'empty-inline' }, ['\u2014']);
          const parts = commits.slice(0, 5).map((c) => {
            const cc = safeObj(c);
            const cSha = safeStr(cc.sha);
            const cMsg = safeStr(cc.message);
            const link = externalLink(shortSha(cSha), commitUrl(repo, cSha));
            if (cMsg && link instanceof HTMLElement) {
              link.setAttribute('title', cMsg);
            }
            return link;
          });
          const extras = commits.length > 5 ? ['+', String(commits.length - 5)] : [];
          return el('span', { class: 'commit-list' }, [
            ...parts.flatMap((p, i) => i ? [' ', p] : [p]),
            ...(extras.length ? [' ', el('span', { class: 'empty-inline' }, extras.join(''))] : []),
          ]);
        },
      },
      {
        key: 'run_url',
        label: 'Run',
        sortable: false,
        render: (row) => externalLink('run', row.run_url || runUrl(repo, row.run_id)),
      },
    ],
    runs,
    {
      filter: { enabled: true, placeholder: 'Filter runs by text…' },
      defaultSort: { key: 'date', direction: 'desc' },
      empty: 'No runs match the filter.',
    }
  );
}

function campaignsTable(dashboard) {
  const flaky = safeObj(dashboard.flaky_tests);
  const campaigns = safeList(flaky.recent_campaigns).map((c) => {
    const ca = safeObj(c);
    return {
      failure_identifier: safeStr(ca.failure_identifier),
      subsystem: safeStr(ca.subsystem),
      status: safeStr(ca.status),
      proof_status: safeStr(ca.proof_status),
      proof_url: safeStr(ca.proof_url),
      pr_url: safeStr(ca.pr_url),
      total_attempts: safeInt(ca.total_attempts),
      consecutive_full_passes: safeInt(ca.consecutive_full_passes),
      queued_pr_payload: ca.queued_pr_payload,
      updated_at: safeStr(ca.updated_at),
    };
  });

  if (!campaigns.length) {
    return el('p', { class: 'empty' }, ['No active flaky remediation campaigns.']);
  }

  return table(
    [
      { key: 'failure_identifier', label: 'Failure' },
      { key: 'subsystem', label: 'Subsystem' },
      { key: 'status', label: 'Status', render: (row) => chip(row.status) },
      {
        key: 'proof_status',
        label: 'Proof',
        render: (row) => {
          if (row.proof_url) return externalLink(row.proof_status || 'proof', row.proof_url);
          if (row.proof_status) return chip(row.proof_status);
          return 'n/a';
        },
      },
      { key: 'total_attempts', label: 'Attempts' },
      { key: 'consecutive_full_passes', label: 'Pass streak' },
      {
        key: 'pr_url',
        label: 'Draft/PR',
        sortable: false,
        render: (row) => {
          if (row.pr_url) return externalLink('PR', row.pr_url);
          if (row.queued_pr_payload && typeof row.queued_pr_payload === 'object') {
            return chip('queued', { tone: 'warn' });
          }
          return 'n/a';
        },
      },
      { key: 'updated_at', label: 'Updated' },
    ],
    campaigns,
    {
      filter: { enabled: true, placeholder: 'Filter campaigns by text…' },
      defaultSort: { key: 'updated_at', direction: 'desc' },
      empty: 'No campaigns match the filter.',
    }
  );
}

// --- Curated agent activity feed ---

const ACTIVITY_TYPES = {
  review_posted: 'Reviewed PR',
  issue_created: 'Filed issue',
  issue_updated: 'Updated issue',
  pr_opened: 'Opened PR',
  backport_pr_created: 'Opened backport PR',
  fuzzer_issue_created: 'Filed fuzzer issue',
  fuzzer_issue_updated: 'Updated fuzzer issue',
};

function eventSubjectLink(event, repoFallback) {
  const attributes = safeObj(event.attributes);
  const subject = safeStr(event.subject);
  for (const key of ['pr_url', 'issue_url', 'run_url', 'proof_url', 'url']) {
    const url = safeStr(attributes[key]);
    if (url) return externalLink(subject, url);
  }
  if (subject.startsWith('http://') || subject.startsWith('https://')) {
    return externalLink(subject, subject);
  }
  const prMatch = subject.match(/^([^#\s]+\/[^#\s]+)#(\d+)$/);
  if (prMatch) return externalLink(subject, pullUrl(prMatch[1], prMatch[2]));
  const shortPr = subject.match(/^#(\d+)$/);
  if (shortPr && repoFallback) return externalLink(subject, pullUrl(repoFallback, shortPr[1]));
  return el('span', {}, [subject]);
}

function agentActivityPanel(dashboard) {
  const outcomes = safeObj(dashboard.agent_outcomes);
  const daily = safeObj(dashboard.daily_health);
  const repoFallback = safeStr(daily.repo) || 'valkey-io/valkey';

  const items = safeList(outcomes.recent_events)
    .filter((e) => {
      const ev = safeObj(e);
      return ACTIVITY_TYPES[safeStr(ev.event_type)] !== undefined;
    })
    .slice(0, 15)
    .map((e) => {
      const ev = safeObj(e);
      return {
        created_at: safeStr(ev.created_at),
        event_type: safeStr(ev.event_type),
        activity: ACTIVITY_TYPES[safeStr(ev.event_type)] || safeStr(ev.event_type),
        subject: safeStr(ev.subject),
        attributes: ev.attributes,
      };
    });

  if (!items.length) {
    return el('p', { class: 'empty' }, ['No recent agent activity recorded.']);
  }

  return table(
    [
      { key: 'created_at', label: 'Time' },
      { key: 'activity', label: 'Activity', render: (row) => chip(row.activity, { tone: 'info' }) },
      {
        key: 'subject',
        label: 'Subject',
        render: (row) => eventSubjectLink(row, repoFallback),
      },
    ],
    items,
    { defaultSort: { key: 'created_at', direction: 'desc' } }
  );
}

export function render(container, dashboard, ctx = {}) {
  const wow = safeObj(dashboard.wow_trends);

  const hero = el('header', { class: 'hero' }, [
    el('div', { class: 'hero-row' }, [
      el('div', {}, [
        el('h2', {}, ['Daily CI']),
        el('p', {}, ['Recurring failures, recent commits, and active remediation loops.']),
      ]),
    ]),
  ]);

  container.replaceChildren(fragment([
    hero,
    headerMetrics(dashboard),
    panel({
      title: 'Failure heatmap',
      subtitle: 'Click a red cell to open the drill-down drawer. \u2190 / \u2192 move between cells.',
      body: heatmap(dashboard.daily_health),
      wide: true,
    }),
    panel({
      title: 'Week-over-Week trends',
      subtitle: 'Compares the last 7 days against the prior 7 days.',
      body: wowTrends(wow),
      wide: true,
    }),
    panel({
      title: 'Recent monitored runs',
      subtitle: 'Sort by date, filter by any column. Commits and run IDs resolve back to GitHub.',
      body: runsTable(dashboard),
      wide: true,
    }),
    panel({
      title: 'Active remediation campaigns',
      subtitle: 'Flaky tests the CI agent is chasing for Valkey.',
      anchor: 'campaigns',
      body: campaignsTable(dashboard),
      wide: true,
    }),
    panel({
      title: 'Recent agent activity',
      subtitle: 'PR reviews, issues filed, and PRs opened by the CI agent for Valkey.',
      body: agentActivityPanel(dashboard),
      wide: true,
    }),
  ]));

  if (ctx.sub === 'campaigns') {
    const target = document.getElementById('campaigns');
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}
