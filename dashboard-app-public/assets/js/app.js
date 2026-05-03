/**
 * Valkey CI Agent Dashboard — entry point.
 * Fetches dashboard.json, validates schema, and hands off to the router.
 */
import { el } from './dom.js';
import { init as initRouter } from './router.js';
import { init as initTheme } from './theme.js';

const EXPECTED_SCHEMA = 1;
const DATA_PATH = 'data/dashboard.json';

function showError(title, detail) {
  const app = document.getElementById('app');
  if (!app) return;
  app.replaceChildren(
    el('div', { class: 'error-banner', role: 'alert' }, [
      el('strong', {}, [title]),
      el('p', {}, [detail]),
    ])
  );
}

async function boot() {
  // Init theme before fetch so dark/light is correct during loading.
  initTheme();

  let dashboard;
  try {
    const res = await fetch(DATA_PATH, { cache: 'no-cache' });
    if (!res.ok) {
      showError(
        'Unable to load dashboard data',
        'Request for ' + DATA_PATH + ' failed with status ' + res.status + '.'
      );
      return;
    }
    dashboard = await res.json();
  } catch (err) {
    showError(
      'Unable to load dashboard data',
      'The dashboard JSON could not be fetched. Open the raw file at ' + DATA_PATH +
      ' to inspect. Error: ' + (err && err.message ? err.message : String(err))
    );
    return;
  }

  const sv = dashboard && dashboard.schema_version;
  if (sv !== EXPECTED_SCHEMA) {
    showError(
      'Unsupported dashboard schema',
      'Expected schema_version ' + EXPECTED_SCHEMA + ', got ' + (sv ?? 'none') +
      '. The dashboard site may be newer or older than the data source.'
    );
    return;
  }

  initRouter(dashboard);
  // Re-init theme with dashboard so the status card + share button get populated.
  initTheme(dashboard);
}

boot();
