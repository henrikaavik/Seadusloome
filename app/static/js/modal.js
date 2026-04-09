/* Seadusloome — Modal behavior (focus trap, Esc/backdrop close, focus restore).
   Accessibility baseline: NFR §10.2. Exposes window.Modal for HTMX integration. */
(function () {
  'use strict';

  var FOCUSABLE = [
    'a[href]', 'button:not([disabled])', 'textarea:not([disabled])',
    'input:not([disabled]):not([type="hidden"])', 'select:not([disabled])',
    '[tabindex]:not([tabindex="-1"])'
  ].join(',');

  var openStack = [];

  function getFocusable(root) {
    return Array.prototype.slice.call(root.querySelectorAll(FOCUSABLE))
      .filter(function (el) { return el.offsetParent !== null || el === document.activeElement; });
  }

  function trapTab(evt, root) {
    if (evt.key !== 'Tab') return;
    var items = getFocusable(root);
    if (!items.length) { evt.preventDefault(); return; }
    var first = items[0], last = items[items.length - 1];
    var active = document.activeElement;
    if (evt.shiftKey && active === first) { evt.preventDefault(); last.focus(); }
    else if (!evt.shiftKey && active === last) { evt.preventDefault(); first.focus(); }
  }

  function onKeydown(evt) {
    var top = openStack[openStack.length - 1];
    if (!top) return;
    if (evt.key === 'Escape' && top.dismissible) { evt.preventDefault(); close(top.id); }
    else trapTab(evt, top.root);
  }

  function onClick(evt) {
    var top = openStack[openStack.length - 1];
    if (!top) return;
    var t = evt.target;
    if (!(t instanceof Element)) return;
    if (t.closest('[data-modal-close]') && top.dismissible) { close(top.id); }
  }

  function open(id) {
    var root = document.getElementById(id);
    if (!root) return;
    var dialog = root.querySelector('[role="dialog"]');
    if (!dialog) return;
    var dismissible = root.getAttribute('data-modal-dismissible') !== 'false';
    var entry = { id: id, root: root, dialog: dialog, dismissible: dismissible,
                  trigger: document.activeElement };
    root.removeAttribute('hidden');
    document.body.style.overflow = 'hidden';
    openStack.push(entry);
    var first = getFocusable(dialog)[0] || dialog;
    first.focus();
  }

  function close(id) {
    var idx = -1;
    for (var i = openStack.length - 1; i >= 0; i--) {
      if (openStack[i].id === id) { idx = i; break; }
    }
    if (idx === -1) return;
    var entry = openStack.splice(idx, 1)[0];
    entry.root.setAttribute('hidden', '');
    if (!openStack.length) document.body.style.overflow = '';
    if (entry.trigger && typeof entry.trigger.focus === 'function') entry.trigger.focus();
  }

  document.addEventListener('keydown', onKeydown);
  document.addEventListener('click', onClick);

  window.Modal = { open: open, close: close };
})();
