"""Fernet-based encrypted file storage.

Every uploaded draft file is encrypted with a single application-wide
Fernet key (``STORAGE_ENCRYPTION_KEY``) before landing on disk. Fernet
uses AES-128-CBC + HMAC-SHA256 (authenticated encryption) and is the
recommended high-level symmetric primitive from ``cryptography``.

Env vars:
    STORAGE_ENCRYPTION_KEY   url-safe base64 Fernet key (32 bytes decoded)
    STORAGE_DIR              root directory for encrypted files
    APP_ENV                  'development' (default) or 'production'

Production enforcement follows the same pattern as ``SECRET_KEY`` in
``app/auth/jwt_provider.py``: a missing key is a hard failure off-dev.
In dev an ephemeral key is generated at import time so local work is
frictionless, with a loud warning so nobody accidentally ships it.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

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
# Key + directory loading (module-level, with prod enforcement)
# ---------------------------------------------------------------------------


def generate_encryption_key() -> str:
    """Return a fresh url-safe base64 Fernet key.

    Useful from ``python -c "from app.storage import generate_encryption_key;
    print(generate_encryption_key())"`` when setting up a new environment.
    """
    return Fernet.generate_key().decode()


def _load_encryption_key() -> bytes:
    """Return the Fernet key bytes, enforcing an explicit value off-dev."""
    value = os.environ.get("STORAGE_ENCRYPTION_KEY")
    if value:
        return value.encode()
    if os.environ.get("APP_ENV", "development") == "development":
        ephemeral = Fernet.generate_key()
        logger.warning(
            "STORAGE_ENCRYPTION_KEY not set — using ephemeral dev key. "
            "Files written with this key will be UNREADABLE after restart. "
            "Set STORAGE_ENCRYPTION_KEY in your environment for persistent dev."
        )
        return ephemeral
    raise RuntimeError("STORAGE_ENCRYPTION_KEY must be set in non-development environments")


def _load_storage_dir() -> Path:
    """Return the root storage directory, with a dev-friendly default."""
    raw = os.environ.get("STORAGE_DIR")
    if raw:
        return Path(raw)
    if os.environ.get("APP_ENV", "development") == "development":
        return Path("./storage/drafts").resolve()
    return Path("/var/seadusloome/drafts")


_KEY = _load_encryption_key()
_FERNET = Fernet(_KEY)
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
    return STORAGE_DIR / shard / owner_id / f"{file_id}.enc"


def store_file(contents: bytes, filename: str, owner_id: str) -> StoredFile:
    """Encrypt *contents* and write them to a new file owned by *owner_id*.

    Args:
        contents: Plaintext bytes to encrypt and persist.
        filename: Original filename (metadata only, not stored on disk).
        owner_id: Stable per-user identifier used to scope the path.

    Returns:
        A ``StoredFile`` pointing at the encrypted artifact.
    """
    target = _build_storage_path(owner_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    ciphertext = _FERNET.encrypt(contents)
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
    """
    path = Path(storage_path)
    if not path.exists():
        raise FileNotFoundError(f"Encrypted file not found: {storage_path}")

    ciphertext = path.read_bytes()
    try:
        return _FERNET.decrypt(ciphertext)
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
