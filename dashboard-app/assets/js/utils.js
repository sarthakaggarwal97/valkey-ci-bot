/** Pure formatting and URL helpers — no DOM. */

export function safeStr(v, fallback = '') {
  if (v == null) return fallback;
  return String(v);
}

export function safeInt(v) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : 0;
}

export function safeList(v) {
  return Array.isArray(v) ? v : [];
}

export function safeObj(v) {
  return v && typeof v === 'object' && !Array.isArray(v) ? v : {};
}

export function formatNumber(v) {
  try {
    const n = Number(v);
    if (!Number.isFinite(n)) return safeStr(v);
    return n.toLocaleString('en-US');
  } catch { return safeStr(v); }
}

export function formatPercent(v) {
  try {
    return Math.round(Number(v) * 100) + '%';
  } catch { return safeStr(v); }
}

export function formatRate(num, den) {
  const d = safeInt(den);
  if (d <= 0) return 'n/a';
  const n = safeInt(num);
  return n + '/' + d + ' (' + Math.round((n / d) * 100) + '%)';
}

export function timeAgo(isoString) {
  if (!isoString) return '';
  try {
    const ms = Date.now() - new Date(isoString).getTime();
    if (ms < 0) return 'just now';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return sec + 's ago';
    const min = Math.floor(sec / 60);
    if (min < 60) return min + 'm ago';
    const hr = Math.floor(min / 60);
    if (hr < 24) return hr + 'h ago';
    const days = Math.floor(hr / 24);
    return days + 'd ago';
  } catch { return ''; }
}

export function shortSha(sha) {
  return safeStr(sha).slice(0, 7);
}

export function truncate(str, limit = 96) {
  const s = safeStr(str);
  return s.length <= limit ? s : s.slice(0, limit - 1) + '\u2026';
}

// --- GitHub URL builders ---

export function repoUrl(repo) {
  const r = safeStr(repo);
  return r && r.includes('/') ? 'https://github.com/' + r : '';
}

export function commitUrl(repo, sha) {
  const r = safeStr(repo), s = safeStr(sha);
  return r && s ? 'https://github.com/' + r + '/commit/' + s : '';
}

export function pullUrl(repo, pr) {
  const r = safeStr(repo), p = safeStr(pr);
  return r && p ? 'https://github.com/' + r + '/pull/' + p : '';
}

export function runUrl(repo, runId) {
  const r = safeStr(repo), id = safeStr(runId);
  return r && id ? 'https://github.com/' + r + '/actions/runs/' + id : '';
}

export function jobUrl(repo, runId, jobId) {
  const r = safeStr(repo), rid = safeStr(runId), jid = safeStr(jobId);
  return r && rid && jid ? 'https://github.com/' + r + '/actions/runs/' + rid + '/job/' + jid : '';
}

export function issueCommentUrl(repo, pr, commentId) {
  const r = safeStr(repo), p = safeStr(pr), c = safeStr(commentId);
  return r && p && c ? 'https://github.com/' + r + '/pull/' + p + '#issuecomment-' + c : '';
}

// --- Status tone mapping ---

const GOOD_WORDS = ['success', 'pass', 'ready', 'merged', 'normal', 'available', 'covered'];
const BAD_WORDS = ['fail', 'error', 'dead', 'abandoned', 'anomalous', 'missing', 'critical', 'blocked', 'cancelled'];
const WARN_WORDS = ['warning', 'queued', 'retry', 'incomplete', 'needs', 'pending', 'partial', 'skipped', 'in_progress'];

export function toneForStatus(label) {
  const s = safeStr(label).toLowerCase();
  if (GOOD_WORDS.some(w => s.includes(w))) return 'good';
  if (BAD_WORDS.some(w => s.includes(w))) return 'bad';
  if (WARN_WORDS.some(w => s.includes(w))) return 'warn';
  return 'info';
}
