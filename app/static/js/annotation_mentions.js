/**
 * Annotation @mention typeahead — Issue #176.
 *
 * Lightweight vanilla JS widget that binds to any textarea carrying
 * the ``data-annotation-textarea`` attribute and pops up a floating
 * list of org-scoped user candidates when the caret is in the middle
 * of an ``@token`` typed by the user.
 *
 * Wire pattern:
 *   - This script is loaded globally from app/main.py _HDRS, so it
 *     attaches itself to every textarea on the page on DOMContentLoaded
 *     AND on every HTMX swap (so freshly-rendered annotation popovers
 *     get the same behaviour without re-loading the script).
 *
 *  - Backend: GET /api/annotations/mentions/search?q=<prefix>
 *    Returns {"results": [{id, label, full_name, email}, ...]}.
 *
 *  - On select, the ``@<query>`` substring is replaced with a
 *    whitespace-free, resolvable token: ``@<email-local-part> ``
 *    (the part before ``@`` in the user's email). ``parse_mentions``
 *    server-side resolves it by matching against ``users.email``'s
 *    local part. The readable display name still shows in the
 *    typeahead UI; we only insert the stable token because the
 *    server-side _MENTION_RE stops at whitespace and would otherwise
 *    truncate ``@Andres Tamm`` to ``@Andres``. If the user has no
 *    email we fall back to the full_name with spaces stripped, or
 *    the user id as a last resort — all whitespace-free.
 *
 *  - Keyboard: Down/Up navigate, Enter/Tab select, Escape closes.
 *
 *  - Accessibility: textarea sets aria-expanded, aria-controls,
 *    aria-activedescendant; the list has role="listbox" and each
 *    option role="option".
 *
 * Security: every list item is built with textContent / DOM APIs only —
 * no innerHTML on untrusted strings — so a malicious display name
 * cannot inject markup.
 */
(function () {
  "use strict";

  // CSS selector for textareas we should hook into.
  const TARGET_SELECTOR = "textarea[data-annotation-textarea]";

  // Debounce delay between the user typing and the fetch firing.
  const DEBOUNCE_MS = 150;

  // Word-char regex for what counts as part of an @token. Matches the
  // server-side _MENTION_RE in app/annotations/models.py:
  //     re.compile(r"@([\w.\-]+)")
  // \w in Python without re.ASCII includes Unicode letters (Estonian
  // diacritics). JS \w is ASCII-only by default, so we use Unicode
  // property escapes for letters + digits.
  const WORD_CHAR_RE = /[\p{L}\p{N}_.\-]/u;

  // Track widget instances by textarea so we don't double-bind.
  const INSTANCES = new WeakMap();

  /** Remove every child of an element without using innerHTML. */
  function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  /**
   * Compute the whitespace-free token to insert after ``@`` for a
   * suggestion. Prefer the email local-part (everything before ``@``),
   * fall back to a space-stripped full_name, then the user id.
   *
   * Only emits chars that match the server-side _MENTION_RE
   * (``[\w.\-]+`` — Unicode letters/digits, underscore, dot, hyphen);
   * any other char in the source is stripped so the token round-trips
   * cleanly through the parser.
   */
  function mentionToken(choice) {
    const safe = (s) =>
      String(s || "").replace(/[^\p{L}\p{N}_.\-]/gu, "");
    const email = String(choice.email || "");
    const atIdx = email.indexOf("@");
    if (atIdx > 0) {
      const local = safe(email.slice(0, atIdx));
      if (local) return local;
    }
    const fromName = safe(choice.full_name || choice.label);
    if (fromName) return fromName;
    return safe(choice.id);
  }

  /**
   * Wrap a single textarea with a mention-typeahead widget.
   * Idempotent: returns the existing instance if the textarea is
   * already wired up.
   */
  function attach(textarea) {
    if (INSTANCES.has(textarea)) {
      return INSTANCES.get(textarea);
    }

    // Floating result list element.
    const list = document.createElement("ul");
    list.className = "mention-typeahead";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    document.body.appendChild(list);

    // ARIA wiring on the textarea.
    const listId = `mention-typeahead-${Math.random().toString(36).slice(2, 8)}`;
    list.id = listId;
    textarea.setAttribute("aria-controls", listId);
    textarea.setAttribute("aria-expanded", "false");
    textarea.setAttribute("aria-autocomplete", "list");

    const state = {
      // Index of the ``@`` that started the active token, or -1 when no
      // active token (closed).
      tokenStart: -1,
      // Current query string (chars after ``@``).
      query: "",
      // Search results list.
      results: [],
      // Active item index (for keyboard nav).
      activeIndex: -1,
      // Debounce timer.
      debounceTimer: null,
      // Last query we fetched, so we don't re-fetch identical strings.
      lastFetched: null,
    };

    function close() {
      state.tokenStart = -1;
      state.query = "";
      state.results = [];
      state.activeIndex = -1;
      list.hidden = true;
      clearChildren(list);
      textarea.setAttribute("aria-expanded", "false");
      textarea.removeAttribute("aria-activedescendant");
    }

    function renderList() {
      clearChildren(list);
      if (!state.results.length) {
        list.hidden = true;
        textarea.setAttribute("aria-expanded", "false");
        textarea.removeAttribute("aria-activedescendant");
        return;
      }
      state.results.forEach((r, idx) => {
        const li = document.createElement("li");
        li.className =
          "mention-typeahead-item" +
          (idx === state.activeIndex ? " mention-typeahead-item--active" : "");
        li.setAttribute("role", "option");
        li.id = `${listId}-opt-${idx}`;
        li.dataset.index = String(idx);
        li.setAttribute(
          "aria-selected",
          idx === state.activeIndex ? "true" : "false",
        );

        const nameSpan = document.createElement("span");
        nameSpan.className = "mention-typeahead-name";
        nameSpan.textContent = r.label || r.full_name || r.email || "";
        li.appendChild(nameSpan);

        if (r.email && r.email !== (r.label || r.full_name)) {
          const emailSpan = document.createElement("span");
          emailSpan.className = "mention-typeahead-email";
          emailSpan.textContent = r.email;
          li.appendChild(emailSpan);
        }

        // Click selects. Use mousedown so the textarea's blur doesn't
        // close the list before the click registers.
        li.addEventListener("mousedown", (ev) => {
          ev.preventDefault();
          select(idx);
        });
        list.appendChild(li);
      });

      positionList();
      list.hidden = false;
      textarea.setAttribute("aria-expanded", "true");
      if (state.activeIndex >= 0) {
        textarea.setAttribute(
          "aria-activedescendant",
          `${listId}-opt-${state.activeIndex}`,
        );
      }
    }

    function positionList() {
      // Position the list under the textarea. A caret-precise position
      // would require a mirror element; for an MVP the bottom of the
      // textarea is good enough and keeps the code small.
      const rect = textarea.getBoundingClientRect();
      list.style.position = "absolute";
      list.style.left = `${window.scrollX + rect.left}px`;
      list.style.top = `${window.scrollY + rect.bottom}px`;
      list.style.minWidth = `${Math.max(rect.width, 220)}px`;
      list.style.zIndex = "9999";
    }

    function detectToken() {
      const value = textarea.value;
      const caret = textarea.selectionStart;
      if (caret === null || caret === undefined) {
        close();
        return;
      }
      // Walk backwards from caret to find ``@`` preceded by start-of-string
      // or whitespace.
      let i = caret - 1;
      while (i >= 0) {
        const ch = value[i];
        if (ch === "@") {
          // Validate the char before ``@`` is whitespace or string start.
          if (i === 0 || /\s/.test(value[i - 1])) {
            // Validate every char between i+1..caret-1 is a word char.
            const between = value.slice(i + 1, caret);
            if (between === "" || isAllWordChars(between)) {
              state.tokenStart = i;
              state.query = between;
              scheduleFetch();
              return;
            }
          }
          break;
        }
        if (!WORD_CHAR_RE.test(ch)) {
          break;
        }
        i -= 1;
      }
      close();
    }

    function isAllWordChars(str) {
      for (const ch of str) {
        if (!WORD_CHAR_RE.test(ch)) return false;
      }
      return true;
    }

    function scheduleFetch() {
      if (state.debounceTimer) clearTimeout(state.debounceTimer);
      state.debounceTimer = setTimeout(runFetch, DEBOUNCE_MS);
    }

    async function runFetch() {
      const q = state.query;
      if (q === state.lastFetched) {
        // Same query as last time — nothing to do.
        return;
      }
      state.lastFetched = q;
      if (q.length === 0) {
        // Empty query → no results, but keep tokenStart so further typing
        // re-enters the loop.
        state.results = [];
        state.activeIndex = -1;
        renderList();
        return;
      }
      try {
        const resp = await fetch(
          `/api/annotations/mentions/search?q=${encodeURIComponent(q)}`,
          {
            headers: { Accept: "application/json" },
            credentials: "same-origin",
          },
        );
        if (!resp.ok) {
          state.results = [];
          state.activeIndex = -1;
          renderList();
          return;
        }
        const data = await resp.json();
        state.results = Array.isArray(data.results) ? data.results : [];
        state.activeIndex = state.results.length ? 0 : -1;
        renderList();
      } catch (e) {
        // Network failure: silently degrade.
        state.results = [];
        state.activeIndex = -1;
        renderList();
      }
    }

    function select(idx) {
      const choice = state.results[idx];
      if (!choice) return;
      const value = textarea.value;
      const before = value.slice(0, state.tokenStart);
      const after = value.slice(textarea.selectionStart);
      // Insert a whitespace-free token so the server-side _MENTION_RE
      // (which stops at whitespace) captures the full identifier.
      // Preferred form: ``@<email-local-part>``; fall back to
      // ``@<full_name-without-spaces>`` and finally ``@<id>`` so we
      // always insert something the resolver can match. The readable
      // display label only appears in the typeahead UI itself.
      const token = mentionToken(choice);
      const insertion = `@${token} `;
      const newValue = before + insertion + after;
      const newCaret = before.length + insertion.length;

      // Stash the resolved user id for future use (e.g. richer client
      // metadata); parse_mentions still re-resolves by name on submit, so
      // this is purely informational.
      const ids = (textarea.dataset.mentions || "")
        .split(",")
        .filter(Boolean);
      if (!ids.includes(choice.id)) ids.push(choice.id);
      textarea.dataset.mentions = ids.join(",");

      textarea.value = newValue;
      textarea.setSelectionRange(newCaret, newCaret);
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      close();
      textarea.focus();
    }

    function moveActive(delta) {
      if (!state.results.length) return;
      const n = state.results.length;
      state.activeIndex = (state.activeIndex + delta + n) % n;
      renderList();
    }

    // --- event handlers -----------------------------------------------------

    function onInput() {
      detectToken();
    }

    function onKeyDown(ev) {
      if (list.hidden || state.results.length === 0) {
        if (state.tokenStart >= 0 && ev.key === "Escape") {
          close();
        }
        return;
      }
      switch (ev.key) {
        case "ArrowDown":
          ev.preventDefault();
          moveActive(1);
          break;
        case "ArrowUp":
          ev.preventDefault();
          moveActive(-1);
          break;
        case "Enter":
        case "Tab":
          if (state.activeIndex >= 0) {
            ev.preventDefault();
            select(state.activeIndex);
          }
          break;
        case "Escape":
          ev.preventDefault();
          close();
          break;
        default:
          break;
      }
    }

    function onBlur() {
      // Delay so a click on the list resolves first.
      setTimeout(() => {
        if (!list.contains(document.activeElement)) close();
      }, 150);
    }

    function onDocClick(ev) {
      if (ev.target === textarea || list.contains(ev.target)) return;
      close();
    }

    function onScrollOrResize() {
      if (!list.hidden) positionList();
    }

    textarea.addEventListener("input", onInput);
    textarea.addEventListener("keydown", onKeyDown);
    textarea.addEventListener("blur", onBlur);
    document.addEventListener("click", onDocClick);
    window.addEventListener("resize", onScrollOrResize);
    window.addEventListener("scroll", onScrollOrResize, true);

    const instance = {
      textarea,
      list,
      destroy() {
        textarea.removeEventListener("input", onInput);
        textarea.removeEventListener("keydown", onKeyDown);
        textarea.removeEventListener("blur", onBlur);
        document.removeEventListener("click", onDocClick);
        window.removeEventListener("resize", onScrollOrResize);
        window.removeEventListener("scroll", onScrollOrResize, true);
        list.remove();
        INSTANCES.delete(textarea);
      },
    };
    INSTANCES.set(textarea, instance);
    return instance;
  }

  function attachAll(root) {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll(TARGET_SELECTOR).forEach(attach);
  }

  // Initial pass.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => attachAll(document));
  } else {
    attachAll(document);
  }

  // Re-scan after every HTMX swap so newly-rendered popovers get bound.
  document.body.addEventListener("htmx:afterSwap", (ev) => {
    attachAll(ev.target || document);
  });

  // Expose for manual rebinds / debugging.
  window.AnnotationMentions = { attach, attachAll };
})();
