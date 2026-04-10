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
    def test_prod_without_key_imports_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Module import must NEVER raise.

        Regression test for the rollback incident where prod container
        crashed on ``import app.main`` because ``STORAGE_ENCRYPTION_KEY``
        was unset in Coolify. A missing key should be diagnosed lazily
        on first ``store_file`` / ``read_file`` call, not at import
        time — otherwise every deploy before the env var is added
        dies before ``/api/ping`` comes up and Coolify rolls back.

        #449: this test now uses ``APP_ENV=production`` explicitly
        because the unified stub gate only enforces real keys when
        ``APP_ENV=production``. Dev / test / staging all fall through
        to the ephemeral key path.
        """
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

        import app.storage.encrypted as encrypted_module

        # Must NOT raise.
        reloaded = importlib.reload(encrypted_module)
        assert reloaded.STORAGE_DIR is not None

        # First call to a storage function that needs a key should raise.
        with pytest.raises(RuntimeError, match="APP_ENV=production"):
            reloaded.store_file(b"x", "x.txt", owner_id="user")

        # Subsequent calls still raise (not a one-shot) — store_file goes
        # through _get_fernet() before touching the filesystem, so the
        # key error wins over any path-level validation.
        with pytest.raises(RuntimeError, match="APP_ENV=production"):
            reloaded.store_file(b"y", "y.txt", owner_id="other")

        # Restore a good key so downstream tests are not affected.
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        importlib.reload(encrypted_module)

    def test_staging_without_key_uses_ephemeral_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """#449: APP_ENV=staging is now stub-mode-eligible.

        The previous code only allowed development; the unified gate
        treats anything other than production as stub-allowed so a
        staging deploy without STORAGE_ENCRYPTION_KEY no longer
        crashes on the first upload — it just uses an ephemeral key
        and logs a warning.
        """
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

        import app.storage.encrypted as encrypted_module

        reloaded = importlib.reload(encrypted_module)
        # Round-trip should still work with the ephemeral key.
        stored = reloaded.store_file(b"staging payload", "f.txt", owner_id="user-s")
        assert reloaded.read_file(stored.storage_path) == b"staging payload"

        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        importlib.reload(encrypted_module)

    def test_prod_with_key_works(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """With a valid key set, prod mode stores and reads normally."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

        import app.storage.encrypted as encrypted_module

        reloaded = importlib.reload(encrypted_module)
        stored = reloaded.store_file(b"payload", "f.txt", owner_id="user-prod")
        assert reloaded.read_file(stored.storage_path) == b"payload"

        monkeypatch.setenv("APP_ENV", "development")
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


class TestEncryptDecryptText:
    def test_encrypt_decrypt_text_round_trip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """encrypt_text → decrypt_text returns the original string."""
        encrypted = _reload_storage(monkeypatch, tmp_path)

        plaintext = "§ 1. Käesolev seadus reguleerib."
        ciphertext = encrypted.encrypt_text(plaintext)

        # Ciphertext must be bytes, not a plain string.
        assert isinstance(ciphertext, bytes)
        # Must not be the raw UTF-8 encoding (i.e. it is actually encrypted).
        assert ciphertext != plaintext.encode("utf-8")

        # Round-trip must recover the original string exactly.
        recovered = encrypted.decrypt_text(ciphertext)
        assert recovered == plaintext

    def test_decrypt_text_wrong_key_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Decrypting with a different key must raise DecryptionError."""
        # Encrypt with key A.
        encrypted_a = _reload_storage(monkeypatch, tmp_path)
        ciphertext = encrypted_a.encrypt_text("tundlik eelnõu sisu")

        # Swap to key B and reload.
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        import app.storage.encrypted as encrypted_module

        encrypted_b = importlib.reload(encrypted_module)

        with pytest.raises(encrypted_b.DecryptionError, match="invalid token"):
            encrypted_b.decrypt_text(ciphertext)
