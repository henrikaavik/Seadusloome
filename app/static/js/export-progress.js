/* Export progress — WebSocket push listener (#610).
 *
 * Augments the existing 2-10s HTMX polling on the export-status
 * fragment with a WebSocket push so progress updates land within
 * 500ms of the worker writing them. The WS is purely an enhancement:
 * if the connection drops or fails to open at all, the existing
 * hx-trigger="every Ns" attribute on .export-status keeps polling
 * and drives the UI to the success/failure terminal state exactly
 * as before. We deliberately do NOT remove those attributes — the
 * polling fallback is the graceful-degradation path the issue's
 * Definition of Done requires.
 *
 * Marker contract: the export-status fragment renders a marker
 * <div data-export-progress-ws data-job-id="..." data-draft-id="...">
 * with a child <progress> + label that the script updates on every
 * push event. The script self-initialises on DOMContentLoaded and
 * also on every htmx:afterSwap so it picks up the marker the moment
 * the export form returns the spinner fragment via HTMX.
 *
 * The WS closes itself once the job reaches a terminal status
 * (success / failed) so the connection doesn't linger forever after
 * the .docx is ready.
 */
(function () {
  'use strict';

  // Tracks markers we've already wired up so re-running init() after
  // an HTMX swap doesn't open a second WebSocket for the same job.
  var ATTACHED_FLAG = 'data-export-progress-attached';

  function start(marker) {
    if (typeof WebSocket === 'undefined') return;
    if (marker.hasAttribute(ATTACHED_FLAG)) return;

    var jobId = marker.getAttribute('data-job-id');
    var draftId = marker.getAttribute('data-draft-id');
    if (!jobId || !draftId) return;

    marker.setAttribute(ATTACHED_FLAG, '1');

    var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + window.location.host + '/ws/drafts/export-progress';
    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      // Browser blocked the connection (CSP, mixed content, etc.).
      // Existing 2-10s polling keeps the page functional.
      return;
    }

    ws.addEventListener('open', function () {
      try {
        ws.send(JSON.stringify({
          type: 'subscribe',
          draft_id: draftId,
          job_id: parseInt(jobId, 10)
        }));
      } catch (e) {
        // Send failure is fine — polling keeps driving the UI.
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

      if (data.type === 'ping') return;

      if (data.type === 'initial' || data.type === 'progress') {
        applyProgress(marker, data.current, data.total);
        return;
      }

      if (data.type === 'terminal') {
        // The HTTP polling tick will catch the success/failed state
        // within at most _EXPORT_POLLING_TIMEOUT_SECONDS / 2 seconds
        // and replace the spinner with the download button or alert.
        // We just close the socket here.
        try { ws.close(1000, 'terminal-status'); } catch (e) { /* noop */ }
        return;
      }

      if (data.type === 'timeout') {
        // The HTTP polling fallback will surface the "Vajab tähelepanu"
        // warning via the existing 300s budget. Nothing to do here.
        try { ws.close(1000, 'timeout'); } catch (e) { /* noop */ }
        return;
      }

      if (data.type === 'error') {
        if (window.console && console.warn) {
          console.warn('export-progress WS error:', data.message);
        }
        try { ws.close(1000, 'error'); } catch (e) { /* noop */ }
        return;
      }
    });

    ws.addEventListener('close', function () {
      // No reconnect: the existing HTMX polling on the fragment
      // continues to drive updates. Adding reconnect logic here would
      // be redundant and could compete with the polling tick. If the
      // user navigates away and back, this script re-runs from the
      // new page-load init and re-opens the WS.
      marker.removeAttribute(ATTACHED_FLAG);
    });

    ws.addEventListener('error', function () {
      // Same rationale as 'close'.
    });
  }

  function applyProgress(marker, current, total) {
    var progressEl = marker.querySelector('progress.export-progress-bar');
    var labelEl = marker.querySelector('.export-progress-label');
    var hasNumeric = (typeof current === 'number' && typeof total === 'number' && total > 0);

    if (progressEl) {
      if (hasNumeric) {
        progressEl.max = total;
        progressEl.value = current;
        progressEl.removeAttribute('data-indeterminate');
      } else {
        // Indeterminate: drop the value so the bar pulses instead.
        progressEl.removeAttribute('value');
        progressEl.setAttribute('data-indeterminate', '1');
      }
    }

    if (labelEl) {
      if (hasNumeric) {
        var pct = Math.min(100, Math.round((current / total) * 100));
        labelEl.textContent = pct + '%';
      }
      // If we have no numeric data, leave the existing fallback text
      // ("Eksport käimas...") in place.
    }
  }

  function init(root) {
    var scope = root || document;
    var markers = scope.querySelectorAll('[data-export-progress-ws][data-job-id]');
    for (var i = 0; i < markers.length; i++) {
      start(markers[i]);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(); });
  } else {
    init();
  }

  // HTMX swaps in the export-status fragment after the user clicks
  // "Laadi alla .docx", so we re-scan the swapped subtree to pick up
  // the marker.
  document.addEventListener('htmx:afterSwap', function (evt) {
    var target = evt && evt.target ? evt.target : null;
    init(target);
  });
})();
