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
 *    whitespace-free, resolvable token: ``@<full-email> `` (e.g.
 *    ``@andres@min.ee ``). The full email is globally unique in
 *    ``users.email``, so ``parse_mentions`` server-side resolves it
 *    unambiguously even when two orgs share the same local-part
 *    (``andres@min.ee`` vs ``andres@agency.ee``). The readable display
 *    name still shows in the typeahead UI; we only insert the stable
 *    token because the server-side _MENTION_RE stops at whitespace and
 *    would otherwise truncate ``@Andres Tamm`` to ``@Andres``. If the
 *    user has no email we fall back to the email local-part, then the
 *    full_name with spaces stripped, then the user id — all
 *    whitespace-free.
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

  // Word-char regex for what counts as part of an *in-flight* @token (the
  // chars the user is currently typing before they accept a suggestion).
  // Mirrors the local-part character class of the server-side _MENTION_RE
  // in app/annotations/models.py:
  //     re.compile(r"@([\w.\-]+(?:@[\w.\-]+)?)")
  // \w in Python without re.ASCII includes Unicode letters (Estonian
  // diacritics). JS \w is ASCII-only by default, so we use Unicode
  // property escapes for letters + digits. We deliberately do NOT include
  // ``@`` here: the user types ``@andres``, and on accept we *replace*
  // that span with the full ``@andres@min.ee`` token — the typeahead
  // never has to detect a literal ``@`` inside an in-flight token.
  const WORD_CHAR_RE = /[\p{L}\p{N}_.\-]/u;

  // Track widget instances by textarea so we don't double-bind.
  const INSTANCES = new WeakMap();

  // A parallel, *iterable* registry of every textarea that currently holds a
  // live instance. The WeakMap above can't be walked, but HTMX cleanup needs
  // to find-and-destroy instances whose textarea is being swapped out — each
  // attach() registers 3 global listeners + a <ul> appended to <body> that
  // would otherwise leak on every popover swap (issue #861-D).
  const LIVE_TEXTAREAS = new Set();

  /** Remove every child of an element without using innerHTML. */
  function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  /**
   * Compute the whitespace-free token to insert after ``@`` for a
   * suggestion. Prefer the full email address (e.g. ``andres@min.ee``)
   * so the server-side resolver matches the exact user — even when two
   * users in different orgs share the same email local-part. Fall back
   * to the email local-part, then a space-stripped full_name, then the
   * user id, so we always emit *something* the parser can match.
   *
   * The full-email path emits chars that match the expanded server-side
   * _MENTION_RE (``[\w.\-]+(?:@[\w.\-]+)?`` — Unicode letters/digits,
   * underscore, dot, hyphen, plus an optional ``@domain`` suffix). The
   * fallback paths emit only ``[\w.\-]+`` chars. Any other char in the
   * source is stripped so the token round-trips cleanly through the
   * parser.
   */
  function mentionToken(choice) {
    // Strict local-part safe (no ``@`` allowed).
    const safeLocal = (s) =>
      String(s || "").replace(/[^\p{L}\p{N}_.\-]/gu, "");
    // Full-email safe — keeps a single ``@`` between local-part and domain
    // and strips any other ``@`` chars so the token always matches the
    // server regex's optional ``@domain`` suffix exactly once.
    const safeEmail = (s) => {
      const cleaned = String(s || "").replace(/[^\p{L}\p{N}_.\-@]/gu, "");
      const at = cleaned.indexOf("@");
      if (at < 0) return cleaned;
      const local = cleaned.slice(0, at).replace(/@/g, "");
      const domain = cleaned.slice(at + 1).replace(/@/g, "");
      if (!local || !domain) return local;
      return `${local}@${domain}`;
    };
    const email = String(choice.email || "");
    if (email.includes("@")) {
      const fullEmail = safeEmail(email);
      // Only accept when both local + domain survived sanitisation; this
      // disambiguates two users who share a local-part in different orgs.
      if (fullEmail.includes("@")) return fullEmail;
      const local = safeLocal(email.split("@")[0]);
      if (local) return local;
    }
    const fromName = safeLocal(choice.full_name || choice.label);
    if (fromName) return fromName;
    return safeLocal(choice.id);
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
      // Preferred form: ``@<full-email>`` (globally unique, immune to
      // local-part collisions across orgs); fall back to
      // ``@<email-local-part>``, then ``@<full_name-without-spaces>``,
      // then ``@<id>`` so we always insert something the resolver can
      // match. The readable display label only appears in the typeahead
      // UI itself.
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
        LIVE_TEXTAREAS.delete(textarea);
      },
    };
    INSTANCES.set(textarea, instance);
    LIVE_TEXTAREAS.add(textarea);
    return instance;
  }

  /** Tear down the instance bound to a single textarea, if any. */
  function destroyOne(textarea) {
    const instance = INSTANCES.get(textarea);
    if (instance) instance.destroy();
  }

  /**
   * Destroy every live instance whose textarea sits inside *root* (inclusive).
   * Used on HTMX swap/cleanup so popovers removed from the DOM also drop their
   * global listeners + the orphan list element they appended to <body>.
   */
  function destroyWithin(root) {
    if (!root || typeof root.contains !== "function") return;
    // Snapshot first — destroy() mutates LIVE_TEXTAREAS during iteration.
    for (const textarea of Array.from(LIVE_TEXTAREAS)) {
      if (root === textarea || root.contains(textarea)) {
        destroyOne(textarea);
      }
    }
  }

  /**
   * Sweep for instances whose textarea has been detached from the document
   * (a swap that replaced an ancestor we never saw a cleanup event for).
   * Belt-and-braces backstop alongside the targeted destroyWithin() calls.
   */
  function destroyDetached() {
    for (const textarea of Array.from(LIVE_TEXTAREAS)) {
      if (!textarea.isConnected) destroyOne(textarea);
    }
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

  // HTMX is about to remove a specific element from the DOM — tear down any
  // instance bound to it (or to a descendant textarea) so its global
  // listeners + orphan <body> list don't leak (issue #861-D).
  document.body.addEventListener("htmx:beforeCleanupElement", (ev) => {
    destroyWithin(ev.target);
  });

  // HTMX is about to replace a swap target's contents — destroy instances
  // living inside the outgoing markup before it's discarded. Pairs with the
  // afterSwap re-scan below, which re-binds the incoming markup.
  document.body.addEventListener("htmx:beforeSwap", (ev) => {
    destroyWithin(ev.target);
  });

  // Re-scan after every HTMX swap so newly-rendered popovers get bound. Also
  // sweep for any instance whose textarea ended up detached without a
  // cleanup event firing (belt-and-braces).
  document.body.addEventListener("htmx:afterSwap", (ev) => {
    destroyDetached();
    attachAll(ev.target || document);
  });

  // Expose for manual rebinds / debugging.
  window.AnnotationMentions = { attach, attachAll, destroyWithin, destroyDetached };
})();
