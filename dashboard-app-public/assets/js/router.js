/**
 * Hash-based SPA router — public site variant.
 * Routes: #/daily (default), #/prs, #/fuzzer
 * Diagnostics is intentionally excluded from the public site.
 */
import { el } from './dom.js';
import * as daily from './pages/daily.js';
import * as prs from './pages/prs.js';
import * as fuzzer from './pages/fuzzer.js';

const ROUTES = {
  daily: { module: daily, label: 'Daily CI' },
  prs: { module: prs, label: 'PRs' },
  fuzzer: { module: fuzzer, label: 'Fuzzer' },
};
const DEFAULT_ROUTE = 'daily';

let currentDashboard = null;

export function parseHash(hash) {
  const raw = (hash || '').replace(/^#?\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const segments = pathPart.split('/').filter(Boolean);
  const page = segments[0] || DEFAULT_ROUTE;
  const sub = segments[1] || '';
  const params = new URLSearchParams(queryPart || '');
  return { page, sub, params };
}

export function buildHash(page, sub, params) {
  let hash = '#/' + page;
  if (sub) hash += '/' + sub;
  if (params) {
    const qs = params.toString();
    if (qs) hash += '?' + qs;
  }
  return hash;
}

export function navigate(hash) {
  if (window.location.hash === hash) {
    handleRoute();
  } else {
    window.location.hash = hash;
  }
}

function updateNav(page) {
  const links = document.querySelectorAll('[data-nav-link]');
  links.forEach((link) => {
    const linkPage = link.getAttribute('data-nav-link');
    if (linkPage === page) {
      link.classList.add('nav-link-active');
      link.setAttribute('aria-current', 'page');
    } else {
      link.classList.remove('nav-link-active');
      link.removeAttribute('aria-current');
    }
  });
}

function handleRoute() {
  const { page, sub, params } = parseHash(window.location.hash);
  const route = ROUTES[page] || ROUTES[DEFAULT_ROUTE];
  const resolvedPage = ROUTES[page] ? page : DEFAULT_ROUTE;

  updateNav(resolvedPage);

  const app = document.getElementById('app');
  if (!app) return;

  try {
    route.module.render(app, currentDashboard, { sub, params });
    if (sub) {
      const target = document.getElementById(sub);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
  } catch (err) {
    app.replaceChildren(
      el('div', { class: 'error-banner', role: 'alert' }, [
        el('strong', {}, ['Page render error']),
        el('p', {}, [err && err.message ? err.message : String(err)]),
      ])
    );
    console.error('render error', err);
  }
}

export function init(dashboard) {
  currentDashboard = dashboard;
  window.addEventListener('hashchange', handleRoute);
  handleRoute();
}
