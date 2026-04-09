"""Encrypted file storage for pre-publication draft legislation.

Drafts are politically sensitive prior to publication (see CLAUDE.md §
"Draft sensitivity"). All file contents are encrypted with Fernet
(authenticated encryption, AES-128-CBC + HMAC-SHA256) before being
written to disk, and decrypted only when the owner reads them back.

Public exports:
    - ``StoredFile``           dataclass describing a stored artifact
    - ``DecryptionError``      raised when a ciphertext cannot be decrypted
    - ``store_file``           encrypt + write
    - ``read_file``            read + decrypt
    - ``delete_file``          idempotent delete
    - ``generate_encryption_key`` helper for dev/migration setup
"""

from app.storage.encrypted import (
    DecryptionError,
    StoredFile,
    delete_file,
    generate_encryption_key,
    read_file,
    store_file,
)

__all__ = [
    "DecryptionError",
    "StoredFile",
    "delete_file",
    "generate_encryption_key",
    "read_file",
    "store_file",
]
