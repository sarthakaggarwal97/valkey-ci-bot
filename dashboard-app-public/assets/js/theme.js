/**
 * Theme manager — public site variant.
 * Status card shows Valkey-facing numbers instead of data age.
 */
import { el } from './dom.js';
import { safeStr, safeInt, safeObj, formatNumber } from './utils.js';

const STORAGE_KEY = 'valkey-dashboard-theme';

export function getTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'dark' || saved === 'light') return saved;
  } catch { /* localStorage blocked */ }
  return null;
}

export function setTheme(theme) {
  if (theme === 'dark' || theme === 'light') {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem(STORAGE_KEY, theme); } catch { /* ignore */ }
  } else {
    document.documentElement.removeAttribute('data-theme');
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
  }
}

function resolveCurrentTheme() {
  const saved = getTheme();
  if (saved) return saved;
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
    ? 'light' : 'dark';
}

function renderToggleButton() {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;
  if (document.getElementById('theme-toggle')) return;

  const current = resolveCurrentTheme();
  const btn = el('button', {
    type: 'button',
    id: 'theme-toggle',
    class: 'theme-toggle',
    'aria-label': 'Toggle theme (currently ' + current + ')',
    title: 'Toggle theme',
  }, [
    el('span', { class: 'theme-toggle-icon', 'aria-hidden': 'true' }, [current === 'light' ? '\u263E' : '\u2600']),
    el('span', {}, [current === 'light' ? 'Dark mode' : 'Light mode']),
  ]);
  btn.addEventListener('click', () => {
    const now = resolveCurrentTheme();
    setTheme(now === 'light' ? 'dark' : 'light');
    btn.replaceChildren(
      el('span', { class: 'theme-toggle-icon', 'aria-hidden': 'true' },
        [now === 'light' ? '\u2600' : '\u263E']),
      el('span', {}, [now === 'light' ? 'Light mode' : 'Dark mode']),
    );
  });
  sidebar.appendChild(btn);
}

function renderShareButton() {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;
  if (document.getElementById('share-button')) return;

  const btn = el('button', {
    type: 'button',
    id: 'share-button',
    class: 'share-button',
    'aria-label': 'Copy current view URL to clipboard',
  }, [
    el('span', { 'aria-hidden': 'true' }, ['\uD83D\uDD17']),
    el('span', {}, ['Share view']),
  ]);
  const status = el('span', { class: 'share-status', role: 'status', 'aria-live': 'polite' }, []);
  btn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      status.replaceChildren(document.createTextNode('Copied!'));
    } catch {
      status.replaceChildren(document.createTextNode('Copy failed'));
    }
    setTimeout(() => status.replaceChildren(), 2000);
  });
  sidebar.appendChild(btn);
  sidebar.appendChild(status);
}

function renderStatusCard(dashboard) {
  const card = document.getElementById('status-card');
  if (!card) return;
  const value = document.getElementById('status-value');
  const detail = document.getElementById('status-detail');
  if (!value || !detail) return;

  const daily = safeObj(dashboard.daily_health);
  const flaky = safeObj(dashboard.flaky_tests);
  const fuzzer = safeObj(dashboard.fuzzer);
  const issueActions = safeObj(fuzzer.issue_action_counts);

  const failedRuns = safeInt(daily.failed_runs);
  const activeCampaigns = safeInt(flaky.active_campaigns);
  const fuzzerIssues = safeInt(issueActions.created) + safeInt(issueActions.updated);

  const tone = failedRuns > 0 ? 'bad' : 'good';
  card.setAttribute('data-tone', tone);
  value.replaceChildren(document.createTextNode(
    formatNumber(failedRuns) + ' failed run' + (failedRuns === 1 ? '' : 's')
  ));
  detail.replaceChildren(document.createTextNode(
    formatNumber(activeCampaigns) + ' active flaky \u00B7 ' +
    formatNumber(fuzzerIssues) + ' fuzzer issue' + (fuzzerIssues === 1 ? '' : 's')
  ));
}

export function init(dashboard) {
  const saved = getTheme();
  if (saved) {
    document.documentElement.setAttribute('data-theme', saved);
  }
  renderToggleButton();
  renderShareButton();
  if (dashboard) {
    renderStatusCard(dashboard);
  }
}
