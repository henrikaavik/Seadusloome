"""MultiFernet key-rotation tests for ``app.storage.encrypted`` (#857).

``STORAGE_ENCRYPTION_KEY`` accepts a comma-separated key list: the FIRST
key encrypts every new write, ALL keys are tried on decrypt. These tests
pin the full rotation contract from the module's runbook:

- single-key values (the historical format) keep working unchanged;
- ciphertexts written under the OLD key still decrypt after the new key
  is prepended;
- NEW writes are encrypted under the FIRST key only;
- ``MultiFernet.rotate`` re-encrypts an old token under the first key
  (runbook step 4);
- malformed / empty configurations fail loudly without echoing key
  material;
- the unset-key ephemeral dev path (``is_stub_allowed`` interplay,
  #449/#865) is unchanged.

Same reload pattern as ``tests/test_storage_encrypted.py``: module-level
singleton state is reset by reloading the module per test.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.fernet import Fernet, InvalidToken


def _reload_with_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key_value: str | None
) -> ModuleType:
    """Reload ``app.storage.encrypted`` with an explicit key configuration."""
    monkeypatch.setenv("APP_ENV", "development")
    if key_value is None:
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
    else:
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", key_value)
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

    import app.storage.encrypted as encrypted

    return importlib.reload(encrypted)


@pytest.fixture(autouse=True)
def _restore_module(monkeypatch: pytest.MonkeyPatch):
    """Leave the module in a clean single-key state for downstream tests."""
    yield
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.storage.encrypted as encrypted

    importlib.reload(encrypted)


class TestSingleKeyBackCompat:
    def test_single_key_round_trip(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """The historical single-key format still round-trips files and text."""
        key = Fernet.generate_key().decode()
        encrypted = _reload_with_key(monkeypatch, tmp_path, key)

        stored = encrypted.store_file(b"sisu", "f.docx", owner_id="u1")
        assert encrypted.read_file(stored.storage_path) == b"sisu"
        assert encrypted.decrypt_text(encrypted.encrypt_text("tekst")) == "tekst"

    def test_single_key_ciphertext_survives_reload_with_same_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        key = Fernet.generate_key().decode()
        encrypted = _reload_with_key(monkeypatch, tmp_path, key)
        stored = encrypted.store_file(b"payload", "f.docx", owner_id="u1")

        encrypted = _reload_with_key(monkeypatch, tmp_path, key)
        assert encrypted.read_file(stored.storage_path) == b"payload"


class TestRotation:
    def test_old_key_file_decrypts_after_rotation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Runbook steps 2-3: prepend the new key; old files stay readable."""
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()

        encrypted = _reload_with_key(monkeypatch, tmp_path, old_key)
        stored = encrypted.store_file(b"vana sisu", "f.docx", owner_id="u1")

        encrypted = _reload_with_key(monkeypatch, tmp_path, f"{new_key},{old_key}")
        assert encrypted.read_file(stored.storage_path) == b"vana sisu"

    def test_old_key_text_decrypts_after_rotation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """``parsed_text_encrypted``-style BYTEA columns survive rotation too."""
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()

        encrypted = _reload_with_key(monkeypatch, tmp_path, old_key)
        ciphertext = encrypted.encrypt_text("§ 1. Tundlik tekst.")

        encrypted = _reload_with_key(monkeypatch, tmp_path, f"{new_key},{old_key}")
        assert encrypted.decrypt_text(ciphertext) == "§ 1. Tundlik tekst."

    def test_new_writes_use_first_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """After rotation, NEW ciphertexts are written under the FIRST key only."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()

        encrypted = _reload_with_key(
            monkeypatch, tmp_path, f"{new_key.decode()},{old_key.decode()}"
        )
        stored = encrypted.store_file(b"uus sisu", "f.docx", owner_id="u1")
        raw_on_disk = Path(stored.storage_path).read_bytes()

        # Decryptable with the new key directly…
        assert Fernet(new_key).decrypt(raw_on_disk) == b"uus sisu"
        # …and NOT with the old key.
        with pytest.raises(InvalidToken):
            Fernet(old_key).decrypt(raw_on_disk)

    def test_rotate_reencrypts_under_first_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Runbook step 4: ``MultiFernet.rotate`` moves a token to the new key."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_token = Fernet(old_key).encrypt(b"roteeritav sisu")

        encrypted = _reload_with_key(
            monkeypatch, tmp_path, f"{new_key.decode()},{old_key.decode()}"
        )
        rotated = encrypted._get_fernet().rotate(old_token)

        assert Fernet(new_key).decrypt(rotated) == b"roteeritav sisu"
        with pytest.raises(InvalidToken):
            Fernet(old_key).decrypt(rotated)

    def test_unrelated_keys_still_fail(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Dropping the writing key from the list bricks its ciphertexts."""
        encrypted = _reload_with_key(monkeypatch, tmp_path, Fernet.generate_key().decode())
        stored = encrypted.store_file(b"secret", "f.docx", owner_id="u1")

        two_other_keys = f"{Fernet.generate_key().decode()},{Fernet.generate_key().decode()}"
        encrypted = _reload_with_key(monkeypatch, tmp_path, two_other_keys)
        with pytest.raises(encrypted.DecryptionError):
            encrypted.read_file(stored.storage_path)


class TestKeyParsing:
    def test_whitespace_and_trailing_comma_tolerated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()

        encrypted = _reload_with_key(monkeypatch, tmp_path, old_key)
        stored = encrypted.store_file(b"data", "f.docx", owner_id="u1")

        encrypted = _reload_with_key(monkeypatch, tmp_path, f" {new_key} , {old_key} ,")
        assert encrypted.read_file(stored.storage_path) == b"data"

    def test_malformed_segment_raises_without_leaking_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        good = Fernet.generate_key().decode()
        encrypted = _reload_with_key(monkeypatch, tmp_path, f"{good},definitely-not-a-key")

        with pytest.raises(RuntimeError, match="entry #2 of 2") as excinfo:
            encrypted.store_file(b"x", "f.docx", owner_id="u1")
        # Neither the good key nor the malformed segment may be echoed.
        assert good not in str(excinfo.value)
        assert "definitely-not-a-key" not in str(excinfo.value)

    def test_value_with_only_commas_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        encrypted = _reload_with_key(monkeypatch, tmp_path, " , ,")
        with pytest.raises(RuntimeError, match="contains no keys"):
            encrypted.store_file(b"x", "f.docx", owner_id="u1")

    def test_malformed_key_raises_even_in_stub_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """An explicitly-set-but-broken key must NOT fall back to ephemeral.

        A silent ephemeral fallback in dev would write files the operator
        believes are covered by their configured key — unreadable after
        the next restart.
        """
        encrypted = _reload_with_key(monkeypatch, tmp_path, "bad-key")
        with pytest.raises(RuntimeError, match="not a valid Fernet key"):
            encrypted.encrypt_text("x")


class TestEphemeralDevPath:
    def test_unset_key_still_uses_ephemeral_in_dev(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """#865 regression: the unset-key dev path is untouched by rotation."""
        encrypted = _reload_with_key(monkeypatch, tmp_path, None)
        stored = encrypted.store_file(b"dev data", "f.docx", owner_id="u1")
        assert encrypted.read_file(stored.storage_path) == b"dev data"

    def test_unset_key_in_prod_still_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        encrypted = _reload_with_key(monkeypatch, tmp_path, None)
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(RuntimeError, match="APP_ENV=production"):
            encrypted.store_file(b"x", "f.docx", owner_id="u1")
