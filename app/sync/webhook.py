"""GitHub webhook receiver for triggering ontology sync on push."""

import hashlib
import hmac
import logging
import os
import threading

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.sync.orchestrator import has_recent_running_row, run_sync
from app.sync.webhook_deliveries import (
    DeliveryRerunResult,
    record_delivery,
    record_delivery_and_request_rerun,
)

logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ONTOLOGY_REPO_FULL_NAME = "henrikaavik/estonian-legal-ontology"

# #853 / comment item 3: cap the request body we will read into memory
# BEFORE reading it. GitHub push payloads are comfortably under a few MB;
# 5 MiB is a generous ceiling that still blocks an attacker from forcing
# an unbounded ``await request.body()`` allocation ahead of (and thus
# bypassing) the signature check.
MAX_WEBHOOK_BODY_BYTES = 5 * 1024 * 1024


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature (SHA256 HMAC).

    #853 / comment item 2: the comparison is done on *bytes*, not str.
    ``hmac.compare_digest`` raises ``TypeError`` when handed two ``str``
    values containing non-ASCII characters — and ``signature`` is the
    attacker-controlled ``X-Hub-Signature-256`` header, so a single
    non-ASCII byte there would otherwise escape this function as an
    unhandled 500. We encode both sides to ``latin-1`` (a total,
    never-raising 1:1 byte mapping for any ``str``) and compare bytes;
    any malformed header simply fails the digest comparison → False.
    """
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    # ``latin-1`` maps every code point 0-255 to a single byte and never
    # raises, so an attacker-supplied non-ASCII signature degrades to a
    # losing comparison instead of a TypeError.
    try:
        return hmac.compare_digest(expected.encode("latin-1"), signature.encode("latin-1"))
    except (UnicodeEncodeError, TypeError):
        # Belt-and-braces: any character outside latin-1 (>U+00FF) can't
        # be a valid hex signature anyway, so reject cleanly.
        return False


def _content_length_ok(request: Request) -> bool:
    """Return False if Content-Length is missing or exceeds the cap.

    We require a declared Content-Length so the size check happens before
    ``await request.body()`` allocates anything. A missing header is
    treated as a rejectable request (GitHub always sends one).
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return False
    try:
        length = int(raw)
    except ValueError:
        return False
    return 0 <= length <= MAX_WEBHOOK_BODY_BYTES


def trigger_sync_background() -> None:
    """Spawn ``run_sync`` in a daemon thread so we can answer GitHub at once.

    Round-3 review (#853): this no longer gates on ``has_recent_running_row``.
    That check is racy, and gating on it reintroduced a stranding hole — two
    webhooks could both pass it, both spawn, and the advisory-lock loser used
    to do nothing useful. We now ALWAYS spawn and let the authoritative
    advisory lock inside ``run_sync`` (#853 / H4) be the sole arbiter:

      * if no sync is running, this spawn acquires the lock and runs;
      * if one is running, this spawn loses the lock and ``run_sync`` itself
        durably queues a coalesced rerun (Finding 1 fix) so the push's
        commit is never lost.

    Callers therefore do not need to inspect a return value to stay correct.
    """
    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()
    logger.info("Sync triggered in background thread")


async def webhook_handler(request: Request) -> JSONResponse:
    """Handle GitHub webhook POST for ontology repo push events."""
    event = request.headers.get("X-GitHub-Event", "")
    signature = request.headers.get("X-Hub-Signature-256", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")

    # #853 / comment item 3: reject oversized / unsized bodies BEFORE we
    # read them into memory (the read also precedes the signature check).
    if not _content_length_ok(request):
        logger.warning(
            "Rejecting webhook: missing or oversized Content-Length (max %d bytes)",
            MAX_WEBHOOK_BODY_BYTES,
        )
        return JSONResponse({"error": "Payload too large or unsized"}, status_code=413)

    body = await request.body()

    # Verify signature — reject if secret is not configured
    if not WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET not configured, rejecting request")
        return JSONResponse({"error": "Webhook not configured"}, status_code=503)
    if not verify_signature(body, signature, WEBHOOK_SECRET):
        logger.warning("Invalid webhook signature")
        return JSONResponse({"error": "Invalid signature"}, status_code=401)

    if event == "ping":
        logger.info("Received GitHub webhook ping")
        return JSONResponse({"status": "pong"})

    if event != "push":
        logger.info("Ignoring non-push event: %s", event)
        return JSONResponse({"status": "ignored", "event": event})

    # Parse push event
    import json

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_name = payload.get("repository", {}).get("full_name", "")
    ref = payload.get("ref", "")

    if repo_name != ONTOLOGY_REPO_FULL_NAME:
        logger.info("Ignoring push from %s", repo_name)
        return JSONResponse({"status": "ignored", "repo": repo_name})

    if ref != "refs/heads/main":
        logger.info("Ignoring push to %s", ref)
        return JSONResponse({"status": "ignored", "ref": ref})

    # #853 H5 + round-2/round-3 review. Replay protection and the
    # coalescing-rerun scheduling both happen here, AFTER the request is
    # proven to be a genuine signature-valid push to the ontology repo's
    # main branch (so irrelevant/replayed events never consume a dedupe row
    # and an attacker can't burn delivery ids without a valid signature).
    #
    # Two paths, split on whether a sync is already running:
    #
    #   * In-progress: record the delivery AND arm the coalesced rerun in
    #     ONE transaction (record_delivery_and_request_rerun). Atomicity is
    #     load-bearing (round-3 Finding 2): if the write fails we return 503
    #     WITHOUT consuming the delivery, so a GitHub manual redelivery — the
    #     only recovery path for pushes — can retry and schedule the rerun.
    #     No fresh sync is spawned; the running sync drains the flag.
    #
    #   * Not running: record the delivery for dedupe, then spawn run_sync
    #     DIRECTLY. We intentionally do not gate the spawn on the (racy)
    #     has_recent_running_row result: run_sync's advisory lock is the sole
    #     arbiter — it either acquires the lock and runs, or loses and
    #     durably queues a rerun itself (round-3 Finding 1). That closes the
    #     "raced into in-progress after our check" window with no redundant
    #     rerun on the common no-contention path.
    if has_recent_running_row():
        result = record_delivery_and_request_rerun(delivery_id, event)
        if result is DeliveryRerunResult.DUPLICATE:
            logger.warning("Rejecting webhook: duplicate delivery %r", delivery_id)
            return JSONResponse({"status": "duplicate"}, status_code=409)
        if result is DeliveryRerunResult.FAILED:
            # Nothing durable — delivery NOT consumed, redelivery will work.
            logger.error(
                "Sync in progress but failed to atomically record+queue resync "
                "for delivery %r (delivery NOT consumed)",
                delivery_id,
            )
            return JSONResponse({"status": "resync_queue_failed"}, status_code=503)
        logger.info("Sync in progress — durable resync queued for delivery %r", delivery_id)
        return JSONResponse({"status": "resync_queued"})

    # No sync running (fast-path): dedupe, then spawn unconditionally.
    if not record_delivery(delivery_id, event):
        logger.warning("Rejecting webhook: duplicate or unrecordable delivery %r", delivery_id)
        return JSONResponse({"status": "duplicate"}, status_code=409)

    logger.info("Ontology repo push to main detected, triggering sync")
    trigger_sync_background()
    return JSONResponse({"status": "sync_triggered"})


def register_webhook_routes(app) -> None:  # type: ignore[no-untyped-def]
    """Register the webhook route on the FastHTML app."""
    app.route("/webhooks/github", methods=["POST"])(webhook_handler)
