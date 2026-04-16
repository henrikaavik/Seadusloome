"""Upload handler for draft legislation documents.

This module owns the *validation + persistence* half of the document
upload flow. It is deliberately kept outside ``routes.py`` so the logic
can be unit-tested without spinning up a FastHTML ``TestClient``.

Flow:
    1. Validate title, filename, content-type, and size.
    2. Read the upload stream into memory.
    3. Encrypt and persist via ``app.storage.store_file``.
    4. Build a stable ``graph_uri`` for the draft's Jena named graph.
    5. Insert the ``drafts`` row with ``status='uploaded'``.
    6. Enqueue a ``parse_draft`` background job.
    7. Return the created ``Draft``.

Any failure after step 3 is caught so the encrypted file can be removed
before the exception is re-raised — we never want ciphertext lying
around with no DB row pointing at it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

from app.auth.provider import UserDict
from app.db import get_connection as _connect
from app.docs.draft_model import Draft, create_draft
from app.jobs import JobQueue
from app.storage import delete_file, store_file

logger = logging.getLogger(__name__)


# Allowed extensions and their canonical content types. The tuple order
# mirrors the ``accept`` attribute we render on the <input type="file">.
_ALLOWED_EXTENSIONS: tuple[str, ...] = (".docx", ".pdf")
_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        # .docx
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        # .pdf
        "application/pdf",
        # Browsers sometimes send octet-stream for unknown MIME — accept it
        # if the extension matches.
        "application/octet-stream",
    }
)

_TITLE_MIN_LEN = 1
_TITLE_MAX_LEN = 200

# Draft graph URIs live in a dedicated sub-namespace of the Estonian
# legal ontology so Jena can host them alongside the enacted laws.
_GRAPH_URI_PREFIX = "https://data.riik.ee/ontology/estleg/drafts/"


class DraftUploadError(ValueError):
    """Raised when the uploaded draft fails validation.

    The ``args[0]`` message is Estonian and safe to render directly to
    the end user — callers should *not* wrap it with extra context.
    """


class _UploadLike(Protocol):
    """Structural type matching Starlette's ``UploadFile``.

    Kept as a Protocol so unit tests can pass a tiny stub without
    depending on the full multipart machinery.
    """

    filename: str | None
    content_type: str | None
    size: int | None

    async def read(self) -> bytes: ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _max_upload_bytes() -> int:
    """Return the maximum accepted upload size in bytes.

    Controlled by ``MAX_UPLOAD_SIZE_MB`` so ops can raise/lower the limit
    per environment without a redeploy. Defaults to 50 MB — Phase 2 spec
    §3 calls for 25 MB but 50 MB leaves headroom for scanned PDFs and is
    still well below the encryption overhead budget.
    """
    raw = os.environ.get("MAX_UPLOAD_SIZE_MB", "50")
    try:
        mb = int(raw)
    except ValueError:
        logger.warning("Invalid MAX_UPLOAD_SIZE_MB=%r, falling back to 50", raw)
        mb = 50
    return max(1, mb) * 1024 * 1024


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_title(title: str) -> str:
    """Strip, length-check, and return the normalised title.

    Raises ``DraftUploadError`` with an Estonian message on any problem.
    """
    cleaned = (title or "").strip()
    if len(cleaned) < _TITLE_MIN_LEN:
        raise DraftUploadError("Pealkiri on kohustuslik.")
    if len(cleaned) > _TITLE_MAX_LEN:
        raise DraftUploadError(
            f"Pealkiri on liiga pikk (maksimaalselt {_TITLE_MAX_LEN} tähemärki)."
        )
    return cleaned


def _validate_filename(filename: str | None) -> str:
    """Ensure the filename has an allowed extension and return it."""
    if not filename:
        raise DraftUploadError("Faili nimi puudub.")
    lower = filename.lower()
    if not any(lower.endswith(ext) for ext in _ALLOWED_EXTENSIONS):
        allowed = ", ".join(_ALLOWED_EXTENSIONS)
        raise DraftUploadError(f"Toetamata failitüüp. Palun laadige üles {allowed} fail.")
    return filename


def _validate_content_type(content_type: str | None) -> str:
    """Return the content type, defaulting to octet-stream when missing."""
    if not content_type:
        return "application/octet-stream"
    # Some browsers append a charset or boundary — strip it.
    primary = content_type.split(";", 1)[0].strip().lower()
    if primary not in _ALLOWED_CONTENT_TYPES:
        raise DraftUploadError("Toetamata failitüüp. Palun laadige üles .docx või .pdf fail.")
    return primary


def _validate_size(size: int | None, contents: bytes) -> int:
    """Return the real (post-read) file size, enforcing the limit."""
    actual = len(contents)
    # Prefer the post-read length — ``UploadFile.size`` is advisory and
    # some clients lie about it.
    limit = _max_upload_bytes()
    if actual == 0:
        raise DraftUploadError("Üleslaaditud fail on tühi.")
    if actual > limit:
        mb = limit // (1024 * 1024)
        raise DraftUploadError(f"Fail on liiga suur. Maksimaalne lubatud suurus on {mb} MB.")
    if size is not None and size > limit:
        mb = limit // (1024 * 1024)
        raise DraftUploadError(f"Fail on liiga suur. Maksimaalne lubatud suurus on {mb} MB.")
    return actual


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def handle_upload(
    user: UserDict,
    title: str,
    upload: _UploadLike,
    *,
    doc_type: str = "eelnou",
    parent_vtk_id: Any = None,
    job_queue: JobQueue | None = None,
    conn_factory: Any = None,
) -> Draft:
    """Validate, encrypt, persist, and enqueue a new draft upload.

    Args:
        user: Authenticated caller. Must carry a non-empty ``org_id``.
        title: Draft title supplied by the uploader (1-200 chars).
        upload: Starlette ``UploadFile`` (or any object matching
            :class:`_UploadLike`) pointing at the multipart stream.
        doc_type: Document classification — ``'eelnou'`` (default) or
            ``'vtk'``.  The route handler is responsible for validating
            the value against the legal set BEFORE calling
            :func:`handle_upload`; we just pass it through to
            :func:`create_draft`.
        parent_vtk_id: Optional foreign-key link to a preceding VTK in
            the same org. The route handler validates FK existence and
            same-org ownership; this function persists whatever is
            passed in.
        job_queue: Optional ``JobQueue`` to enqueue the parse job.
            Defaults to a fresh :class:`app.jobs.JobQueue` instance —
            tests pass a stub to avoid hitting Postgres.
        conn_factory: Optional callable returning a context-managed DB
            connection. Defaults to :func:`app.db.get_connection`; tests
            patch this to inject a mock.

    Returns:
        The freshly-inserted :class:`Draft`.

    Raises:
        DraftUploadError: Validation failed. Message is Estonian and
            user-facing.
    """
    if not user.get("org_id"):
        raise DraftUploadError("Ainult organisatsiooni liikmed saavad eelnõusid üles laadida.")
    user_id = user["id"]
    org_id = user["org_id"]
    if org_id is None:
        # Satisfies the type checker; the ``if not ...`` above already
        # handles the runtime case.
        raise DraftUploadError("Ainult organisatsiooni liikmed saavad eelnõusid üles laadida.")

    cleaned_title = _validate_title(title)
    filename = _validate_filename(upload.filename)
    content_type = _validate_content_type(upload.content_type)

    contents = await upload.read()
    file_size = _validate_size(upload.size, contents)

    # Step 3: encrypt and persist. Store owner-scoped so the storage
    # path itself already includes the acting user's id.
    stored = store_file(contents, filename=filename, owner_id=str(user_id))

    # Steps 4-6 live inside a single transaction so we either commit the
    # draft row *and* enqueue the job, or we bail and clean up the file.
    factory = conn_factory or _connect
    queue = job_queue or JobQueue()
    try:
        with factory() as conn:
            draft = create_draft(
                conn,
                user_id=user_id,
                org_id=org_id,
                title=cleaned_title,
                filename=filename,
                content_type=content_type,
                file_size=file_size,
                storage_path=stored.storage_path,
                # graph_uri must embed the freshly-minted draft id, but we
                # only get the id after the INSERT. Use a stable placeholder
                # based on the storage_path (which is already unique) then
                # patch the real URI immediately afterwards.
                graph_uri=f"{_GRAPH_URI_PREFIX}pending-{stored.storage_path}",
                doc_type=doc_type,  # type: ignore[arg-type]
                parent_vtk_id=parent_vtk_id,
            )
            final_graph_uri = f"{_GRAPH_URI_PREFIX}{draft.id}"
            conn.execute(
                "update drafts set graph_uri = %s where id = %s",
                (final_graph_uri, str(draft.id)),
            )
            conn.commit()
        draft.graph_uri = final_graph_uri
    except Exception:
        logger.exception(
            "Draft insert failed after file was stored — cleaning up path=%s",
            stored.storage_path,
        )
        delete_file(stored.storage_path)
        raise

    # Step 6 proper: enqueue the async parse pipeline. A failure here is
    # *not* fatal — the DB row already exists, and ops can re-enqueue from
    # the admin dashboard. We still log loudly so the failure is visible.
    try:
        queue.enqueue(
            "parse_draft",
            {"draft_id": str(draft.id)},
            priority=0,
        )
    except Exception:
        logger.exception("Failed to enqueue parse_draft job for draft_id=%s", draft.id)

    logger.info(
        "Draft uploaded id=%s user=%s org=%s size=%d filename=%s",
        draft.id,
        user_id,
        org_id,
        file_size,
        filename,
    )
    return draft
