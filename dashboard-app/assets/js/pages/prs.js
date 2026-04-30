/**
 * PRs page.
 * Sections: header metrics, tracked PRs, replay review cases, workflow contract cases.
 */
import { el, fragment } from '../dom.js';
import {
  safeObj, safeList, safeInt, safeStr,
  formatNumber, shortSha, truncate,
  pullUrl, commitUrl, issueCommentUrl,
} from '../utils.js';
import { chip } from '../components/chip.js';
import { metric, metricGrid } from '../components/metric.js';
import { panel } from '../components/panel.js';
import { table } from '../components/table.js';
import { externalLink } from '../components/link.js';

function reviewCommentUrl(repo, pr) {
  const r = safeStr(repo);
  const p = safeStr(pr);
  return r && p ? 'https://github.com/' + r + '/pull/' + p : '';
}

function headerMetrics(dashboard) {
  const reviews = safeObj(dashboard.pr_reviews);
  const acceptance = safeObj(dashboard.acceptance);
  const replayFailed = safeInt(acceptance.review_failed);
  return metricGrid([
    metric('Tracked PRs', safeInt(reviews.tracked_prs), { note: 'With durable review state' }),
    metric('Review comments', safeInt(reviews.review_comments), { note: 'Persisted comment IDs' }),
    metric('Coverage gaps', safeInt(reviews.coverage_incomplete_cases), {
      note: 'Incomplete coverage',
      tone: safeInt(reviews.coverage_incomplete_cases) ? 'warn' : 'good',
    }),
    metric('Replay failures', replayFailed, {
      note: formatNumber(safeInt(acceptance.review_cases)) + ' review cases',
      tone: replayFailed ? 'bad' : 'good',
    }),
    metric('Findings', safeInt(acceptance.finding_count), { note: 'Replay findings recorded' }),
  ]);
}

function trackedReviewsTable(dashboard) {
  const reviews = safeObj(dashboard.pr_reviews);
  const items = safeList(reviews.recent_reviews).map((r) => {
    const rev = safeObj(r);
    return {
      repo: safeStr(rev.repo),
      pr_number: safeStr(rev.pr_number),
      last_reviewed_head_sha: safeStr(rev.last_reviewed_head_sha),
      summary_comment_id: safeStr(rev.summary_comment_id),
      review_comment_ids: safeList(rev.review_comment_ids),
      updated_at: safeStr(rev.updated_at),
    };
  });

  if (!items.length) {
    return el('p', { class: 'empty' }, ['No tracked PR review state.']);
  }

  return table(
    [
      {
        key: 'pr_number',
        label: 'PR',
        render: (row) =>
          externalLink(
            row.repo + '#' + row.pr_number,
            pullUrl(row.repo, row.pr_number)
          ),
      },
      {
        key: 'last_reviewed_head_sha',
        label: 'Head',
        render: (row) =>
          externalLink(
            shortSha(row.last_reviewed_head_sha),
            commitUrl(row.repo, row.last_reviewed_head_sha)
          ),
      },
      {
        key: 'summary_comment_id',
        label: 'Summary',
        render: (row) => {
          if (!row.summary_comment_id) return 'n/a';
          return externalLink(
            row.summary_comment_id,
            issueCommentUrl(row.repo, row.pr_number, row.summary_comment_id)
          );
        },
      },
      {
        key: 'review_comment_ids',
        label: 'Review notes',
        sortValue: (row) => row.review_comment_ids.length,
        render: (row) =>
          row.review_comment_ids.length
            ? externalLink(
                formatNumber(row.review_comment_ids.length),
                reviewCommentUrl(row.repo, row.pr_number)
              )
            : '0',
      },
      { key: 'updated_at', label: 'Updated' },
    ],
    items,
    {
      filter: { enabled: true, placeholder: 'Filter reviews…' },
      defaultSort: { key: 'updated_at', direction: 'desc' },
      empty: 'No reviews match the filter.',
    }
  );
}

function replayReviewTable(dashboard) {
  const acceptance = safeObj(dashboard.acceptance);
  const items = safeList(acceptance.recent_review_results).map((r) => {
    const result = safeObj(r);
    const coverage = safeObj(result.coverage);
    const claimed = safeList(coverage.claimed_without_tool);
    const unaccounted = safeList(coverage.unaccounted_files);
    const coverageComplete = !claimed.length && !unaccounted.length && !coverage.fetch_limit_hit;
    return {
      name: safeStr(result.name),
      repo: safeStr(result.repo) || 'valkey-io/valkey',
      pr_number: safeStr(result.pr_number),
      passed: Boolean(result.passed),
      coverage_complete: Boolean(coverage) && coverageComplete,
      coverage_present: Boolean(coverage) && Object.keys(coverage).length > 0,
      findings: safeList(result.findings).length,
      model_followups: safeList(result.model_followups),
    };
  });

  if (!items.length) {
    return el('p', { class: 'empty' }, ['No replay review results.']);
  }

  return table(
    [
      { key: 'name', label: 'Case' },
      {
        key: 'pr_number',
        label: 'PR',
        render: (row) => row.pr_number ? externalLink(row.pr_number, pullUrl(row.repo, row.pr_number)) : 'n/a',
      },
      {
        key: 'passed',
        label: 'Verdict',
        render: (row) => chip(row.passed ? 'pass' : 'needs follow-up'),
      },
      {
        key: 'coverage_complete',
        label: 'Coverage',
        render: (row) => {
          if (!row.coverage_present) return chip('missing', { tone: 'bad' });
          return chip(row.coverage_complete ? 'covered' : 'incomplete', {
            tone: row.coverage_complete ? 'good' : 'warn',
          });
        },
      },
      { key: 'findings', label: 'Findings' },
      {
        key: 'model_followups',
        label: 'Follow-ups',
        sortValue: (row) => row.model_followups.length,
        render: (row) => row.model_followups.length ? row.model_followups.join(', ') : 'none',
      },
    ],
    items,
    {
      filter: { enabled: true, placeholder: 'Filter replay cases…' },
      empty: 'No replay cases match the filter.',
    }
  );
}

function workflowContractTable(dashboard) {
  const acceptance = safeObj(dashboard.acceptance);
  const items = safeList(acceptance.recent_workflow_results).map((r) => {
    const result = safeObj(r);
    return {
      name: safeStr(result.name),
      workflow_path: safeStr(result.workflow_path),
      passed: Boolean(result.passed),
      checks: safeList(result.checks).length,
      notes: safeStr(result.notes),
    };
  });

  if (!items.length) {
    return el('p', { class: 'empty' }, ['No workflow contract cases supplied.']);
  }

  return table(
    [
      { key: 'name', label: 'Case' },
      { key: 'workflow_path', label: 'Workflow' },
      {
        key: 'passed',
        label: 'Verdict',
        render: (row) => chip(row.passed ? 'pass' : 'needs follow-up'),
      },
      { key: 'checks', label: 'Checks' },
      {
        key: 'notes',
        label: 'Notes',
        sortable: false,
        render: (row) => truncate(row.notes, 100),
      },
    ],
    items,
    { empty: 'No workflow cases.' }
  );
}

export function render(container, dashboard, ctx = {}) {
  const acceptance = safeObj(dashboard.acceptance);
  const readiness = safeStr(acceptance.readiness) || 'unknown';

  const hero = el('header', { class: 'hero' }, [
    el('div', { class: 'hero-row' }, [
      el('div', {}, [
        el('h2', {}, ['Pull Requests']),
        el('p', {}, ['Tracked review state, replay evidence, and workflow contract checks.']),
      ]),
      el('div', { class: 'hero-meta' }, [
        el('span', { class: 'meta-pill' }, [
          el('strong', {}, ['Readiness']),
          el('span', {}, [' ', chip(readiness)]),
        ]),
      ]),
    ]),
  ]);

  container.replaceChildren(fragment([
    hero,
    headerMetrics(dashboard),
    panel({
      title: 'Tracked pull requests',
      subtitle: 'PRs, commits, and review comments resolve back to GitHub.',
      body: trackedReviewsTable(dashboard),
      wide: true,
    }),
    panel({
      title: 'Replay review cases',
      subtitle: 'Acceptance-harness replay against real PRs.',
      anchor: 'replay',
      body: replayReviewTable(dashboard),
      wide: true,
    }),
    panel({
      title: 'Workflow contract cases',
      subtitle: 'Workflow-level acceptance checks.',
      body: workflowContractTable(dashboard),
      wide: true,
    }),
  ]));

  if (ctx.sub === 'replay') {
    const target = document.getElementById('replay');
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}
