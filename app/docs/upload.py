"""Upload handler for draft legislation documents.

This module owns the *validation + persistence* half of the document
upload flow. It is deliberately kept outside ``routes.py`` so the logic
can be unit-tested without spinning up a FastHTML ``TestClient``.

Flow (new-draft branch):
    1. Validate title, filename, content-type, and the *declared* size.
    2. Read the upload stream incrementally with a hard byte cap, then
       sniff magic bytes + run the .docx zip-bomb caps (#858).
    3. Encrypt and persist via ``app.storage.store_file``.
    4. Build a stable ``graph_uri`` for the draft's Jena named graph.
    5. Insert the ``drafts`` row with ``status='uploaded'``.
    5b. Insert a v1 ``draft_versions`` row in the SAME transaction so
        the read JOIN in :func:`app.docs.draft_model.get_draft` always
        finds a backing version (#618 PR-B).
    6. Enqueue a ``parse_draft`` background job.
    7. Return the created ``Draft``.

Flow (new-version branch, #618 PR-B):
    When ``parent_draft_id`` is supplied, the handler creates a NEW
    ``draft_versions`` row tied to the EXISTING parent draft instead
    of a brand-new ``drafts`` row.  The new version inherits the
    parent's ``owner_id`` / ``org_id`` (cross-org uploads are
    rejected) and steps the parent's latest ``reading_stage`` one
    notch forward via :func:`app.docs.version_model.next_reading_stage`.
    The fresh encrypted file path becomes the version's
    ``storage_path``; ``graph_uri`` is allocated per §9.5 as
    ``...drafts/{parent_id}/v{version_number}``.

Any failure after step 3 is caught so the encrypted file can be removed
before the exception is re-raised — we never want ciphertext lying
around with no DB row pointing at it.
"""

from __future__ import annotations

import io
import logging
import uuid
import zipfile
from typing import Any, Protocol

import psycopg

from app import config
from app.auth.audit import log_action
from app.auth.provider import UserDict
from app.db import get_connection as _connect
from app.docs.draft_model import Draft, create_draft, get_draft
from app.docs.version_model import (
    create_draft_version,
    get_latest_version,
    get_next_version_number,
    next_reading_stage,
)
from app.jobs import JobQueue
from app.storage import delete_file, store_file

logger = logging.getLogger(__name__)

# How many times to re-run the v2+ version-creation transaction when it
# trips the ``UNIQUE(draft_id, version_number)`` constraint (#745).  The
# advisory lock in :func:`app.docs.version_model.get_next_version_number`
# should make a collision practically impossible, but a belt-and-braces
# retry turns any residual race into a transparent second attempt rather
# than a 500.  A tiny budget is enough — each retry re-reads ``MAX`` after
# the conflicting transaction has committed, so the next number is free.
_MAX_VERSION_ALLOC_ATTEMPTS = 3


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

# ---------------------------------------------------------------------------
# Resource bounds + content sniffing (#858)
# ---------------------------------------------------------------------------

# Incremental read window for the bounded upload reader. 1 MiB keeps the
# per-iteration allocation small while still finishing a 50 MB upload in
# ~50 read calls.
_READ_CHUNK_BYTES = 1024 * 1024

# Magic bytes of the two accepted formats. A .docx is an OOXML ZIP and
# always begins with a ZIP local-file-header; a PDF must carry the
# ``%PDF-`` header near the start (the spec tolerates a small preamble,
# so we scan the first KiB rather than offset 0 only).
_DOCX_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF-"
_PDF_MAGIC_SCAN_BYTES = 1024

# Zip-bomb guards for .docx (#858). The *claimed* uncompressed size comes
# from the ZIP central directory (stdlib ``zipfile`` — no decompression
# needed). Two independent caps:
#
#   * absolute: total claimed uncompressed size may not exceed
#     ``_DOCX_UNCOMPRESSED_MULTIPLIER × max_upload_bytes()`` (tracks the
#     ops-tunable upload limit, 500 MB at the 50 MB default);
#   * ratio: above ``_RATIO_CHECK_FLOOR_BYTES`` uncompressed, the
#     expansion ratio may not exceed ``_MAX_DOCX_COMPRESSION_RATIO``.
#     Real-world OOXML compresses ~5-20x; sparse XML can reach ~50x.
#     The 10 MiB floor keeps tiny-but-highly-compressible legitimate
#     documents out of the ratio police.
_DOCX_UNCOMPRESSED_MULTIPLIER = 10
_MAX_DOCX_COMPRESSION_RATIO = 100
_RATIO_CHECK_FLOOR_BYTES = 10 * 1024 * 1024

# User-facing Estonian messages for the content checks. Module-level so
# tests (and a future i18n pass) reference the exact strings.
MSG_CONTENT_MISMATCH_DOCX = (
    "Faili sisu ei vasta .docx vormingule. Palun laadige üles korrektne Wordi dokument."
)
MSG_CONTENT_MISMATCH_PDF = (
    "Faili sisu ei vasta .pdf vormingule. Palun laadige üles korrektne PDF-dokument."
)
MSG_DOCX_CORRUPT = (
    "DOCX-fail on vigane või rikutud. Palun salvestage dokument uuesti ja proovige siis uuesti."
)
MSG_DOCX_BOMB = (
    "DOCX-faili pakitud sisu on lubatust palju suurem. "
    "Palun jagage dokument väiksemateks failideks."
)

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

    ``read`` mirrors Starlette's incremental signature (#858): the
    bounded reader in :func:`_read_bounded` always passes an explicit
    chunk size so an oversized upload is rejected after at most
    ``max_upload_bytes() + 1`` buffered bytes instead of being slurped
    whole into memory first.
    """

    filename: str | None
    content_type: str | None
    size: int | None

    async def read(self, size: int = -1, /) -> bytes: ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _max_upload_mb() -> int:
    """Return the configured ``MAX_UPLOAD_SIZE_MB`` clamped to ``[1, ∞)``.

    Controlled by ``MAX_UPLOAD_SIZE_MB`` so ops can raise/lower the limit
    per environment without a redeploy. Defaults to 50 MB — Phase 2 spec
    §3 calls for 25 MB but 50 MB leaves headroom for scanned PDFs and is
    still well below the encryption overhead budget.
    """
    return config.env_int("MAX_UPLOAD_SIZE_MB")


def max_upload_bytes() -> int:
    """Return the maximum accepted upload size in bytes.

    Single source of truth shared between server-side validation
    (:func:`_reject_oversize_declared` / :func:`_read_bounded`) and the
    upload-form UI (#776). Reads
    ``MAX_UPLOAD_SIZE_MB`` from the environment on every call so a
    runtime override (e.g. ``monkeypatch.setenv`` in tests, or a Coolify
    env-var change) is picked up without a restart.
    """
    return _max_upload_mb() * 1024 * 1024


def max_upload_mb_display() -> str:
    """Return the configured upload size as a user-facing ``"<N> MB"`` label.

    Companion helper to :func:`max_upload_bytes` — both derive from the
    same ``MAX_UPLOAD_SIZE_MB`` read so the JS byte constant and the
    Estonian copy in the upload form / listing page stay in lockstep
    (#776).
    """
    return f"{_max_upload_mb()} MB"


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


def _too_large_error() -> DraftUploadError:
    """Build the canonical Estonian oversize rejection."""
    label = max_upload_mb_display()
    return DraftUploadError(f"Fail on liiga suur. Maksimaalne lubatud suurus on {label}.")


def _reject_oversize_declared(size: int | None) -> None:
    """Reject on the *declared* size BEFORE any bytes are read (#858).

    ``UploadFile.size`` carries the multipart part's Content-Length-
    equivalent (Starlette counts the bytes while spooling the part).
    A declared size over the limit is rejected up front so we never
    pull an oversized spool into process memory at all. A missing /
    lying declaration is still caught by the bounded read below.
    """
    if size is not None and size > max_upload_bytes():
        raise _too_large_error()


async def _read_bounded(upload: _UploadLike) -> bytes:
    """Read the upload incrementally with a hard cap (#858).

    Reads at most ``max_upload_bytes() + 1`` bytes in
    :data:`_READ_CHUNK_BYTES` windows — the extra byte is only there to
    detect "stream continues past the limit". The moment the running
    total exceeds the limit we raise, so process memory never holds
    more than ``limit + 1`` bytes of an attacker-sized body (the old
    code slurped the whole stream first and size-checked afterwards).

    Raises:
        DraftUploadError: Oversized stream, or a fully-empty stream.
    """
    limit = max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        window = min(_READ_CHUNK_BYTES, limit + 1 - total)
        chunk = await upload.read(window)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _too_large_error()
        chunks.append(chunk)
    if total == 0:
        raise DraftUploadError("Üleslaaditud fail on tühi.")
    return b"".join(chunks)


def _validate_content_matches_type(contents: bytes, filename: str) -> None:
    """Sniff the true file type from magic bytes (#858).

    The extension/content-type pair is attacker-controlled metadata; the
    leading bytes are what Tika will actually parse. A renamed
    executable (or anything else) is rejected here with an Estonian
    message before it is encrypted, persisted, or shipped to Tika.
    ``_validate_filename`` has already guaranteed the extension is one
    of :data:`_ALLOWED_EXTENSIONS`, so the branches are exhaustive.
    """
    lower = filename.lower()
    if lower.endswith(".docx"):
        if not contents.startswith(_DOCX_MAGIC):
            raise DraftUploadError(MSG_CONTENT_MISMATCH_DOCX)
        _validate_docx_zip(contents)
    elif lower.endswith(".pdf"):
        if _PDF_MAGIC not in contents[:_PDF_MAGIC_SCAN_BYTES]:
            raise DraftUploadError(MSG_CONTENT_MISMATCH_PDF)


def _validate_docx_zip(contents: bytes) -> None:
    """Validate the .docx ZIP central directory + zip-bomb caps (#858).

    Opening with stdlib :mod:`zipfile` parses the central directory only
    (no decompression), which both proves the archive is structurally
    sound and yields each entry's *claimed* uncompressed size. The
    claims are exactly what a classic zip bomb inflates, so capping the
    claimed total (absolute + expansion-ratio, see the constants block)
    stops the bomb before Tika ever tries to expand it. A bomb that
    under-declares its sizes slips past this check but is then caught
    by the Tika response byte ceiling (``TIKA_MAX_TEXT_BYTES``).
    """
    try:
        # NOTE: deliberately *no* ``testzip()`` — that decompresses every
        # entry and would hand the bomb exactly the CPU/RAM it wants.
        # Central-directory parsing is metadata-only.
        with zipfile.ZipFile(io.BytesIO(contents)) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError) as exc:
        raise DraftUploadError(MSG_DOCX_CORRUPT) from exc

    total_uncompressed = sum(max(0, info.file_size) for info in infos)
    if total_uncompressed > _DOCX_UNCOMPRESSED_MULTIPLIER * max_upload_bytes():
        raise DraftUploadError(MSG_DOCX_BOMB)
    if (
        total_uncompressed > _RATIO_CHECK_FLOOR_BYTES
        and total_uncompressed > _MAX_DOCX_COMPRESSION_RATIO * max(1, len(contents))
    ):
        raise DraftUploadError(MSG_DOCX_BOMB)


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
    parent_draft_id: Any = None,
    job_queue: JobQueue | None = None,
    conn_factory: Any = None,
) -> Draft:
    """Validate, encrypt, persist, and enqueue a new draft upload.

    Args:
        user: Authenticated caller. Must carry a non-empty ``org_id``.
        title: Draft title supplied by the uploader (1-200 chars).
            Ignored when ``parent_draft_id`` is set — a new version
            inherits the parent's title because the legislative text
            is what changes between readings, not the draft's
            identity.
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
        parent_draft_id: Optional foreign-key link to an existing
            ``drafts`` row.  When supplied, the handler creates a NEW
            ``draft_versions`` row tied to the parent (#618 PR-B
            "version" branch) instead of a fresh ``drafts`` row.  The
            parent must exist, belong to the caller's org, and have
            ``status='ready'`` (cross-org / mid-pipeline parents are
            rejected with a Estonian :class:`DraftUploadError`).  The
            uploader inherits the parent's ``owner_id`` / ``org_id``
            so a colleague uploading a v2 cannot orphan the version
            from the original drafter's audit trail.
        job_queue: Optional ``JobQueue`` to enqueue the parse job.
            Defaults to a fresh :class:`app.jobs.JobQueue` instance —
            tests pass a stub to avoid hitting Postgres.
        conn_factory: Optional callable returning a context-managed DB
            connection. Defaults to :func:`app.db.get_connection`; tests
            patch this to inject a mock.

    Returns:
        The freshly-inserted :class:`Draft`.  When the version branch
        runs, the returned dataclass reflects the PARENT draft id +
        the new version's ``storage_path`` / ``graph_uri`` / ``status``
        (because :func:`app.docs.draft_model.get_draft` JOINs through
        ``draft_versions`` for those fields post-PR-B).

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

    # Title validation only matters for the new-draft branch — versions
    # inherit the parent's title.  We still validate filename + bytes
    # for both branches.
    if parent_draft_id is None:
        cleaned_title = _validate_title(title)
    else:
        cleaned_title = title.strip() if title else ""
    filename = _validate_filename(upload.filename)
    content_type = _validate_content_type(upload.content_type)

    # #858 resource bounds: reject on the declared size BEFORE reading,
    # then read incrementally with a hard byte cap, then sniff the true
    # content type (magic bytes + .docx zip-bomb caps) before anything
    # is encrypted or persisted.
    _reject_oversize_declared(upload.size)
    contents = await _read_bounded(upload)
    _validate_content_matches_type(contents, filename)
    file_size = len(contents)

    # Step 3: encrypt and persist. Store owner-scoped so the storage
    # path itself already includes the acting user's id.
    stored = store_file(contents, filename=filename, owner_id=str(user_id))

    # Steps 4-6 live inside a single transaction so we either commit the
    # draft row *and* enqueue the job, or we bail and clean up the file.
    #
    # The v2+ branch additionally retries the whole transaction on a
    # ``UNIQUE(draft_id, version_number)`` violation (#745): the advisory
    # lock in ``get_next_version_number`` already serialises allocators,
    # but if one still slips through we re-read ``MAX`` and try again
    # rather than surface a raw 500.  The new-draft branch never retries.
    factory = conn_factory or _connect
    queue = job_queue or JobQueue()
    max_attempts = _MAX_VERSION_ALLOC_ATTEMPTS if parent_draft_id is not None else 1
    draft: Draft | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with factory() as conn:
                if parent_draft_id is not None:
                    draft = _create_new_version(
                        conn,
                        parent_draft_id=parent_draft_id,
                        user=user,
                        file_size=file_size,
                        filename=filename,
                        content_type=content_type,
                        storage_path=stored.storage_path,
                    )
                else:
                    draft = _create_new_draft(
                        conn,
                        user_id=user_id,
                        org_id=org_id,
                        title=cleaned_title,
                        filename=filename,
                        content_type=content_type,
                        file_size=file_size,
                        storage_path=stored.storage_path,
                        doc_type=doc_type,
                        parent_vtk_id=parent_vtk_id,
                    )
                conn.commit()
            break
        except psycopg.errors.UniqueViolation:
            # Only the v2+ branch knows what a unique violation means here
            # (the ``UNIQUE(draft_id, version_number)`` race) and how to
            # recover from it.  Anything else propagates to the generic
            # handler below — re-raised unchanged after the file cleanup.
            if parent_draft_id is None:
                logger.exception(
                    "Draft insert hit a unique violation — cleaning up path=%s",
                    stored.storage_path,
                )
                delete_file(stored.storage_path)
                raise
            # Two uploads raced on the version number despite the advisory
            # lock.  Roll back is implicit (the ``with`` block aborts the
            # txn); retry if we still have budget.
            if attempt < max_attempts:
                logger.warning(
                    "Draft version-number collision for parent=%s on attempt %d/%d — retrying",
                    parent_draft_id,
                    attempt,
                    max_attempts,
                )
                continue
            logger.warning(
                "Draft version-number collision for parent=%s exhausted retries; "
                "cleaning up file path=%s",
                parent_draft_id,
                stored.storage_path,
            )
            delete_file(stored.storage_path)
            raise DraftUploadError(
                "Uue versiooni loomine ebaõnnestus samaaegse üleslaadimise tõttu. "
                "Palun proovige uuesti."
            ) from None
        except DraftUploadError:
            # User-facing validation failure — clean up the orphan file
            # before re-raising so the caller can render the error.
            logger.info(
                "Draft upload validation failed; cleaning up file path=%s",
                stored.storage_path,
            )
            delete_file(stored.storage_path)
            raise
        except Exception:
            logger.exception(
                "Draft insert failed after file was stored — cleaning up path=%s",
                stored.storage_path,
            )
            delete_file(stored.storage_path)
            raise

    if draft is None:
        # Unreachable: the loop either assigned ``draft`` and broke, or
        # raised.  Guard so the type checker (and a future refactor) can't
        # fall through with a stale file.
        delete_file(stored.storage_path)
        raise RuntimeError("Draft upload transaction produced no draft row")

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
        "Draft uploaded id=%s user=%s org=%s size=%d filename=%s parent_draft=%s",
        draft.id,
        user_id,
        org_id,
        file_size,
        filename,
        parent_draft_id,
    )
    return draft


# ---------------------------------------------------------------------------
# Branch implementations (new draft vs new version)
# ---------------------------------------------------------------------------


def _create_new_draft(
    conn: Any,
    *,
    user_id: Any,
    org_id: Any,
    title: str,
    filename: str,
    content_type: str,
    file_size: int,
    storage_path: str,
    doc_type: str,
    parent_vtk_id: Any,
) -> Draft:
    """Insert a brand-new ``drafts`` row + its v1 ``draft_versions`` row.

    Both rows land in the same transaction so a partial commit can
    never leave a draft without a backing version.  The post-insert
    ``graph_uri`` patch (which now embeds the freshly-minted draft id)
    is also part of the same transaction.
    """
    draft = create_draft(
        conn,
        user_id=user_id,
        org_id=org_id,
        title=title,
        filename=filename,
        content_type=content_type,
        file_size=file_size,
        storage_path=storage_path,
        # graph_uri must embed the freshly-minted draft id, but we
        # only get the id after the INSERT. Use a stable placeholder
        # based on the storage_path (which is already unique) then
        # patch the real URI immediately afterwards.
        graph_uri=f"{_GRAPH_URI_PREFIX}pending-{storage_path}",
        doc_type=doc_type,  # type: ignore[arg-type]
        parent_vtk_id=parent_vtk_id,
    )
    final_graph_uri = f"{_GRAPH_URI_PREFIX}{draft.id}"
    conn.execute(
        "update drafts set graph_uri = %s where id = %s",
        (final_graph_uri, str(draft.id)),
    )
    draft.graph_uri = final_graph_uri

    # #618 PR-B: explicit v1 row in the SAME transaction.  Migration
    # 030's backfill handled every PRE-PR-A draft; new uploads need
    # an in-code insert because the migration only runs once.
    create_draft_version(
        conn,
        draft_id=draft.id,
        version_number=1,
        reading_stage="vtk",
        storage_path=storage_path,
        graph_uri=final_graph_uri,
        status="uploaded",
        created_by=user_id,
    )
    log_action(
        str(user_id),
        "draft.version.create",
        {
            "draft_id": str(draft.id),
            "version_number": 1,
            "reading_stage": "vtk",
        },
    )
    return draft


def _create_new_version(
    conn: Any,
    *,
    parent_draft_id: Any,
    user: UserDict,
    file_size: int,
    filename: str,
    content_type: str,
    storage_path: str,
) -> Draft:
    """Insert a NEW ``draft_versions`` row tied to *parent_draft_id*.

    The parent must exist, belong to the caller's org, and be in the
    terminal ``ready`` status.  Any other state surfaces as a
    user-facing :class:`DraftUploadError` so the route handler can
    re-render the form with a banner.

    The new version inherits the parent's ``owner_id`` (NOT the
    uploader's id, because the audit trail of ownership stays with
    the original drafter) and is allocated the next free
    ``version_number``.  Reading stage steps one notch forward in the
    legislative pipeline.

    Returns the parent :class:`Draft` (re-fetched through
    :func:`get_draft` so the JOIN surfaces the new version's
    ``storage_path`` / ``graph_uri`` / ``status`` to the caller).
    """
    if not isinstance(parent_draft_id, uuid.UUID):
        try:
            parent_draft_id = uuid.UUID(str(parent_draft_id))
        except (TypeError, ValueError) as exc:
            raise DraftUploadError("Vanem-eelnõu ei ole kättesaadav.") from exc

    parent = get_draft(conn, parent_draft_id)
    if parent is None:
        # 404-equivalent: the parent does not exist.  We do NOT
        # disclose existence vs cross-org separately so the same
        # message covers both branches.
        raise DraftUploadError("Vanem-eelnõu ei ole kättesaadav.")

    user_org_id = user.get("org_id")
    if user_org_id is None or str(parent.org_id) != str(user_org_id):
        # Cross-org parent — same indistinguishable message as above so
        # we never confirm the existence of another org's draft.
        raise DraftUploadError("Vanem-eelnõu ei ole kättesaadav.")

    # The parent's status is read through the version-aware JOIN so a
    # parent whose latest version is mid-pipeline is correctly rejected.
    if parent.status != "ready":
        raise DraftUploadError("Uue versiooni saab luua ainult eelnõust, mille analüüs on valmis.")

    # Allocate the next version slot.  The SELECT runs against the open
    # connection so two concurrent uploads against the same parent will
    # serialise on the row-level lock the subsequent INSERT takes.
    next_version = get_next_version_number(conn, parent_draft_id)
    latest = get_latest_version(conn, parent_draft_id)
    base_stage = latest.reading_stage if latest is not None else "vtk"
    next_stage = next_reading_stage(base_stage)

    # §9.5 per-version graph URI scheme.
    graph_uri = f"{_GRAPH_URI_PREFIX}{parent_draft_id}/v{next_version}"

    create_draft_version(
        conn,
        draft_id=parent_draft_id,
        version_number=next_version,
        reading_stage=next_stage,
        storage_path=storage_path,
        graph_uri=graph_uri,
        status="uploaded",
        # Inherit ownership from the parent draft so the audit trail
        # stays attached to the original drafter.  The acting user is
        # captured in the audit log_action call below.
        created_by=parent.user_id,
    )

    # Touch ``drafts.updated_at`` + flip the legacy status mirror back
    # to ``uploaded`` so the listing UI reflects the in-flight pipeline
    # for the new version.  Also bump file metadata so the latest
    # filename / size matches the new bytes.
    conn.execute(
        """
        update drafts
        set status = %s,
            filename = %s,
            content_type = %s,
            file_size = %s,
            storage_path = %s,
            graph_uri = %s,
            updated_at = now(),
            error_message = null,
            error_debug = null,
            processing_completed_at = null
        where id = %s
        """,
        (
            "uploaded",
            filename,
            content_type,
            file_size,
            storage_path,
            graph_uri,
            str(parent_draft_id),
        ),
    )

    log_action(
        str(user["id"]),
        "draft.version.create",
        {
            "draft_id": str(parent_draft_id),
            "version_number": next_version,
            "reading_stage": next_stage,
            "uploader_id": str(user["id"]),
        },
    )

    # Re-fetch the parent so the returned Draft reflects the new
    # version's status / graph_uri / storage_path via the JOIN.
    refreshed = get_draft(conn, parent_draft_id)
    if refreshed is None:
        # Should be impossible given we just wrote both rows, but
        # defend against it so the caller doesn't get a None back.
        raise RuntimeError(f"Failed to re-fetch draft {parent_draft_id} after version insert")
    return refreshed
