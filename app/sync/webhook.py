"""GitHub webhook receiver for triggering ontology sync on push."""

import hashlib
import hmac
import logging
import os
import threading

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.sync.orchestrator import has_recent_running_row, run_sync
from app.sync.webhook_deliveries import record_delivery

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


def trigger_sync_background() -> bool:
    """Run sync in a background thread so we can respond to GitHub immediately.

    Returns ``False`` if another sync is already in flight (detected via
    sync_log.status='running') and the request should be treated as a
    no-op. Returns ``True`` when a new sync thread has been spawned.

    NOTE: the ``has_recent_running_row`` check here is only a fast-path
    hint; the authoritative cross-process mutual exclusion is the
    advisory lock inside ``run_sync`` (#853 / H4), so even if two webhooks
    slip past this check only one will actually proceed.
    """
    if has_recent_running_row():
        logger.info("Webhook sync skipped — another sync is already running")
        return False
    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()
    logger.info("Sync triggered in background thread")
    return True


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

    # #853 / H5: replay protection. Only AFTER the request is proven to be
    # a genuine, signature-valid push to the ontology repo's main branch
    # do we record its delivery id and refuse duplicates. Doing this last
    # means a replayed-but-irrelevant event (ping, other repo) never
    # consumes a dedupe row, and an attacker can't burn delivery ids for
    # pushes we'd act on without a valid signature.
    if not record_delivery(delivery_id, event):
        logger.warning("Rejecting webhook: duplicate or unrecordable delivery %r", delivery_id)
        return JSONResponse({"status": "duplicate"}, status_code=409)

    logger.info("Ontology repo push to main detected, triggering sync")
    started = trigger_sync_background()

    if not started:
        return JSONResponse({"status": "sync_in_progress"})
    return JSONResponse({"status": "sync_triggered"})


def register_webhook_routes(app) -> None:  # type: ignore[no-untyped-def]
    """Register the webhook route on the FastHTML app."""
    app.route("/webhooks/github", methods=["POST"])(webhook_handler)
