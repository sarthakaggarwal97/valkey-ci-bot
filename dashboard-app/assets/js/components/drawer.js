/**
 * Side drawer for drill-down views.
 *
 * Usage:
 *   const d = openDrawer({ title: '...', body: node, onClose: () => {...} });
 *   d.close();
 *
 * Features:
 *   - slides in from the right (respects prefers-reduced-motion)
 *   - ESC and overlay click close
 *   - focus trap while open, focus returns to trigger on close
 */
import { el } from '../dom.js';

let activeDrawer = null;

export function openDrawer({ title, body, onClose } = {}) {
  // Close any existing drawer first.
  if (activeDrawer) activeDrawer.close();

  const triggerEl = document.activeElement;
  const closeBtn = el('button', {
    type: 'button',
    class: 'drawer-close',
    'aria-label': 'Close',
  }, ['\u00D7']);

  const titleEl = el('h2', { id: 'drawer-title', class: 'drawer-title' }, [title || '']);
  const bodyEl = el('div', { class: 'drawer-body' }, [body || '']);

  const panel = el('aside', {
    class: 'drawer',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': 'drawer-title',
    tabindex: '-1',
  }, [
    el('header', { class: 'drawer-head' }, [titleEl, closeBtn]),
    bodyEl,
  ]);

  const overlay = el('div', { class: 'drawer-overlay', 'aria-hidden': 'true' }, []);
  const host = el('div', { class: 'drawer-host' }, [overlay, panel]);
  document.body.appendChild(host);

  function getFocusable() {
    return panel.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
  }

  function trapFocus(ev) {
    if (ev.key !== 'Tab') return;
    const focusables = getFocusable();
    if (!focusables.length) {
      ev.preventDefault();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  function handleKey(ev) {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      close();
    } else {
      trapFocus(ev);
    }
  }

  function close() {
    document.removeEventListener('keydown', handleKey);
    host.classList.add('drawer-host-closing');
    // brief fadeout then remove
    setTimeout(() => {
      host.remove();
      if (triggerEl && typeof triggerEl.focus === 'function') {
        triggerEl.focus();
      }
    }, 120);
    activeDrawer = null;
    if (typeof onClose === 'function') onClose();
  }

  overlay.addEventListener('click', close);
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', handleKey);

  // Open animation — enter "open" state on next frame
  requestAnimationFrame(() => {
    host.classList.add('drawer-host-open');
    // Focus the first interactive element or the close button
    const focusables = getFocusable();
    (focusables[0] || closeBtn).focus();
  });

  const controller = { close, panel, bodyEl, titleEl };
  activeDrawer = controller;
  return controller;
}
