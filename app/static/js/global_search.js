/* Global search bar behaviour (B1 — epic #784).
 *
 * Responsibilities (kept in vanilla JS — no framework):
 *   1. Cmd+K / Ctrl+K focuses the bar (no modal opens).
 *      On viewports ≤768px the inline bar does not exist; the shortcut
 *      instead navigates to the full-screen /search page, which then
 *      auto-focuses its own input (server-rendered with `autofocus`).
 *   2. Open/close the dropdown when the htmx swap delivers content,
 *      including aria-expanded sync and an outside-click closer.
 *   3. Keyboard navigation through the dropdown rows
 *      (ArrowDown / ArrowUp / Enter / Escape).
 *   4. Write the count summary into the sr-only aria-live region so
 *      screen-reader users hear "3 entiteeti, 2 tegevust" updates.
 *
 * No external dependencies; runs at the end of <body> via PageShell.
 */
(function () {
  'use strict';

  var MOBILE_BREAKPOINT = 768;

  function isMobile() {
    return window.matchMedia('(max-width: ' + MOBILE_BREAKPOINT + 'px)').matches;
  }

  /* ------------------------------------------------------------------ */
  /* Cmd+K / Ctrl+K — focus the inline bar (desktop/tablet) or          */
  /* navigate to /search (mobile, where the bar isn't rendered).         */
  /* ------------------------------------------------------------------ */
  function bindShortcut() {
    document.addEventListener('keydown', function (e) {
      var isK = e.key === 'k' || e.key === 'K';
      var withMod = e.metaKey || e.ctrlKey;
      if (!isK || !withMod) return;
      var input = document.getElementById('global-search-input');
      // Don't hijack the shortcut from text inputs the user is already typing in,
      // unless that input *is* our global search input (then we just select-all).
      if (input && document.activeElement === input) {
        e.preventDefault();
        input.select();
        return;
      }
      // On mobile the inline bar is hidden — open the dedicated page.
      if (isMobile() || !input) {
        if (!input) {
          // Inline bar missing entirely (e.g. unauth pages) — bail.
          return;
        }
        e.preventDefault();
        window.location.href = '/search';
        return;
      }
      e.preventDefault();
      input.focus();
      input.select();
    });
  }

  /* ------------------------------------------------------------------ */
  /* Dropdown open/close + ARIA sync.                                    */
  /* ------------------------------------------------------------------ */
  function findContainer(el) {
    return el && el.closest ? el.closest('.global-search') : null;
  }

  function openDropdown(container) {
    if (!container) return;
    container.classList.add('global-search--open');
    container.setAttribute('aria-expanded', 'true');
    var input = container.querySelector('.global-search-input');
    if (input) input.setAttribute('aria-expanded', 'true');
  }

  function closeDropdown(container) {
    if (!container) return;
    container.classList.remove('global-search--open');
    container.setAttribute('aria-expanded', 'false');
    var input = container.querySelector('.global-search-input');
    if (input) input.setAttribute('aria-expanded', 'false');
    var rows = container.querySelectorAll('.global-search-row');
    rows.forEach(function (r) { r.classList.remove('global-search-row--active'); });
  }

  function bindAfterSwap() {
    document.body.addEventListener('htmx:afterSwap', function (e) {
      var target = e.target;
      if (!target || !target.id || target.id.indexOf('-results') === -1) return;
      var container = findContainer(target);
      if (!container) return;

      // Mirror the summary into the sr-only live region.
      var summaryEl = target.querySelector('.global-search-summary');
      var status = container.querySelector('[aria-live="polite"]');
      if (status) {
        status.textContent = summaryEl ? (summaryEl.getAttribute('data-summary') || '') : '';
      }

      var hasRows = target.querySelector('.global-search-row, .global-search-empty');
      if (hasRows) {
        openDropdown(container);
      } else {
        closeDropdown(container);
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Outside-click closes the dropdown.                                  */
  /* ------------------------------------------------------------------ */
  function bindOutsideClick() {
    document.addEventListener('click', function (e) {
      var openOnes = document.querySelectorAll('.global-search.global-search--open');
      openOnes.forEach(function (c) {
        if (!c.contains(e.target)) closeDropdown(c);
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* Keyboard navigation through rows.                                   */
  /* ------------------------------------------------------------------ */
  function rowsOf(container) {
    return Array.prototype.slice.call(
      container.querySelectorAll('.global-search-row')
    );
  }

  function activeIndex(rows) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].classList.contains('global-search-row--active')) return i;
    }
    return -1;
  }

  function setActive(rows, idx) {
    rows.forEach(function (r, i) {
      if (i === idx) {
        r.classList.add('global-search-row--active');
        r.setAttribute('aria-selected', 'true');
        if (typeof r.scrollIntoView === 'function') {
          r.scrollIntoView({ block: 'nearest' });
        }
      } else {
        r.classList.remove('global-search-row--active');
        r.removeAttribute('aria-selected');
      }
    });
  }

  function bindKeyboardNav() {
    document.addEventListener('keydown', function (e) {
      var input = e.target;
      if (!input || !input.classList || !input.classList.contains('global-search-input')) return;
      var container = findContainer(input);
      if (!container) return;
      var rows = rowsOf(container);
      if (e.key === 'Escape') {
        closeDropdown(container);
        input.blur();
        return;
      }
      if (!rows.length) return;
      var idx = activeIndex(rows);
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActive(rows, (idx + 1) % rows.length);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActive(rows, idx <= 0 ? rows.length - 1 : idx - 1);
      } else if (e.key === 'Enter') {
        if (idx >= 0) {
          e.preventDefault();
          var href = rows[idx].getAttribute('href');
          if (href) window.location.href = href;
        }
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Entry point.                                                        */
  /* ------------------------------------------------------------------ */
  function init() {
    bindShortcut();
    bindAfterSwap();
    bindOutsideClick();
    bindKeyboardNav();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
