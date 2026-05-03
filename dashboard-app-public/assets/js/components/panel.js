/** Panel: titled section with optional subtitle, anchor id, and wide layout. */
import { el } from '../dom.js';

export function panel({ title, subtitle = '', body, anchor = '', wide = false }) {
  const attrs = { class: wide ? 'panel panel-wide' : 'panel' };
  if (anchor) attrs.id = anchor;

  const head = el('div', { class: 'panel-head' }, [
    el('h2', {}, [title]),
    subtitle ? el('p', { class: 'panel-subtitle' }, [subtitle]) : null,
  ]);

  return el('section', attrs, [head, body]);
}
