"""Socket-count guard for ``app/static/js/export-progress.js`` (#856).

The export-status fragment polls via HTMX with ``hx-swap="outerHTML"``
every 2-10s. Before #856 the JS keyed its "already attached" state on a
marker-element attribute, which the swap wiped — so every poll tick
opened ANOTHER WebSocket (and another server-side DB-watcher loop):
~15 concurrent sockets over a single 30s export. The fix is a
module-level ``Map`` keyed by job id that survives element swaps.

Two layers of coverage:

1. A static source guard (repo convention — see
   ``tests/test_chat_js_ontology_rewrite.py``: "no JS test runner in
   this repo") that fails loudly if the Map registry or the live-marker
   re-query is removed.
2. A functional harness that runs the real script under ``node`` with
   a ~60-line DOM/WebSocket stub and counts constructed sockets across
   simulated HTMX poll swaps. Skipped when node is unavailable
   (GitHub's ubuntu runners and dev machines have it; the static guard
   above still runs everywhere).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_JS_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "export-progress.js"


def _source() -> str:
    return _JS_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Static source guard
# ---------------------------------------------------------------------------


def test_export_progress_js_exists():
    assert _JS_PATH.is_file(), f"missing {_JS_PATH}"


def test_registry_is_a_module_level_map_keyed_by_job_id():
    src = _source()
    assert "socketsByJob" in src, "module-level socket registry removed"
    assert "new Map()" in src
    # The dedupe decision happens BEFORE a new WebSocket is opened.
    get_idx = src.index("socketsByJob.get(jobId)")
    open_idx = src.index("new WebSocket(url)")
    assert get_idx < open_idx, "registry lookup must precede socket construction"
    # Live sockets short-circuit on OPEN/CONNECTING readyState.
    assert "WebSocket.OPEN" in src
    assert "WebSocket.CONNECTING" in src


def test_registry_slot_released_on_close():
    src = _source()
    assert "socketsByJob.delete(jobId)" in src, (
        "close handler must release the registry slot so a later poll "
        "swap can re-open a socket for a still-running job"
    )


def test_updates_target_the_live_marker_not_the_construction_time_one():
    src = _source()
    # The marker a socket was opened from is swapped out by HTMX; every
    # frame must re-resolve the current marker from the document.
    assert "function liveMarker" in src
    assert "document.querySelector(" in src


def test_attribute_based_attached_flag_is_gone():
    # The per-element flag was the bug: outerHTML swaps reset it.
    assert "data-export-progress-attached" not in _source()


# ---------------------------------------------------------------------------
# 2. Functional socket-count harness (node)
# ---------------------------------------------------------------------------

_NODE_DRIVER = r"""
'use strict';
const fs = require('fs');
const scriptPath = process.argv[2];

// --- DOM / WebSocket stubs -------------------------------------------------
let currentMarkers = [];
const docListeners = {};

function makeProgressEl() {
  return {
    max: null,
    value: null,
    attrs: {},
    removeAttribute(n) { delete this.attrs[n]; },
    setAttribute(n, v) { this.attrs[n] = String(v); }
  };
}

class FakeElement {
  constructor(attrs) {
    this.attrs = Object.assign({}, attrs);
    this.progressEl = makeProgressEl();
    this.labelEl = { textContent: '' };
  }
  getAttribute(n) {
    return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null;
  }
  hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  removeAttribute(n) { delete this.attrs[n]; }
  querySelector(sel) {
    if (sel.indexOf('progress') === 0) return this.progressEl;
    if (sel.indexOf('.export-progress-label') === 0) return this.labelEl;
    return null;
  }
}

global.document = {
  readyState: 'complete',
  addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  querySelectorAll() { return currentMarkers.slice(); },
  querySelector(sel) {
    const m = /data-job-id="([^"]+)"/.exec(sel);
    if (!m) return null;
    for (const el of currentMarkers) {
      if (el.getAttribute('data-job-id') === m[1]) return el;
    }
    return null;
  }
};

global.window = { location: { protocol: 'http:', host: 'testserver' }, console: console };

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.listeners = {};
    this.sentFrames = [];
    FakeWebSocket.instances.push(this);
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  send(d) { this.sentFrames.push(d); }
  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this._fire('close', {});
  }
  _fire(type, evt) { (this.listeners[type] || []).forEach(fn => fn(evt)); }
  _open() { this.readyState = FakeWebSocket.OPEN; this._fire('open', {}); }
  _message(obj) { this._fire('message', { data: JSON.stringify(obj) }); }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];
global.WebSocket = FakeWebSocket;

// --- Load the script under test --------------------------------------------
// eval() is intentional and safe here: this test harness executes our
// OWN first-party static asset (app/static/js/export-progress.js — the
// path is pinned by the pytest wrapper, no untrusted input) inside the
// stubbed browser globals above. The file is a browser IIFE, not a
// CommonJS module, so require() cannot load it.
eval(fs.readFileSync(scriptPath, 'utf8'));

function makeMarker(jobId) {
  return new FakeElement({
    'data-export-progress-ws': '',
    'data-job-id': String(jobId),
    'data-draft-id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
  });
}

function fireSwap(markers) {
  currentMarkers = markers.slice();
  const target = { querySelectorAll: () => markers.slice() };
  (docListeners['htmx:afterSwap'] || []).forEach(fn => fn({ target }));
}

const out = {};

// Initial fragment renders the marker for job 42.
const first42 = makeMarker(42);
fireSwap([first42]);
out.after_first_swap = FakeWebSocket.instances.length;

// Handshake completes; subscribe frame goes out.
FakeWebSocket.instances[0]._open();
out.subscribe_frames = FakeWebSocket.instances[0].sentFrames.length;

// Five HTMX poll ticks each swap in a FRESH marker element for the
// SAME job — the pre-#856 bug opened a new socket on every one.
let latest42 = first42;
for (let i = 0; i < 5; i++) {
  latest42 = makeMarker(42);
  fireSwap([latest42]);
}
out.sockets_after_five_poll_swaps = FakeWebSocket.instances.length;

// A progress frame must update the LIVE marker (the latest swap), not
// the long-gone element the socket was opened from.
FakeWebSocket.instances[0]._message({ type: 'progress', current: 6, total: 10 });
out.live_marker_value = latest42.progressEl.value;
out.live_marker_max = latest42.progressEl.max;
out.stale_marker_value = first42.progressEl.value;

// A different job id gets its own socket.
const markerB = makeMarker(77);
fireSwap([latest42, markerB]);
out.sockets_after_second_job = FakeWebSocket.instances.length;

// Terminal frame: the socket closes itself and releases its registry
// slot, so a later swap (job still rendered while polling catches up)
// may open a replacement.
FakeWebSocket.instances[0]._message({ type: 'terminal' });
out.job42_socket_state_after_terminal = FakeWebSocket.instances[0].readyState;
fireSwap([makeMarker(42), markerB]);
out.sockets_after_post_close_swap = FakeWebSocket.instances.length;

console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_poll_swaps_do_not_accumulate_sockets(tmp_path: Path):
    driver = tmp_path / "driver.js"
    driver.write_text(_NODE_DRIVER, encoding="utf-8")

    proc = subprocess.run(
        ["node", str(driver), str(_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}\n{proc.stdout}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # One socket on first render, exactly one subscribe frame.
    assert out["after_first_swap"] == 1
    assert out["subscribe_frames"] == 1

    # THE bug being guarded: five outerHTML poll swaps for the same job
    # must NOT open five more sockets (pre-#856 behaviour: 6 here).
    assert out["sockets_after_five_poll_swaps"] == 1, (
        f"HTMX poll swaps accumulated sockets: {out['sockets_after_five_poll_swaps']} "
        f"open for one job after 5 swaps"
    )

    # Frames land on the marker currently in the DOM, not the stale one.
    assert out["live_marker_value"] == 6
    assert out["live_marker_max"] == 10
    assert out["stale_marker_value"] is None

    # Distinct jobs still get distinct sockets.
    assert out["sockets_after_second_job"] == 2

    # After a terminal close the slot is released — a later swap may
    # legitimately open a replacement socket (e.g. transient drop).
    assert out["job42_socket_state_after_terminal"] == 3  # CLOSED
    assert out["sockets_after_post_close_swap"] == 3
