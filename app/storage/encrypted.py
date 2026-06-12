"""Fernet-based encrypted file storage with MultiFernet key rotation.

Every uploaded draft file is encrypted with an application-wide Fernet
key set (``STORAGE_ENCRYPTION_KEY``) before landing on disk. Fernet
uses AES-128-CBC + HMAC-SHA256 (authenticated encryption) and is the
recommended high-level symmetric primitive from ``cryptography``.

``STORAGE_ENCRYPTION_KEY`` accepts a **comma-separated list** of keys
wrapped in :class:`cryptography.fernet.MultiFernet` (#857): the FIRST
key encrypts every new write, ALL keys are tried for decryption. A
single key (the historical format) keeps working unchanged.

Key rotation runbook (#857)
---------------------------
1. Generate a fresh key::

       uv run python -c "from app.storage import generate_encryption_key; \\
           print(generate_encryption_key())"

2. **Prepend** it to the existing value in the Coolify environment for
   ``seadusloome-app`` (new key first — it becomes the encryption key;
   the old key stays so existing ciphertexts remain readable)::

       STORAGE_ENCRYPTION_KEY=<new-key>,<old-key>

3. Redeploy. New uploads / ``encrypt_text`` writes now use ``<new-key>``;
   files and BYTEA columns written under ``<old-key>`` still decrypt.

4. (Optional, to fully retire a leaked key) Re-encrypt at rest: for each
   stored artifact run ``read_file`` + re-write, or use
   ``MultiFernet.rotate(token)`` on the raw ciphertext — ``rotate``
   re-encrypts under the first key without exposing the plaintext.
   The same applies to ``parsed_text_encrypted`` /
   ``draft_content_encrypted`` BYTEA columns via
   ``decrypt_text`` + ``encrypt_text``.

5. Only after every ciphertext has been re-encrypted (or expired) drop
   ``<old-key>`` from the list. **Never remove a key that still has
   live ciphertexts** — those files become permanently unreadable
   (``DecryptionError``).

Env vars:
    STORAGE_ENCRYPTION_KEY   one or more url-safe base64 Fernet keys
                             (32 bytes decoded each), comma-separated;
                             first key encrypts, all keys decrypt
    STORAGE_DIR              root directory for encrypted files
    APP_ENV                  'development' (default) or 'production'

Production enforcement is **deferred to first use** (same pattern as
``app/docs/tika_client.py``): the module imports cleanly even when
``STORAGE_ENCRYPTION_KEY`` is unset, and raises ``RuntimeError`` only
when ``store_file``/``read_file`` is actually called. This matters for
prod deploys — Coolify's container healthcheck probes ``/api/ping``
before the upload path is exercised, and we do not want a missing
env var to block the whole app from starting. The trade-off is that
a misconfigured prod will look healthy until the first upload; the
alternative (crash on import) bricks every deploy until the env var
is set, which is exactly how batches 68e1259 and 66ada63 rolled back.

In dev an ephemeral key is generated on first use so local work is
frictionless, with a loud warning so nobody accidentally ships it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app import config

logger = logging.getLogger(__name__)


class DecryptionError(Exception):
    """Raised when a ciphertext cannot be decrypted (wrong key, tampered)."""


@dataclass(frozen=True)
class StoredFile:
    """Handle returned after a successful ``store_file`` call.

    Attributes:
        storage_path: Absolute path of the encrypted file on disk.
        size_bytes: Length of the **original plaintext** in bytes.
        filename: Original filename supplied by the uploader.
    """

    storage_path: str
    size_bytes: int
    filename: str


# ---------------------------------------------------------------------------
# Lazy Fernet initialisation — NEVER raise at import time. See module
# docstring for the rationale. The _get_fernet() helper is the single
# choke point through which both store_file and read_file go.
# ---------------------------------------------------------------------------


_fernet: MultiFernet | None = None
_warned_dev_ephemeral = False
# #453: protect _fernet singleton init from concurrent worker threads.
_fernet_lock = threading.Lock()


def generate_encryption_key() -> str:
    """Return a fresh url-safe base64 Fernet key.

    Useful from ``python -c "from app.storage import generate_encryption_key;
    print(generate_encryption_key())"`` when setting up a new environment.
    """
    return Fernet.generate_key().decode()


def _load_fernets() -> list[Fernet]:
    """Return the configured Fernet instances, enforcing explicit keys in prod.

    ``STORAGE_ENCRYPTION_KEY`` may hold a single key (historical format)
    or a comma-separated list (#857). Whitespace around segments and a
    trailing comma are tolerated; the segment ORDER is significant —
    the first key is the one new ciphertexts are written with.

    A *set-but-unusable* value (empty after parsing, or any segment
    that is not a valid Fernet key) raises ``RuntimeError`` in every
    environment, including dev/stub mode: an explicitly configured key
    that silently fell back to an ephemeral one would write files
    nobody can ever read back after a restart.

    The unset-key dev/test/staging ephemeral path is gated through
    :func:`app.config.is_stub_allowed` so all three Phase 2 stubs
    (Tika, Claude, Fernet) follow the same APP_ENV rule (#449, #865).
    """
    global _warned_dev_ephemeral
    value = config.env_str("STORAGE_ENCRYPTION_KEY")
    if value:
        segments = [seg.strip() for seg in value.split(",")]
        keys = [seg for seg in segments if seg]
        if not keys:
            raise RuntimeError(
                "STORAGE_ENCRYPTION_KEY is set but contains no keys. "
                "Provide one or more comma-separated url-safe base64 "
                "Fernet keys (first key encrypts, all keys decrypt)."
            )
        fernets: list[Fernet] = []
        for position, key in enumerate(keys, start=1):
            try:
                fernets.append(Fernet(key.encode()))
            except (ValueError, TypeError) as exc:
                # Never echo the key material itself into logs/tracebacks.
                raise RuntimeError(
                    f"STORAGE_ENCRYPTION_KEY entry #{position} of {len(keys)} "
                    "is not a valid Fernet key (expected url-safe base64, "
                    "32 bytes decoded). Generate one with `uv run python -c "
                    '"from app.storage import generate_encryption_key; '
                    'print(generate_encryption_key())"`.'
                ) from exc
        return fernets
    if config.is_stub_allowed():
        if not _warned_dev_ephemeral:
            logger.warning(
                "STORAGE_ENCRYPTION_KEY not set — using ephemeral key. "
                "Files written with this key will be UNREADABLE after restart. "
                "Set STORAGE_ENCRYPTION_KEY in your environment for persistent storage."
            )
            _warned_dev_ephemeral = True
        return [Fernet(Fernet.generate_key())]
    raise RuntimeError(
        "STORAGE_ENCRYPTION_KEY must be set when APP_ENV=production. "
        'Generate one with `uv run python -c "from app.storage import '
        'generate_encryption_key; print(generate_encryption_key())"` and '
        "set it in the Coolify environment variables for seadusloome-app."
    )


def _get_fernet() -> MultiFernet:
    """Lazily construct and cache the module-level MultiFernet instance.

    Double-checked locking (#453) so concurrent worker threads racing
    to encrypt the first upload after process start don't end up
    constructing two instances with two different ephemeral keys
    (which would corrupt the round-trip).

    ``MultiFernet`` encrypts with the FIRST configured key and tries
    every key (in order) on decrypt — the rotation contract of #857.
    A single-key configuration behaves exactly like plain ``Fernet``.
    """
    global _fernet
    if _fernet is None:
        with _fernet_lock:
            if _fernet is None:
                _fernet = MultiFernet(_load_fernets())
    return _fernet


def _load_storage_dir() -> Path:
    """Return the root storage directory, with a dev-friendly default.

    Uses the same APP_ENV rule as :func:`is_stub_allowed` (#449) so
    test/staging/dev all default to the local relative path; only
    APP_ENV=production falls back to the system-wide ``/var`` path.
    """
    raw = config.env_str("STORAGE_DIR")
    if raw:
        return Path(raw)
    if config.is_stub_allowed():
        return Path("./storage/drafts").resolve()
    return Path("/var/seadusloome/drafts")


def _get_storage_dir() -> Path:
    """Re-read STORAGE_DIR on every call so tests can monkeypatch env vars.

    The cost of an env-var lookup per upload is negligible compared to
    the I/O cost of writing the encrypted file itself.
    """
    return _load_storage_dir()


# Module-level export kept for backwards compatibility with existing
# callers and tests that import `STORAGE_DIR` directly. Read at import
# time so ``from app.storage.encrypted import STORAGE_DIR`` works, but
# functions below always go through ``_get_storage_dir()`` so monkey-
# patched env vars are respected at call time.
STORAGE_DIR = _load_storage_dir()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_storage_path(owner_id: str) -> Path:
    """Return a per-owner sharded path for a new encrypted file.

    Layout: ``<STORAGE_DIR>/<owner_first_2>/<owner_id>/<uuid>.enc``

    The two-character shard keeps any single directory from accumulating
    thousands of owner folders and mirrors a common convention for
    content-addressed stores.
    """
    if not owner_id:
        raise ValueError("owner_id must be a non-empty string")
    shard = owner_id[:2]
    file_id = uuid.uuid4().hex
    return _get_storage_dir() / shard / owner_id / f"{file_id}.enc"


def store_file(contents: bytes, filename: str, owner_id: str) -> StoredFile:
    """Encrypt *contents* and write them to a new file owned by *owner_id*.

    Args:
        contents: Plaintext bytes to encrypt and persist.
        filename: Original filename (metadata only, not stored on disk).
        owner_id: Stable per-user identifier used to scope the path.

    Returns:
        A ``StoredFile`` pointing at the encrypted artifact.

    Raises:
        RuntimeError: If ``STORAGE_ENCRYPTION_KEY`` is unset and
            ``APP_ENV`` is not ``'development'``. The error message
            includes generation instructions.
    """
    fernet = _get_fernet()
    target = _build_storage_path(owner_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    ciphertext = fernet.encrypt(contents)
    target.write_bytes(ciphertext)

    logger.info(
        "Stored encrypted file owner=%s path=%s size=%d",
        owner_id,
        target,
        len(contents),
    )
    return StoredFile(
        storage_path=str(target),
        size_bytes=len(contents),
        filename=filename,
    )


def read_file(storage_path: str) -> bytes:
    """Read an encrypted file and return the decrypted plaintext.

    Raises:
        FileNotFoundError: If *storage_path* does not exist.
        DecryptionError: If the ciphertext is corrupt or the key is wrong.
        RuntimeError: If ``STORAGE_ENCRYPTION_KEY`` is unset in prod.
    """
    path = Path(storage_path)
    if not path.exists():
        raise FileNotFoundError(f"Encrypted file not found: {storage_path}")

    fernet = _get_fernet()
    ciphertext = path.read_bytes()
    try:
        return fernet.decrypt(ciphertext)
    except InvalidToken as exc:
        raise DecryptionError(
            f"Failed to decrypt {storage_path}: invalid token or wrong key"
        ) from exc


def delete_file(storage_path: str) -> None:
    """Remove an encrypted file from disk.

    Idempotent: a missing file is a silent no-op so that draft deletion
    stays safe even if the filesystem and the DB fall out of sync.
    """
    path = Path(storage_path)
    try:
        path.unlink()
        logger.info("Deleted encrypted file path=%s", storage_path)
    except FileNotFoundError:
        logger.debug("delete_file: path already absent path=%s", storage_path)


def encrypt_text(plaintext: str) -> bytes:
    """Encrypt a string via Fernet and return ciphertext bytes.

    Used for ``parsed_text_encrypted`` and ``draft_content_encrypted``
    columns where the content is a string (not a file). The caller
    writes the returned bytes to a BYTEA column.

    Raises:
        RuntimeError: If ``STORAGE_ENCRYPTION_KEY`` is unset and
            ``APP_ENV`` is not ``'development'``. Same lazy enforcement
            as ``store_file`` / ``read_file``.
    """
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode("utf-8"))


def decrypt_text(ciphertext: bytes) -> str:
    """Decrypt Fernet ciphertext bytes back to a string.

    Args:
        ciphertext: Raw bytes previously returned by ``encrypt_text``.

    Returns:
        The original UTF-8 string.

    Raises:
        DecryptionError: If the key is wrong or the data is corrupt.
        RuntimeError: If ``STORAGE_ENCRYPTION_KEY`` is unset in prod.
    """
    fernet = _get_fernet()
    try:
        return fernet.decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionError("Failed to decrypt text: invalid token or wrong key") from exc
