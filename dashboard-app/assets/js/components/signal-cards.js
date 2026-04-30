/**
 * Signal-map cards for the landing page.
 * Compact page-navigation cards with a title, description, and 2 key stats each.
 */
import { el } from '../dom.js';
import { safeObj, safeInt, formatNumber } from '../utils.js';

function card({ title, href, description, stats }) {
  return el('a', { class: 'signal-card', href }, [
    el('div', { class: 'signal-card-head' }, [
      el('h3', {}, [title]),
      el('span', { class: 'signal-card-arrow' }, ['Open \u2192']),
    ]),
    el('p', {}, [description]),
    el('ul', { class: 'signal-card-stats' }, stats.map(([label, value]) =>
      el('li', {}, [
        el('span', {}, [label]),
        el('strong', {}, [String(value)]),
      ])
    )),
  ]);
}

export function signalCards(dashboard) {
  const snapshot = safeObj(dashboard.snapshot);
  const daily = safeObj(dashboard.daily_health);
  const reviews = safeObj(dashboard.pr_reviews);
  const acceptance = safeObj(dashboard.acceptance);
  const fuzzer = safeObj(dashboard.fuzzer);
  const outcomes = safeObj(dashboard.agent_outcomes);
  const health = safeObj(dashboard.state_health);

  return el('section', { class: 'signal-cards', 'aria-label': 'Dashboard sections' }, [
    card({
      title: 'Daily CI',
      href: '#/daily',
      description: 'Failures, runs, and remediation loops.',
      stats: [
        ['Runs', formatNumber(safeInt(daily.total_runs))],
        ['Failures', formatNumber(safeInt(daily.failed_runs))],
      ],
    }),
    card({
      title: 'PRs',
      href: '#/prs',
      description: 'Review coverage and replay evidence.',
      stats: [
        ['Tracked', formatNumber(safeInt(reviews.tracked_prs))],
        ['Replay', formatNumber(safeInt(acceptance.review_cases))],
      ],
    }),
    card({
      title: 'Fuzzer',
      href: '#/fuzzer',
      description: 'Anomalies, seeds, and root causes.',
      stats: [
        ['Analyzed', formatNumber(safeInt(snapshot.fuzzer_runs_analyzed))],
        ['Anomalous', formatNumber(safeInt(safeObj(fuzzer.status_counts).anomalous))],
      ],
    }),
    card({
      title: 'Diagnostics',
      href: '#/diagnostics',
      description: 'Events, watermarks, and AI reliability.',
      stats: [
        ['Events', formatNumber(safeInt(outcomes.events))],
        ['Warnings', formatNumber(safeList(health.input_warnings).length)],
      ],
    }),
  ]);
}

function safeList(v) {
  return Array.isArray(v) ? v : [];
}
