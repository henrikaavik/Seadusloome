/**
 * Tabs keyboard navigation — Seadusloome design system.
 *
 * Implements the WAI-ARIA Authoring Practices "tabs with manual activation"
 * pattern: arrow keys move focus between tabs (roving tabindex), Home/End
 * jump to the first/last tab, and Enter or Space activates the focused tab.
 * Selection does NOT follow focus — users must explicitly activate.
 *
 * Usage:
 *   <script src="/static/js/tabs.js" defer></script>
 *
 * Any element with `data-tabs="horizontal"` or `data-tabs="vertical"` will be
 * wired up automatically on DOMContentLoaded. HTMX swaps also re-run init.
 */
(function () {
  "use strict";

  function initTablist(tablist) {
    if (tablist.dataset.tabsInit === "1") return;
    tablist.dataset.tabsInit = "1";

    const orientation = tablist.dataset.tabs || "horizontal";
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
    if (tabs.length === 0) return;

    const nextKey = orientation === "vertical" ? "ArrowDown" : "ArrowRight";
    const prevKey = orientation === "vertical" ? "ArrowUp" : "ArrowLeft";

    // Roving tabindex follows *focus*: when arrow keys move focus to another
    // tab, that tab becomes the single tab-stop (tabindex="0") and the rest
    // drop to tabindex="-1". This is intentionally separate from selection —
    // `aria-selected` and the panels' `hidden` state only change on actual
    // activation (Enter/Space/click) via `activateTab`. WAI-ARIA APG: "tabs
    // with manual activation". See issue #744.
    function setRovingTabindex(focusedIndex) {
      tabs.forEach(function (t, i) {
        t.setAttribute("tabindex", i === focusedIndex ? "0" : "-1");
      });
    }

    function focusTab(index) {
      const targetIndex = (index + tabs.length) % tabs.length;
      setRovingTabindex(targetIndex);
      tabs[targetIndex].focus();
    }

    function activateTab(tab) {
      tabs.forEach(function (t) {
        const selected = t === tab;
        t.setAttribute("aria-selected", selected ? "true" : "false");
        t.setAttribute("tabindex", selected ? "0" : "-1");
        const panelId = t.getAttribute("aria-controls");
        if (panelId) {
          const panel = document.getElementById(panelId);
          if (panel) {
            if (selected) {
              panel.removeAttribute("hidden");
            } else {
              panel.setAttribute("hidden", "");
            }
          }
        }
      });
    }

    tabs.forEach(function (tab, index) {
      tab.addEventListener("keydown", function (event) {
        switch (event.key) {
          case nextKey:
            event.preventDefault();
            focusTab(index + 1);
            break;
          case prevKey:
            event.preventDefault();
            focusTab(index - 1);
            break;
          case "Home":
            event.preventDefault();
            focusTab(0);
            break;
          case "End":
            event.preventDefault();
            focusTab(tabs.length - 1);
            break;
          case "Enter":
          case " ":
            event.preventDefault();
            activateTab(tab);
            break;
        }
      });
      tab.addEventListener("click", function () {
        activateTab(tab);
      });
    });
  }

  function initAll(root) {
    (root || document).querySelectorAll("[data-tabs]").forEach(initTablist);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initAll(document);
    });
  } else {
    initAll(document);
  }

  document.body &&
    document.body.addEventListener("htmx:afterSwap", function (event) {
      initAll(event.target);
    });
})();
