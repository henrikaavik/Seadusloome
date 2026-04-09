"""Unit tests for ``app.storage.encrypted``.

These tests exercise the encrypted-file API without touching real
filesystem paths outside of pytest's ``tmp_path`` fixture. Module-level
key/directory state is controlled by reloading the module inside each
test with ``monkeypatch.setenv`` + ``importlib.reload``.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.fernet import Fernet


def _reload_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """Reload ``app.storage.encrypted`` with a per-test key + storage dir."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

    import app.storage.encrypted as encrypted

    return importlib.reload(encrypted)


class TestRoundTrip:
    def test_round_trip(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Encrypting then decrypting yields the original bytes."""
        encrypted = _reload_storage(monkeypatch, tmp_path)

        payload = b"Eelnou sisu on konfidentsiaalne."
        stored = encrypted.store_file(payload, "eelnou.docx", owner_id="user-123")

        assert stored.size_bytes == len(payload)
        assert stored.filename == "eelnou.docx"
        assert Path(stored.storage_path).exists()

        # Bytes on disk are NOT the plaintext.
        raw_on_disk = Path(stored.storage_path).read_bytes()
        assert raw_on_disk != payload

        # Round-trip decrypt.
        decrypted = encrypted.read_file(stored.storage_path)
        assert decrypted == payload

    def test_storage_path_is_owner_scoped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Generated path embeds the owner_id under a 2-char shard."""
        encrypted = _reload_storage(monkeypatch, tmp_path)

        owner = "abcd-1234"
        stored = encrypted.store_file(b"data", "x.docx", owner_id=owner)

        # Path looks like <tmp_path>/ab/abcd-1234/<uuid>.enc
        path = Path(stored.storage_path)
        assert owner in path.parts, f"owner_id not in path: {stored.storage_path}"
        assert path.suffix == ".enc"
        # The shard directory is the first 2 chars of the owner id.
        parents = [p.name for p in path.parents]
        assert owner[:2] in parents


class TestDecryption:
    def test_different_keys_cannot_decrypt(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """A file encrypted with key A cannot be decrypted with key B."""
        # Write with key A.
        encrypted_a = _reload_storage(monkeypatch, tmp_path)
        stored = encrypted_a.store_file(b"secret payload", "a.txt", owner_id="user-a")
        disk_path = stored.storage_path

        # Swap key and reload module.
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        import app.storage.encrypted as encrypted_module

        encrypted_b = importlib.reload(encrypted_module)

        with pytest.raises(encrypted_b.DecryptionError):
            encrypted_b.read_file(disk_path)

    def test_read_file_raises_filenotfound(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Reading a missing path raises FileNotFoundError, not DecryptionError."""
        encrypted = _reload_storage(monkeypatch, tmp_path)
        with pytest.raises(FileNotFoundError):
            encrypted.read_file(str(tmp_path / "does-not-exist.enc"))


class TestDelete:
    def test_delete_idempotent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Deleting a missing path is a silent no-op (draft-delete safety)."""
        encrypted = _reload_storage(monkeypatch, tmp_path)
        # Should not raise.
        encrypted.delete_file(str(tmp_path / "never-existed.enc"))

    def test_delete_removes_existing_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        encrypted = _reload_storage(monkeypatch, tmp_path)
        stored = encrypted.store_file(b"data", "x.docx", owner_id="user-x")
        assert Path(stored.storage_path).exists()

        encrypted.delete_file(stored.storage_path)
        assert not Path(stored.storage_path).exists()


class TestProdEnforcement:
    def test_prod_without_key_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Missing STORAGE_ENCRYPTION_KEY off-dev must fail module import."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

        import app.storage.encrypted as encrypted_module

        with pytest.raises(RuntimeError, match="STORAGE_ENCRYPTION_KEY"):
            importlib.reload(encrypted_module)

        # Restore a good key so the next test that imports the module
        # (e.g. through test_import_safety) doesn't inherit the bad state.
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        importlib.reload(encrypted_module)

    def test_dev_generates_ephemeral_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """In dev with no key set, the module imports cleanly with an ephemeral key."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

        import app.storage.encrypted as encrypted_module

        reloaded = importlib.reload(encrypted_module)
        # Round-trip still works with the generated key.
        stored = reloaded.store_file(b"dev payload", "f.txt", owner_id="user-d")
        assert reloaded.read_file(stored.storage_path) == b"dev payload"

        # Restore explicit key for subsequent tests / module users.
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        importlib.reload(encrypted_module)


class TestGenerateKey:
    def test_generate_encryption_key_is_valid_fernet(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """The helper emits keys that Fernet itself will accept."""
        encrypted = _reload_storage(monkeypatch, tmp_path)
        key = encrypted.generate_encryption_key()
        assert isinstance(key, str)

        # Should be usable by Fernet directly.
        fernet = Fernet(key.encode())
        ct = fernet.encrypt(b"hello")
        assert fernet.decrypt(ct) == b"hello"
