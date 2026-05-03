/** Link builders — safe external links with rel="noreferrer". */
import { el } from '../dom.js';
import { safeStr } from '../utils.js';

export function link(label, url, { compact = false } = {}) {
  const u = safeStr(url);
  const text = label == null ? '' : String(label);
  if (!u) {
    return el('span', {}, [text]);
  }
  const cls = compact ? 'link link-compact' : 'link';
  return el('a', { class: cls, href: u }, [text]);
}

export function externalLink(label, url) {
  const u = safeStr(url);
  const text = label == null ? '' : String(label);
  if (!u) {
    return el('span', {}, [text]);
  }
  return el('a', {
    class: 'link',
    href: u,
    target: '_blank',
    rel: 'noreferrer noopener',
  }, [text]);
}
