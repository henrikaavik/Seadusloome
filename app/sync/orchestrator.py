"""Sync pipeline orchestrator: GitHub → RDF → validate → Jena Fuseki."""

import logging
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.db import get_connection
from app.sync.converter import convert_ontology, serialize_to_turtle
from app.sync.jena_loader import clear_default_graph, get_triple_count, upload_turtle
from app.sync.validator import load_shapes, validate_graph

# Lazy import to avoid circular dependencies; used for WS notifications.
_notify_sync: object | None = None


def _get_notify_fn():  # type: ignore[no-untyped-def]
    """Lazily import the sync-complete notifier."""
    global _notify_sync  # noqa: PLW0603
    if _notify_sync is None:
        from app.explorer.websocket import notify_sync_complete_sync

        _notify_sync = notify_sync_complete_sync
    return _notify_sync


logger = logging.getLogger(__name__)

ONTOLOGY_REPO = "https://github.com/henrikaavik/estonian-legal-ontology.git"


def log_sync(
    status: str,
    started_at: datetime,
    entity_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Write a sync_log entry to PostgreSQL."""
    try:
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO sync_log
                   (started_at, finished_at, status, entity_count, error_message)
                   VALUES (%s, %s, %s, %s, %s)""",  # type: ignore[arg-type]
                (started_at, datetime.now(UTC), status, entity_count, error_message),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to write sync log")


def clone_or_pull(target_dir: Path) -> None:
    """Clone or pull the ontology repo."""
    if (target_dir / ".git").exists():
        logger.info("Pulling latest changes...")
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=target_dir,
            check=True,
            capture_output=True,
        )
    else:
        logger.info("Cloning ontology repo...")
        subprocess.run(
            ["git", "clone", "--depth", "1", ONTOLOGY_REPO, str(target_dir)],
            check=True,
            capture_output=True,
        )


def run_sync(repo_dir: Path | None = None) -> bool:
    """Execute the full sync pipeline.

    Steps:
    1. Clone/pull ontology repo
    2. Convert JSON-LD → RDF
    3. Validate with SHACL shapes
    4. Clear default graph in Jena
    5. Upload new RDF data
    6. Log result

    Args:
        repo_dir: Path to existing ontology repo clone. If None, clones to temp dir.

    Returns:
        True if sync succeeded.
    """
    started_at = datetime.now(UTC)
    logger.info("Starting sync pipeline at %s", started_at.isoformat())

    use_temp = repo_dir is None
    if use_temp:
        temp_dir = tempfile.mkdtemp(prefix="ontology-sync-")
        repo_dir = Path(temp_dir)

    try:
        # Step 1: Clone or pull
        clone_or_pull(repo_dir)

        # Step 2: Convert JSON-LD → RDF
        logger.info("Converting JSON-LD to RDF...")
        graph = convert_ontology(repo_dir)
        entity_count = len(graph)
        logger.info("Converted %d triples", entity_count)

        # Step 3: Validate with SHACL (if shapes exist)
        shapes_dir = repo_dir / "shacl"
        if shapes_dir.exists() and any(shapes_dir.iterdir()):
            logger.info("Validating with SHACL shapes...")
            shapes = load_shapes(shapes_dir)
            if len(shapes) > 0:
                conforms, report = validate_graph(graph, shapes)
                if not conforms:
                    logger.error("SHACL validation failed, aborting sync")
                    log_sync("failed", started_at, error_message=report[:1000])
                    return False
        else:
            logger.info("No SHACL shapes found, skipping validation")

        # Step 4: Serialize to Turtle
        logger.info("Serializing to Turtle...")
        turtle = serialize_to_turtle(graph)

        # Step 5: Clear and upload
        logger.info("Clearing default graph...")
        clear_default_graph()

        logger.info("Uploading to Jena Fuseki...")
        success = upload_turtle(turtle)
        if not success:
            log_sync("failed", started_at, error_message="Upload to Jena failed")
            return False

        # Step 6: Verify
        final_count = get_triple_count()
        logger.info("Sync complete. %d triples in Jena.", final_count)

        log_sync("success", started_at, entity_count=final_count)

        # Notify connected explorer WebSocket clients.
        try:
            fn = _get_notify_fn()
            if callable(fn):
                fn()
        except Exception:
            logger.debug("WS notification after sync failed (non-critical)")

        return True

    except Exception as e:
        logger.exception("Sync pipeline failed")
        log_sync("failed", started_at, error_message=str(e)[:1000])
        return False

    finally:
        if use_temp and repo_dir:
            shutil.rmtree(repo_dir, ignore_errors=True)
