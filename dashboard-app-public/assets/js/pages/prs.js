/**
 * PRs page — public site variant.
 * Shows only tracked PR reviews. Agent-internal panels removed.
 */
import { el, fragment } from '../dom.js';
import {
  safeObj, safeList, safeInt, safeStr,
  formatNumber, shortSha,
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
  return metricGrid([
    metric('Tracked PRs', safeInt(reviews.tracked_prs), { note: 'With durable review state' }),
    metric('Review comments', safeInt(reviews.review_comments), { note: 'Persisted comment IDs' }),
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

export function render(container, dashboard) {
  const hero = el('header', { class: 'hero' }, [
    el('div', { class: 'hero-row' }, [
      el('div', {}, [
        el('h2', {}, ['PR Reviews']),
        el('p', {}, ['Pull requests reviewed by the CI agent for Valkey.']),
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
  ]));
}
