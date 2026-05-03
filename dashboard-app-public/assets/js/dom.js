/**
 * XSS-safe DOM builder. All string children become text nodes — never innerHTML.
 * This is the ONLY way the app creates DOM elements.
 *
 * Usage:
 *   el('div', { class: 'panel' }, [
 *     el('h2', {}, ['Title']),
 *     el('p', {}, ['Some ', el('strong', {}, ['bold']), ' text']),
 *   ])
 */

export function el(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'dataset') {
      for (const [dk, dv] of Object.entries(value)) {
        element.dataset[dk] = dv;
      }
    } else if (key.startsWith('on') && typeof value === 'function') {
      element.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value !== null && value !== undefined && value !== false) {
      element.setAttribute(key, String(value));
    }
  }
  for (const child of children) {
    if (child == null) continue;
    if (child instanceof Node) {
      element.appendChild(child);
    } else {
      element.appendChild(document.createTextNode(String(child)));
    }
  }
  return element;
}

export function text(str) {
  return document.createTextNode(String(str ?? ''));
}

export function fragment(children = []) {
  const frag = document.createDocumentFragment();
  for (const child of children) {
    if (child == null) continue;
    frag.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return frag;
}
