/* Draft pipeline status — WebSocket push listener (#608).
 *
 * Augments the existing 3s HTMX polling on the draft detail page
 * with a WS push so transitions land in <500ms instead of up to 3s.
 * The WS is purely an enhancement: if the connection drops or fails
 * to open at all, the existing hx-trigger="every Ns" attributes on
 * the .draft-status-wrapper continue polling exactly as before. We
 * deliberately do NOT remove those attributes — keeping them is the
 * graceful-degradation path the issue's DoD asks for.
 *
 * Usage: the page renders a <div data-draft-status-ws data-draft-id="...">
 * marker; this script self-initialises on DOMContentLoaded and
 * connects to /ws/drafts/status, subscribes to the draft, and swaps
 * the status wrapper via htmx.ajax() on every push event.
 *
 * The WS closes itself once the draft reaches a terminal status
 * (ready / failed) so the connection doesn't linger forever on a
 * page the user is no longer actively monitoring.
 */
(function () {
  'use strict';

  var TERMINAL_STATUSES = ['ready', 'failed'];

  function start(draftId) {
    if (!draftId || typeof WebSocket === 'undefined') return;

    // Build the WS URL from the current location so it follows
    // wss:// in production behind Traefik and ws:// in local dev.
    var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + window.location.host + '/ws/drafts/status';
    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      // Browser blocked the connection (CSP, mixed content, etc.).
      // Existing 3s polling keeps the page functional.
      return;
    }

    var subscribed = false;

    ws.addEventListener('open', function () {
      try {
        ws.send(JSON.stringify({ type: 'subscribe', draft_id: draftId }));
        subscribed = true;
      } catch (e) {
        // If send fails, fall through to polling.
      }
    });

    ws.addEventListener('message', function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return; // ignore malformed frames
      }
      if (!data || typeof data !== 'object') return;

      // ping events are heartbeats from the server; no UI effect.
      if (data.type === 'ping') return;

      // initial + status both warrant a tracker refresh: pull the
      // server-rendered fragment so we don't re-implement state
      // formatting in JS.
      if (data.type === 'initial' || data.type === 'status') {
        refreshTracker(draftId);

        // Close the socket once the pipeline finishes. The 3s
        // polling will also stop on its own because the server-
        // rendered fragment drops the hx-trigger attributes for
        // terminal states.
        if (data.status && TERMINAL_STATUSES.indexOf(data.status) !== -1) {
          try { ws.close(1000, 'terminal-status'); } catch (e) { /* noop */ }
        }
        return;
      }

      if (data.type === 'error') {
        // Auth or subscription error — the existing polling will
        // still run; just log for debugging.
        if (window.console && console.warn) {
          console.warn('draft-status WS error:', data.message);
        }
        return;
      }
    });

    ws.addEventListener('close', function () {
      // No reconnect: the existing HTMX polling on the wrapper
      // continues to drive updates. Adding reconnect logic here
      // would be redundant and could compete with the polling
      // tick. If the user navigates away and back, this script
      // re-runs from the new page-load init and re-opens the WS.
    });

    ws.addEventListener('error', function () {
      // Same rationale as 'close'.
    });
  }

  function refreshTracker(draftId) {
    var target = document.getElementById('draft-status-' + draftId);
    if (!target) return;
    var url = '/drafts/' + draftId + '/status';

    // Prefer htmx.ajax so the existing HTMX swap pipeline runs
    // (event hooks, transitions, attribute parsing). Fall back to
    // a plain fetch + replace if HTMX isn't loaded yet.
    if (window.htmx && typeof htmx.ajax === 'function') {
      try {
        htmx.ajax('GET', url, { target: '#draft-status-' + draftId, swap: 'outerHTML' });
        return;
      } catch (e) {
        // fall through to plain fetch
      }
    }

    fetch(url, { credentials: 'same-origin', headers: { 'HX-Request': 'true' } })
      .then(function (resp) { return resp.ok ? resp.text() : null; })
      .then(function (html) {
        if (!html) return;
        var holder = document.createElement('div');
        holder.innerHTML = html;
        var fresh = holder.firstElementChild;
        if (fresh && target.parentNode) {
          target.parentNode.replaceChild(fresh, target);
        }
      })
      .catch(function () { /* keep polling fallback */ });
  }

  function init() {
    var marker = document.querySelector('[data-draft-status-ws][data-draft-id]');
    if (!marker) return;
    var draftId = marker.getAttribute('data-draft-id');
    start(draftId);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
