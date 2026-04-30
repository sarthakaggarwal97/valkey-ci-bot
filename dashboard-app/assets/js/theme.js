/**
 * Theme manager + sidebar UX polish.
 * - Reads localStorage
 * - Applies data-theme attribute
 * - Renders a theme toggle button in the sidebar
 * - Renders a data-age indicator in the status card
 * - Wires a share button that copies the current hash URL
 */
import { el } from './dom.js';
import { safeStr, timeAgo } from './utils.js';

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
  // Avoid duplicate inserts on re-init
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

  const generatedAt = safeStr(dashboard.generated_at);
  if (!generatedAt) {
    value.replaceChildren(document.createTextNode('No data'));
    detail.replaceChildren(document.createTextNode('dashboard.json missing generated_at'));
    return;
  }
  const ms = Date.now() - new Date(generatedAt).getTime();
  const hours = ms / (1000 * 60 * 60);
  let tone = 'good';
  if (hours > 12) tone = 'bad';
  else if (hours > 6) tone = 'warn';

  card.setAttribute('data-tone', tone);
  value.replaceChildren(document.createTextNode(timeAgo(generatedAt)));
  detail.replaceChildren(document.createTextNode(
    'Generated ' + generatedAt.slice(0, 16).replace('T', ' ')
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
