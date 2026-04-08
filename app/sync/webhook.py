"""GitHub webhook receiver for triggering ontology sync on push."""

import hashlib
import hmac
import logging
import os
import threading

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.sync.orchestrator import run_sync

logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ONTOLOGY_REPO_FULL_NAME = "henrikaavik/estonian-legal-ontology"


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature (SHA256 HMAC)."""
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def trigger_sync_background() -> None:
    """Run sync in a background thread so we can respond to GitHub immediately."""
    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()
    logger.info("Sync triggered in background thread")


async def webhook_handler(request: Request) -> JSONResponse:
    """Handle GitHub webhook POST for ontology repo push events."""
    event = request.headers.get("X-GitHub-Event", "")
    signature = request.headers.get("X-Hub-Signature-256", "")

    body = await request.body()

    # Verify signature if secret is configured
    if WEBHOOK_SECRET:
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

    logger.info("Ontology repo push to main detected, triggering sync")
    trigger_sync_background()

    return JSONResponse({"status": "sync_triggered"})


def register_webhook_routes(app) -> None:  # type: ignore[no-untyped-def]
    """Register the webhook route on the FastHTML app."""
    app.route("/webhooks/github", methods=["POST"])(webhook_handler)
