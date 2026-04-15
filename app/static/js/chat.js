/**
 * chat.js — Vestlus AI Advisory Chat WebSocket Client
 *
 * Replaces the inline IIFE that lived in app/chat/routes.py (_CHAT_JS).
 * Loaded via <script src="/static/js/chat.js"> after marked.js and
 * DOMPurify CDN scripts in the page head.
 *
 * ============================================================
 * WebSocket event protocol
 * ============================================================
 *
 * CLIENT → SERVER  (JSON objects)
 * --------------------------------
 *   {type: "send_message",    conversation_id, content}
 *   {type: "stop_generation", conversation_id}
 *
 * SERVER → CLIENT  (JSON objects, all have `type`)
 * --------------------------------
 *   {type: "retrieval_started"}
 *   {type: "retrieval_done",  chunk_count: N}
 *   {type: "tool_use",        tool, input, tool_call_id}
 *   {type: "tool_result",     tool, tool_call_id, result_count, result}
 *   {type: "content_delta",   delta, message_id?}
 *   {type: "done",            message_id}
 *   {type: "sources",         message_id, sources: [{source_uri, title, score, snippet}]}
 *   {type: "follow_ups",      message_id, suggestions: ["...", ...]}
 *   {type: "stopped",         message_id}
 *   {type: "error",           message}
 *
 * ============================================================
 * Dependencies (must be loaded before this file)
 * ============================================================
 *   - marked.js   (window.marked)
 *   - DOMPurify   (window.DOMPurify)
 */

/* global marked, DOMPurify */

(function () {
  'use strict';

  // --------------------------------------------------------------------------
  // Bootstrap: read conversation id from the container attribute.
  // Exit silently when the chat container is not on this page.
  // --------------------------------------------------------------------------

  const container = document.getElementById('chat-container');
  if (!container) return;

  const convId = container.dataset.conversationId;
  if (!convId) return;

  // --------------------------------------------------------------------------
  // Configure markdown parser once at module scope.
  // ADD_ATTR ensures target="_blank" on links survives sanitisation.
  // --------------------------------------------------------------------------

  marked.setOptions({ breaks: true, gfm: true });

  const PURIFY_CONFIG = {
    ADD_ATTR: ['target'],
  };

  // --------------------------------------------------------------------------
  // Slash command palette — mirrors app/chat/slash.py COMMANDS
  // --------------------------------------------------------------------------

  const SLASH_COMMANDS = [
    { name: 'draft',          label: 'Loo eelnõu',      hint: '/draft <teema>' },
    { name: 'compare',        label: 'Võrdle sättega',   hint: '/compare <URI>' },
    { name: 'find-conflicts', label: 'Leia vastuolud',   hint: '/find-conflicts <teema>' },
    { name: 'explain',        label: 'Selgita',          hint: '/explain <tekst>' },
    { name: 'sources',        label: 'Näita allikaid',   hint: '/sources' },
  ];

  // Human-readable tool labels for tool_use events (mirrors orchestrator labels)
  const TOOL_LABELS = {
    query_ontology:      'Päring ontoloogiasse',
    search_provisions:   'Otsin sätetest',
    get_draft_impact:    'Analüüsin eelnõu mõju',
    get_provision_details: 'Loen sätte detaile',
  };

  // --------------------------------------------------------------------------
  // Estonian legal citation regex  (mirrors sanitize.py _CITATION_*_RE)
  // --------------------------------------------------------------------------

  // §-style: "KarS § 113", "TsUS § 5 lg 2", "PS § 13 lg 1 p 2"
  const CITATION_PARAGRAPH_RE =
    /\b[A-ZÕÄÖÜ][A-Za-zÕÄÖÜõäöü]{1,10}\s*§\s*\d+(?:\s*lg\s*\d+)?(?:\s*p\s*\d+)?\b/g;

  // Article-style: "Art. 5", "Art. 5 lõige 2"
  const CITATION_ARTICLE_RE = /\bArt\.\s*\d+(?:\s*l[õo]ige\s*\d+)?\b/g;

  // Tags whose TEXT content must NOT be relinked
  const SKIP_CITATION_TAGS = new Set(['a', 'code', 'pre', 'script', 'style']);

  // --------------------------------------------------------------------------
  // State
  // --------------------------------------------------------------------------

  let ws = null;
  let reconnectAttempt = 0;
  let reconnectTimer = null;
  let streaming = false;
  let wasConnected = false;
  // Set when the WS closes mid-stream; the next successful open surfaces a
  // broken-response note, releases the send button, and clears pendingBubble.
  // We deliberately do NOT auto-resend — simpler + predictable for the user.
  let brokenMidStream = false;

  // Per-message-id accumulation buffer and bubble element map
  const msgBuffers = {};   // message_id → accumulated text string
  const msgBubbles = {};   // message_id → .chat-message element

  // Transient (pre-done) streaming state
  let pendingMsgId = null;  // provisional id while streaming before done arrives
  let pendingBubble = null; // the live assistant bubble being streamed into

  // Slash palette keyboard selection index
  let paletteIndex = -1;

  // Quota refresh timer id
  let quotaTimer = null;

  // --------------------------------------------------------------------------
  // DOM references (looked up once — all are optional; guarded before use)
  // --------------------------------------------------------------------------

  const messagesEl   = document.getElementById('chat-messages');
  const inputEl      = document.getElementById('chat-input');
  const sendBtn      = document.getElementById('chat-send-btn');
  const stopBtn      = document.getElementById('chat-stop-btn');
  const statusEl     = document.getElementById('chat-status');
  const paletteEl    = document.getElementById('chat-slash-palette');
  const quotaEl      = document.getElementById('chat-quota');

  // --------------------------------------------------------------------------
  // § 3 — Toast helper
  // --------------------------------------------------------------------------

  function getOrCreateToastContainer() {
    let el = document.querySelector('.chat-toast-container');
    if (!el) {
      el = document.createElement('div');
      el.className = 'chat-toast-container';
      document.body.appendChild(el);
    }
    return el;
  }

  /**
   * Show a transient notification.
   * @param {string} msg      Notification text.
   * @param {string} variant  'success' | 'warning' | 'info' | 'error'
   * @param {number} ttl      Auto-dismiss delay in ms (default 4000).
   */
  function showToast(msg, variant, ttl) {
    // Named ``toastContainer`` (not ``container``) to avoid shadowing the
    // module-level ``container`` reference to ``#chat-container``.
    const toastContainer = getOrCreateToastContainer();
    const ttlMs = typeof ttl === 'number' ? ttl : 4000;

    const toast = document.createElement('div');
    toast.className = 'chat-toast chat-toast--' + variant;
    toast.textContent = msg;
    toastContainer.appendChild(toast);

    setTimeout(function () {
      toast.classList.add('chat-toast--leaving');
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 200);
    }, ttlMs);
  }

  // --------------------------------------------------------------------------
  // § 14 — Quota pill
  // --------------------------------------------------------------------------

  function refreshQuota() {
    fetch('/api/me/usage', { credentials: 'same-origin' })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (!data || !quotaEl) return;
        renderQuotaPill(data);
      })
      .catch(function () { /* quota is best-effort */ });
  }

  function renderQuotaPill(data) {
    if (!quotaEl) return;

    const pct = (data.percentages && data.percentages.messages) || 0;
    const used = data.messages_this_hour || 0;
    const limit = data.message_limit_per_hour || 0;
    const remaining = data.messages_remaining || 0;
    const secs = data.seconds_until_reset || 0;

    quotaEl.setAttribute('aria-valuenow', String(Math.round(pct)));

    // First real paint: drop the server-rendered aria-busy so screen readers
    // stop announcing the pill as loading.
    if (quotaEl.dataset.initial === 'true') {
      quotaEl.removeAttribute('aria-busy');
      delete quotaEl.dataset.initial;
    }

    quotaEl.classList.remove('chat-quota--warning', 'chat-quota--critical');
    if (pct >= 100) {
      quotaEl.classList.add('chat-quota--critical');
    } else if (pct >= 80) {
      quotaEl.classList.add('chat-quota--warning');
    }

    // If viewport <= 420px the CSS class hints compact mode; JS just adds it.
    if (window.innerWidth <= 420) {
      quotaEl.classList.add('chat-quota--compact');
    } else {
      quotaEl.classList.remove('chat-quota--compact');
    }

    let label;
    if (remaining === 0 && secs > 0) {
      const mins = Math.ceil(secs / 60);
      label = 'Proovige uuesti ' + mins + ' min pärast';
    } else {
      label = used + '/' + limit + ' sõnumit selles tunnis';
    }

    // Update the text inside the pill without replacing the whole element
    // so that aria-* attributes stay stable.
    const labelEl = quotaEl.querySelector('.chat-quota-label');
    if (labelEl) {
      labelEl.textContent = label;
    } else {
      quotaEl.textContent = label;
    }
  }

  function startQuotaPolling() {
    refreshQuota();
    quotaTimer = setInterval(function () {
      if (!document.hidden) refreshQuota();
    }, 60_000);
  }

  // --------------------------------------------------------------------------
  // § 2 — WebSocket lifecycle
  // --------------------------------------------------------------------------

  function setStatus(state) {
    if (!statusEl) return;
    statusEl.className = 'chat-status chat-status--' + state;
    const labels = {
      connected:    'Ühendatud',
      reconnecting: 'Taastub...',
      disconnected: 'Ühendus katkes',
    };
    statusEl.textContent = labels[state] || state;
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + '/ws/chat');

    ws.addEventListener('open', function () {
      if (wasConnected) {
        showToast('Ühendus taastatud', 'success');
      }
      wasConnected = true;
      reconnectAttempt = 0;
      setStatus('connected');

      // Recovery after a mid-stream disconnect: the server will not resume
      // the broken turn, so surface the break to the user, unlock the input,
      // and clear transient streaming state. No auto-resend — the user
      // re-sends manually if they want another attempt.
      if (brokenMidStream) {
        brokenMidStream = false;
        if (pendingBubble) {
          const chatBubble = pendingBubble.querySelector('.chat-bubble');
          const note = document.createElement('p');
          note.className = 'chat-stopped-note';
          note.textContent =
            '— ühendus katkes, vajutage genereerimiseks uuesti';
          if (chatBubble) chatBubble.appendChild(note);
        }
        pendingBubble = null;
        pendingMsgId = null;
        enableInput();
        showToast('Ühendus taastus — vastus jäi pooleli', 'warning');
      }
    });

    ws.addEventListener('close', function () {
      setStatus(reconnectAttempt === 0 ? 'disconnected' : 'reconnecting');
      if (reconnectAttempt === 0) {
        showToast('Ühendus katkes — proovin taastada...', 'warning');
      }
      // Remember that the socket died while a response was being streamed;
      // the next ``open`` handler uses this to recover the UI state.
      if (streaming) {
        brokenMidStream = true;
      }
      scheduleReconnect();
    });

    ws.addEventListener('message', function (evt) {
      let event;
      try {
        event = JSON.parse(evt.data);
      } catch (e) {
        return; // ignore unparseable frames
      }
      handleServerEvent(event);
    });

    ws.addEventListener('error', function () {
      // 'close' fires right after 'error'; let that handler do the work.
    });
  }

  function scheduleReconnect() {
    if (reconnectTimer) return; // already scheduled
    // Exponential backoff: 1s, 2s, 4s, 8s, cap 16s
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempt), 16_000);
    reconnectAttempt++;
    setStatus('reconnecting');
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function wsSend(payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(payload));
    return true;
  }

  // --------------------------------------------------------------------------
  // § 4 — Live streaming render helpers
  // --------------------------------------------------------------------------

  /**
   * Return the distance in pixels the user has scrolled away from the bottom.
   */
  function distanceFromBottom() {
    if (!messagesEl) return 0;
    return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
  }

  function scrollToBottom() {
    if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function maybeScrollToBottom() {
    if (distanceFromBottom() <= 80) {
      scrollToBottom();
    } else {
      showNewMessagesPill();
    }
  }

  function showNewMessagesPill() {
    if (document.getElementById('chat-new-messages-pill')) return;
    const pill = document.createElement('button');
    pill.id = 'chat-new-messages-pill';
    pill.className = 'chat-new-messages-pill';
    pill.textContent = '↓ Uued sõnumid';
    pill.setAttribute('type', 'button');
    pill.addEventListener('click', function () {
      scrollToBottom();
      removePill();
    });
    // Insert just before the input area
    const inputArea = container.querySelector('.chat-input-area');
    if (inputArea) {
      container.insertBefore(pill, inputArea);
    } else if (messagesEl && messagesEl.parentNode) {
      messagesEl.parentNode.insertBefore(pill, messagesEl.nextSibling);
    }
  }

  function removePill() {
    const pill = document.getElementById('chat-new-messages-pill');
    if (pill && pill.parentNode) pill.parentNode.removeChild(pill);
  }

  // Remove pill once user scrolls close to bottom manually
  if (messagesEl) {
    messagesEl.addEventListener('scroll', function () {
      if (distanceFromBottom() <= 80) removePill();
    }, { passive: true });
  }

  /**
   * Create a new assistant bubble and add it to the messages container.
   * Returns the bubble element.
   */
  function createAssistantBubble(msgId) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-message chat-message-assistant';
    if (msgId) wrap.dataset.messageId = msgId;

    wrap.innerHTML =
      '<div class="chat-bubble chat-bubble-assistant">' +
        '<div class="chat-message-text"></div>' +
      '</div>';

    if (messagesEl) messagesEl.appendChild(wrap);
    return wrap;
  }

  /**
   * Re-render the accumulated text buffer into the bubble's text container.
   */
  function renderBuffer(bubble, text) {
    const textEl = bubble.querySelector('.chat-message-text');
    if (!textEl) return;
    const html = DOMPurify.sanitize(marked.parse(text), PURIFY_CONFIG);
    textEl.innerHTML = html;
    linkifyCitations(textEl);
  }

  // --------------------------------------------------------------------------
  // § 4 (continued) — Estonian legal citation auto-linker (client-side)
  // Uses a TreeWalker to visit text nodes; wraps matches with <a>.
  // Skips inside <a>, <code>, <pre>.
  // --------------------------------------------------------------------------

  function linkifyCitations(root) {
    // Collect candidate text nodes first (mutating during walk is unsafe).
    const candidates = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        // Walk up and skip if we're inside a forbidden ancestor
        let el = node.parentElement;
        while (el && el !== root) {
          if (SKIP_CITATION_TAGS.has(el.tagName.toLowerCase())) {
            return NodeFilter.FILTER_REJECT;
          }
          el = el.parentElement;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let node;
    while ((node = walker.nextNode())) {
      candidates.push(node);
    }

    for (const textNode of candidates) {
      const original = textNode.nodeValue;
      if (!original) continue;

      // Reset lastIndex before each search
      CITATION_PARAGRAPH_RE.lastIndex = 0;
      CITATION_ARTICLE_RE.lastIndex = 0;

      const matches = [];
      let m;
      CITATION_PARAGRAPH_RE.lastIndex = 0;
      while ((m = CITATION_PARAGRAPH_RE.exec(original)) !== null) {
        matches.push({ start: m.index, end: m.index + m[0].length, text: m[0] });
      }
      CITATION_ARTICLE_RE.lastIndex = 0;
      while ((m = CITATION_ARTICLE_RE.exec(original)) !== null) {
        matches.push({ start: m.index, end: m.index + m[0].length, text: m[0] });
      }
      if (matches.length === 0) continue;

      // Sort and de-overlap (keep earliest; on tie keep longest)
      matches.sort(function (a, b) {
        return a.start !== b.start ? a.start - b.start : (b.end - b.start) - (a.end - a.start);
      });
      const nonOverlapping = [];
      let lastEnd = -1;
      for (const match of matches) {
        if (match.start < lastEnd) continue;
        nonOverlapping.push(match);
        lastEnd = match.end;
      }

      // Build fragment with text + anchor nodes
      const frag = document.createDocumentFragment();
      let cursor = 0;
      for (const match of nonOverlapping) {
        if (match.start > cursor) {
          frag.appendChild(document.createTextNode(original.slice(cursor, match.start)));
        }
        const anchor = document.createElement('a');
        anchor.className = 'citation-link';
        anchor.href = '/explorer?q=' + encodeURIComponent(match.text);
        anchor.textContent = match.text;
        frag.appendChild(anchor);
        cursor = match.end;
      }
      if (cursor < original.length) {
        frag.appendChild(document.createTextNode(original.slice(cursor)));
      }

      textNode.parentNode.replaceChild(frag, textNode);
    }
  }

  // --------------------------------------------------------------------------
  // § 4 (continued) — Get-or-create streaming bubble
  // --------------------------------------------------------------------------

  function getOrCreateStreamingBubble(msgId) {
    // Prefer a stable msgId; fall back to the current pendingBubble.
    if (msgId && msgBubbles[msgId]) return msgBubbles[msgId];

    if (msgId && pendingBubble) {
      // Promote pendingBubble to the real msgId
      pendingBubble.dataset.messageId = msgId;
      msgBubbles[msgId] = pendingBubble;
      if (pendingMsgId && pendingMsgId !== msgId) {
        delete msgBubbles[pendingMsgId];
        delete msgBuffers[pendingMsgId];
      }
      pendingMsgId = msgId;
      return pendingBubble;
    }

    if (pendingBubble) return pendingBubble;

    // Create brand-new bubble
    const bubble = createAssistantBubble(msgId || null);
    pendingBubble = bubble;
    pendingMsgId = msgId || ('_pending_' + Date.now());
    if (msgId) msgBubbles[msgId] = bubble;
    return bubble;
  }

  // --------------------------------------------------------------------------
  // § 5 — Tool activity
  // --------------------------------------------------------------------------

  function toolLabel(toolName) {
    return TOOL_LABELS[toolName] || toolName;
  }

  /**
   * Create a <details> element for a tool_use event and insert it before
   * the next assistant content (or at end of messages).
   */
  function createToolActivity(toolCallId, toolName, input) {
    const details = document.createElement('details');
    details.className = 'chat-tool-activity';
    details.dataset.toolCallId = toolCallId;

    const label = toolLabel(toolName);
    const spinner = '<span class="chat-tool-spinner" aria-hidden="true"></span>';

    const summary = document.createElement('summary');
    summary.innerHTML = spinner + label;
    details.appendChild(summary);

    const pre = document.createElement('pre');
    pre.className = 'chat-tool-input';
    pre.textContent = JSON.stringify(input, null, 2);
    details.appendChild(pre);

    if (messagesEl) messagesEl.appendChild(details);
    return details;
  }

  /**
   * Find an existing tool activity <details> by tool_call_id.
   */
  function findToolActivity(toolCallId) {
    return messagesEl
      ? messagesEl.querySelector('.chat-tool-activity[data-tool-call-id="' + toolCallId + '"]')
      : null;
  }

  /**
   * Mark a tool activity as done with its result.
   */
  function completeToolActivity(toolCallId, toolName, resultCount, result) {
    const details = findToolActivity(toolCallId);
    if (!details) return;

    details.classList.add('chat-tool-activity-done');

    const summary = details.querySelector('summary');
    if (summary) {
      const spinnerEl = summary.querySelector('.chat-tool-spinner');
      if (spinnerEl && spinnerEl.parentNode) spinnerEl.parentNode.removeChild(spinnerEl);
      summary.textContent = '✓ ' + toolLabel(toolName) + ' — ' + resultCount + ' tulemust';
    }

    const resultPre = document.createElement('pre');
    resultPre.className = 'chat-tool-result';
    resultPre.textContent = JSON.stringify(result, null, 2);
    details.appendChild(resultPre);
  }

  // --------------------------------------------------------------------------
  // § 6 — Sources panel
  // --------------------------------------------------------------------------

  function appendSources(bubble, sources) {
    const details = document.createElement('details');
    details.className = 'chat-sources';

    const summary = document.createElement('summary');

    if (!sources || sources.length === 0) {
      summary.textContent = 'Allikaid ei leitud';
      details.appendChild(summary);
    } else {
      summary.textContent = 'Allikad (' + sources.length + ')';
      details.appendChild(summary);

      const ul = document.createElement('ul');
      for (const src of sources) {
        const li = document.createElement('li');

        const uri = src.source_uri || '';
        const title = src.title || uriLastSegment(uri) || uri;
        const snippet = src.snippet || '';

        const a = document.createElement('a');
        a.href = '/explorer?q=' + encodeURIComponent(uri);
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = title;

        const p = document.createElement('p');
        // Use textContent to escape snippet — no raw HTML
        p.textContent = snippet;

        li.appendChild(a);
        if (snippet) li.appendChild(p);
        ul.appendChild(li);
      }
      details.appendChild(ul);
    }

    const chatBubble = bubble.querySelector('.chat-bubble');
    if (chatBubble) {
      chatBubble.appendChild(details);
    } else {
      bubble.appendChild(details);
    }
  }

  function uriLastSegment(uri) {
    try {
      const parts = uri.replace(/\/$/, '').split('/');
      return decodeURIComponent(parts[parts.length - 1]);
    } catch (e) {
      return uri;
    }
  }

  // --------------------------------------------------------------------------
  // § 7 — Follow-up suggestion chips
  // --------------------------------------------------------------------------

  function appendFollowUps(bubble, suggestions) {
    if (!suggestions || suggestions.length === 0) return;

    const chipsDiv = document.createElement('div');
    chipsDiv.className = 'chat-follow-ups';

    for (const suggestion of suggestions) {
      const btn = document.createElement('button');
      btn.className = 'chat-follow-up-chip';
      btn.type = 'button';
      btn.textContent = suggestion;
      btn.addEventListener('click', function () {
        if (inputEl) inputEl.value = suggestion;
        // Remove chip container after use
        if (chipsDiv.parentNode) chipsDiv.parentNode.removeChild(chipsDiv);
        sendMessage();
      });
      chipsDiv.appendChild(btn);
    }

    const chatBubble = bubble.querySelector('.chat-bubble');
    if (chatBubble) {
      chatBubble.appendChild(chipsDiv);
    } else {
      bubble.appendChild(chipsDiv);
    }
  }

  // --------------------------------------------------------------------------
  // § 8 — Done / Stopped / Error helpers
  // --------------------------------------------------------------------------

  // Streaming lock: we only gate the *send* path, never the textarea. Users
  // must stay free to scroll, select, copy, or compose their next message
  // while the assistant is still streaming. The legacy names are kept so
  // callers read naturally ("enable input for sending" / "lock sending").
  function enableInput() {
    // inputEl.disabled is intentionally NOT toggled — textarea stays usable.
    if (sendBtn) sendBtn.disabled = false;
    if (stopBtn) {
      stopBtn.disabled = false;
      stopBtn.style.display = 'none';
    }
    streaming = false;
  }

  function disableInput() {
    // inputEl.disabled is intentionally NOT toggled — textarea stays usable
    // while the assistant streams so users can compose the follow-up.
    if (sendBtn) sendBtn.disabled = true;
    if (stopBtn) {
      stopBtn.disabled = false;
      stopBtn.style.display = '';
    }
    streaming = true;
  }

  function finaliseBubble(msgId) {
    // Promote any pending bubble to the real message id
    if (msgId && pendingBubble && !msgBubbles[msgId]) {
      pendingBubble.dataset.messageId = msgId;
      msgBubbles[msgId] = pendingBubble;
    }
    pendingBubble = null;
    pendingMsgId = null;
  }

  function appendErrorBubble(message) {
    const div = document.createElement('div');
    div.className = 'chat-message chat-message-error';
    div.innerHTML =
      '<div class="chat-bubble chat-bubble-error">' +
        '<p>' + escapeHtml(message || 'Viga') + '</p>' +
      '</div>';
    if (messagesEl) messagesEl.appendChild(div);
    scrollToBottom();
  }

  // --------------------------------------------------------------------------
  // Main event dispatcher
  // --------------------------------------------------------------------------

  function handleServerEvent(event) {
    switch (event.type) {

      // -----------------------------------------------------------------------
      case 'retrieval_started':
        // Could show a subtle "Otsin..." indicator; for now a no-op is fine
        // since tool_use events immediately follow.
        break;

      // -----------------------------------------------------------------------
      case 'retrieval_done':
        // chunk_count available if UI wants to display it; currently no-op.
        break;

      // -----------------------------------------------------------------------
      case 'tool_use': {
        const toolCallId = event.tool_call_id || ('tool_' + Date.now());
        createToolActivity(toolCallId, event.tool || '', event.input || {});
        maybeScrollToBottom();
        break;
      }

      // -----------------------------------------------------------------------
      case 'tool_result': {
        completeToolActivity(
          event.tool_call_id || '',
          event.tool || '',
          typeof event.result_count === 'number' ? event.result_count : 0,
          event.result
        );
        break;
      }

      // -----------------------------------------------------------------------
      case 'content_delta': {
        const msgId = event.message_id || null;
        const bubble = getOrCreateStreamingBubble(msgId);

        // Resolve buffer key
        const bufKey = msgId || pendingMsgId;
        if (!msgBuffers[bufKey]) msgBuffers[bufKey] = '';
        msgBuffers[bufKey] += event.delta || '';

        renderBuffer(bubble, msgBuffers[bufKey]);
        maybeScrollToBottom();
        break;
      }

      // -----------------------------------------------------------------------
      case 'done': {
        const msgId = event.message_id;
        finaliseBubble(msgId);
        enableInput();
        scrollToBottom();
        if (inputEl) inputEl.focus();
        break;
      }

      // -----------------------------------------------------------------------
      case 'sources': {
        const bubble = msgBubbles[event.message_id] || pendingBubble;
        if (bubble) appendSources(bubble, event.sources || []);
        break;
      }

      // -----------------------------------------------------------------------
      case 'follow_ups': {
        const bubble = msgBubbles[event.message_id] || pendingBubble;
        if (bubble) appendFollowUps(bubble, event.suggestions || []);
        break;
      }

      // -----------------------------------------------------------------------
      case 'stopped': {
        const msgId = event.message_id;
        finaliseBubble(msgId);
        // Append truncation note to the last assistant bubble
        const bubble = (msgId && msgBubbles[msgId]) || pendingBubble;
        if (bubble) {
          const note = document.createElement('p');
          note.className = 'chat-stopped-note';
          note.textContent = '— vastus katkestati';
          const chatBubble = bubble.querySelector('.chat-bubble');
          if (chatBubble) chatBubble.appendChild(note);
        }
        enableInput();
        showToast('Genereerimine peatatud', 'info');
        if (inputEl) inputEl.focus();
        break;
      }

      // -----------------------------------------------------------------------
      case 'error': {
        finaliseBubble(null);
        appendErrorBubble(event.message);
        enableInput();
        if (inputEl) inputEl.focus();
        // Rate-limit hit: refresh quota display
        if (event.message && event.message.toLowerCase().includes('limiit')) {
          refreshQuota();
        }
        break;
      }

      // -----------------------------------------------------------------------
      default:
        // Unknown server event — silently ignore.
        break;
    }
  }

  // --------------------------------------------------------------------------
  // § 12 — User message rendering (optimistic, before WS echo)
  // --------------------------------------------------------------------------

  function appendUserBubble(text) {
    const div = document.createElement('div');
    div.className = 'chat-message chat-message-user';
    div.innerHTML =
      '<div class="chat-bubble chat-bubble-user">' +
        '<p class="chat-message-text">' + escapeHtml(text) + '</p>' +
      '</div>';
    if (messagesEl) messagesEl.appendChild(div);
    scrollToBottom();
    return div;
  }

  // --------------------------------------------------------------------------
  // Send message
  // --------------------------------------------------------------------------

  function sendMessage() {
    if (!inputEl) return;
    const text = inputEl.value.trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      showToast('Ühendus puudub — palun oodake...', 'warning');
      return;
    }

    appendUserBubble(text);
    hidePalette();
    hideEmptyState();

    wsSend({ type: 'send_message', conversation_id: convId, content: text });

    inputEl.value = '';
    autoGrowTextarea(inputEl);
    disableInput();
    // disableInput() no longer toggles inputEl.disabled (textarea stays
    // writable during streaming), so there is nothing to re-enable here.
    // Return focus to the textarea for keyboard users.
    if (inputEl) inputEl.focus();
  }

  // --------------------------------------------------------------------------
  // § 9 — Stop button
  // --------------------------------------------------------------------------

  if (stopBtn) {
    stopBtn.style.display = 'none'; // hidden until streaming starts
    stopBtn.addEventListener('click', function () {
      stopBtn.disabled = true;
      showToast('Peatamine...', 'info');
      wsSend({ type: 'stop_generation', conversation_id: convId });
    });
  }

  // --------------------------------------------------------------------------
  // § 10 — Keyboard shortcuts + textarea auto-grow
  // --------------------------------------------------------------------------

  // Skip JS-driven height manipulation when the browser already handles it
  // natively via CSS ``field-sizing: content``. Manipulating style.height on
  // top of field-sizing causes the two mechanisms to fight (jittery resize).
  const SUPPORTS_FIELD_SIZING =
    typeof CSS !== 'undefined' &&
    typeof CSS.supports === 'function' &&
    CSS.supports('field-sizing', 'content');

  function autoGrowTextarea(el) {
    if (!el) return;
    if (SUPPORTS_FIELD_SIZING) return; // CSS already sizes the textarea.
    el.style.height = 'auto';
    const maxHeight = 200;
    el.style.height = Math.min(el.scrollHeight, maxHeight) + 'px';
    el.style.overflowY = el.scrollHeight > maxHeight ? 'auto' : 'hidden';
  }

  if (inputEl) {
    inputEl.addEventListener('input', function () {
      autoGrowTextarea(inputEl);

      // Slash palette trigger: show palette when input starts with '/'
      const val = inputEl.value;
      if (val.startsWith('/')) {
        const prefix = val.slice(1).split(' ')[0];
        showPalette(prefix);
      } else {
        hidePalette();
      }
    });

    inputEl.addEventListener('keydown', function (e) {
      // Slash palette navigation takes priority when palette is open
      if (paletteEl && !paletteEl.hidden) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          movePaletteSelection(1);
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          movePaletteSelection(-1);
          return;
        }
        if (e.key === 'Enter') {
          e.preventDefault();
          selectPaletteItem();
          return;
        }
        if (e.key === 'Escape') {
          e.preventDefault();
          hidePalette();
          // Fall through to the streaming-stop check below: a user pressing
          // Esc while the palette is open AND a response is streaming
          // expects both things to happen (close palette + stop generation).
          if (streaming && stopBtn && !stopBtn.disabled) {
            stopBtn.click();
          }
          return;
        }
      }

      // Cmd/Ctrl+Enter → send
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (!streaming) sendMessage();
        return;
      }

      // Plain Enter → insert newline (default textarea behaviour — do nothing)

      // Escape while streaming → trigger stop
      if (e.key === 'Escape' && streaming) {
        e.preventDefault();
        if (stopBtn && !stopBtn.disabled) stopBtn.click();
        return;
      }

      // '/' at position 0 with empty value → open slash palette
      if (e.key === '/' && inputEl.value === '' && inputEl.selectionStart === 0) {
        // Let the character be typed first (input event fires after), but
        // also pre-open the full palette.
        setTimeout(function () { showPalette(''); }, 0);
      }
    });
  }

  if (sendBtn) {
    sendBtn.addEventListener('click', function () {
      if (!streaming) sendMessage();
    });
  }

  // --------------------------------------------------------------------------
  // § 11 — Slash command palette
  // --------------------------------------------------------------------------

  function showPalette(prefix) {
    if (!paletteEl) return;

    const lower = prefix.toLowerCase();
    const matches = lower === ''
      ? SLASH_COMMANDS
      : SLASH_COMMANDS.filter(function (cmd) { return cmd.name.startsWith(lower); });

    if (matches.length === 0) {
      hidePalette();
      return;
    }

    paletteEl.innerHTML = '';
    paletteIndex = -1;

    for (let i = 0; i < matches.length; i++) {
      const cmd = matches[i];
      const li = document.createElement('li');
      li.className = 'chat-slash-palette-item';
      li.dataset.name = cmd.name;
      li.setAttribute('role', 'option');
      li.innerHTML =
        '<span class="chat-slash-palette-label">' + escapeHtml(cmd.label) + '</span>' +
        '<span class="chat-slash-palette-hint">' + escapeHtml(cmd.hint) + '</span>';

      li.addEventListener('mousedown', function (e) {
        // mousedown fires before blur — prevent focus loss from input
        e.preventDefault();
        insertSlashCommand(cmd.name);
      });

      paletteEl.appendChild(li);
    }

    paletteEl.hidden = false;
  }

  function hidePalette() {
    if (!paletteEl) return;
    paletteEl.hidden = true;
    paletteIndex = -1;
  }

  function movePaletteSelection(delta) {
    if (!paletteEl || paletteEl.hidden) return;
    const items = paletteEl.querySelectorAll('.chat-slash-palette-item');
    if (items.length === 0) return;

    if (paletteIndex >= 0 && paletteIndex < items.length) {
      items[paletteIndex].classList.remove('is-selected');
    }
    paletteIndex = Math.max(0, Math.min(items.length - 1, paletteIndex + delta));
    items[paletteIndex].classList.add('is-selected');
    items[paletteIndex].scrollIntoView({ block: 'nearest' });
  }

  function selectPaletteItem() {
    if (!paletteEl || paletteEl.hidden) return;
    const items = paletteEl.querySelectorAll('.chat-slash-palette-item');
    const idx = paletteIndex >= 0 ? paletteIndex : 0;
    if (items[idx]) {
      insertSlashCommand(items[idx].dataset.name);
    }
  }

  function insertSlashCommand(name) {
    if (!inputEl) return;
    inputEl.value = '/' + name + ' ';
    hidePalette();
    inputEl.focus();
    // Place cursor at end
    inputEl.selectionStart = inputEl.selectionEnd = inputEl.value.length;
    autoGrowTextarea(inputEl);
  }

  // --------------------------------------------------------------------------
  // § 12 — Example prompts (empty state)
  // --------------------------------------------------------------------------

  function hideEmptyState() {
    const emptyState = container.querySelector('.chat-empty-state');
    if (emptyState) emptyState.style.display = 'none';
  }

  // Event delegation: any .chat-example-prompt with data-prompt
  container.addEventListener('click', function (e) {
    const promptEl = e.target.closest('.chat-example-prompt[data-prompt]');
    if (!promptEl) return;
    const prompt = promptEl.dataset.prompt;
    if (!prompt) return;
    hideEmptyState();
    if (inputEl) inputEl.value = prompt;
    if (!streaming) sendMessage();
    if (inputEl) inputEl.focus();
  });

  // --------------------------------------------------------------------------
  // § 13 — Message actions (copy, feedback, regenerate, edit)
  // --------------------------------------------------------------------------

  // Event delegation on the messages container
  if (messagesEl) {
    messagesEl.addEventListener('click', function (e) {
      const btn = e.target.closest('.chat-message-action-btn[data-action]');
      if (!btn) return;

      const action = btn.dataset.action;
      const msgEl = btn.closest('[data-message-id]');
      const msgId = msgEl ? msgEl.dataset.messageId : null;

      switch (action) {
        case 'copy':
          handleCopy(msgEl, btn);
          break;
        case 'feedback-up':
          handleFeedback(msgId, 1, btn);
          break;
        case 'feedback-down':
          handleFeedback(msgId, -1, btn);
          break;
        case 'regenerate':
          handleRegenerate(msgId, btn);
          break;
        case 'edit':
          handleEdit(msgId, msgEl, btn);
          break;
      }
    });
  }

  function handleCopy(msgEl, btn) {
    if (!msgEl) return;
    const textEl = msgEl.querySelector('.chat-message-text');
    const text = textEl ? textEl.innerText || textEl.textContent : '';
    if (!text) return;
    navigator.clipboard.writeText(text).then(function () {
      showToast('Kopeeritud', 'success', 2000);
    }).catch(function () {
      showToast('Kopeerimine ebaõnnestus', 'error');
    });
  }

  function handleFeedback(msgId, rating, btn) {
    if (!msgId) return;
    const formData = new FormData();
    formData.append('rating', String(rating));

    fetch('/chat/' + convId + '/messages/' + msgId + '/feedback', {
      method: 'POST',
      body: formData,
      credentials: 'same-origin',
    }).then(function (res) {
      if (res.ok) {
        // Mark the clicked button selected; deselect sibling
        const actionRow = btn.closest('.chat-message-actions');
        if (actionRow) {
          actionRow.querySelectorAll('.chat-message-action-btn').forEach(function (b) {
            b.classList.remove('is-selected');
          });
        }
        btn.classList.add('is-selected');
        showToast('Täname tagasiside eest', 'success', 3000);
      }
    }).catch(function () {
      showToast('Tagasiside salvestamine ebaõnnestus', 'error');
    });
  }

  function handleRegenerate(msgId, btn) {
    if (!msgId) return;

    fetch('/chat/' + convId + '/messages/' + msgId + '/regenerate', {
      method: 'POST',
      credentials: 'same-origin',
    }).then(function (res) {
      if (!res.ok) {
        showToast('Taasgenereerimine ebaõnnestus', 'error');
        return;
      }
      // Locate the preceding user message text and replay it via WS
      const msgEl = messagesEl
        ? messagesEl.querySelector('[data-message-id="' + msgId + '"]')
        : null;
      let userText = '';
      if (msgEl) {
        // Walk backwards through siblings to find the previous user bubble
        let prev = msgEl.previousElementSibling;
        while (prev) {
          if (prev.classList.contains('chat-message-user')) {
            const t = prev.querySelector('.chat-message-text');
            if (t) userText = t.innerText || t.textContent || '';
            break;
          }
          prev = prev.previousElementSibling;
        }
        // Remove the assistant bubble we just asked to regenerate
        if (msgEl.parentNode) msgEl.parentNode.removeChild(msgEl);
      }

      if (userText && inputEl) {
        inputEl.value = userText;
        sendMessage();
      }
    }).catch(function () {
      showToast('Taasgenereerimine ebaõnnestus', 'error');
    });
  }

  function handleEdit(msgId, msgEl, btn) {
    if (!msgId || !msgEl) return;

    const textEl = msgEl.querySelector('.chat-message-text');
    const originalText = textEl ? (textEl.innerText || textEl.textContent || '').trim() : '';

    // Replace the bubble content with an inline edit form
    const bubble = msgEl.querySelector('.chat-bubble');
    if (!bubble) return;
    const originalHtml = bubble.innerHTML;

    const textarea = document.createElement('textarea');
    textarea.className = 'chat-edit-textarea';
    textarea.value = originalText;
    textarea.rows = 3;

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn btn-primary btn-sm chat-edit-save';
    saveBtn.textContent = 'Salvesta';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn btn-secondary btn-sm chat-edit-cancel';
    cancelBtn.textContent = 'Tühista';

    const btnRow = document.createElement('div');
    btnRow.className = 'chat-edit-actions';
    btnRow.appendChild(saveBtn);
    btnRow.appendChild(cancelBtn);

    bubble.innerHTML = '';
    bubble.appendChild(textarea);
    bubble.appendChild(btnRow);
    textarea.focus();

    cancelBtn.addEventListener('click', function () {
      bubble.innerHTML = originalHtml;
    });

    saveBtn.addEventListener('click', function () {
      const newContent = textarea.value.trim();
      if (!newContent) return;

      const formData = new FormData();
      formData.append('content', newContent);

      fetch('/chat/' + convId + '/messages/' + msgId + '/edit', {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
      }).then(function (res) {
        if (res.ok) {
          // Simplest correct approach: reload the page so server-rendered
          // history is authoritative (per spec).
          location.reload();
        } else {
          showToast('Muutmine ebaõnnestus', 'error');
          bubble.innerHTML = originalHtml;
        }
      }).catch(function () {
        showToast('Muutmine ebaõnnestus', 'error');
        bubble.innerHTML = originalHtml;
      });
    });
  }

  // --------------------------------------------------------------------------
  // Utility: HTML escape (for content inserted via innerHTML)
  // --------------------------------------------------------------------------

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // --------------------------------------------------------------------------
  // Initialise
  // --------------------------------------------------------------------------

  connect();
  scrollToBottom();
  startQuotaPolling();

  // Auto-grow textarea on initial render (e.g. pre-filled value)
  if (inputEl) {
    autoGrowTextarea(inputEl);
    inputEl.focus();
  }

})();
