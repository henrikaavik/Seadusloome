"""Fail-closed stub gating tests (#847, review IDs C-a / C-b).

Covers, in one place:

- ``app.config.get_app_env`` / ``is_stub_allowed`` normalization and the
  explicit allowlist (development / test / ci / staging). Unknown
  non-empty ``APP_ENV`` values fail closed; missing/empty values default
  to ``development`` so local dev and the test suite keep working.
- ``get_worker_mode`` sharing the same normalization helper.
- ``ClaudeProvider`` / ``VoyageProvider`` refusing to instantiate
  without API keys when stubs are disallowed (C-b), including the
  module singletons and the RAG ingestion path that would otherwise
  persist random stub embeddings via ``ON CONFLICT DO UPDATE``.
- The consumer matrix from the ticket review: storage encryption
  (ephemeral-key fallback), Tika, email, signed download URLs, and the
  reference-resolver hash secret all gate on ``is_stub_allowed()`` —
  these tests pin their behavior across the env matrix WITHOUT any
  code change in those modules.
- ``scripts/migrate_chat_encryption.py`` requiring
  ``STORAGE_ENCRYPTION_KEY`` unconditionally (ride-along).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.config import STUB_ALLOWED_ENVS, get_app_env, get_worker_mode, is_stub_allowed

# Values that must allow stubs: the allowlist plus normalization
# variants plus the unset/empty default. ``None`` means "delete APP_ENV".
STUB_ALLOWED_VALUES: list[str | None] = [
    None,
    "",
    "   ",
    "development",
    "test",
    "ci",
    "staging",
    "Development",
    "TEST",
    " staging ",
    "CI",
    "  Test  ",
]

# Values that must fail closed: production in any spelling/spacing, and
# every unrecognized value (typos, aliases the allowlist does not name).
STUB_DENIED_VALUES: list[str] = [
    "production",
    "Production",
    "PRODUCTION",
    " production ",
    "production ",
    "prod",
    "Prod",
    "produciton",
    "live",
    "qa",
    "testing",
]


def _set_app_env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", value)


# ---------------------------------------------------------------------------
# C-a: config normalization + allowlist
# ---------------------------------------------------------------------------


class TestGetAppEnv:
    def test_unset_defaults_to_development(self, monkeypatch: pytest.MonkeyPatch):
        """Missing APP_ENV must keep defaulting to development (local dev)."""
        monkeypatch.delenv("APP_ENV", raising=False)
        assert get_app_env() == "development"

    @pytest.mark.parametrize("raw", ["", "   ", "\t"])
    def test_empty_and_whitespace_default_to_development(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ):
        monkeypatch.setenv("APP_ENV", raw)
        assert get_app_env() == "development"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" Production ", "production"),
            ("TEST", "test"),
            ("Staging", "staging"),
            ("ci ", "ci"),
            ("pRoD", "prod"),
        ],
    )
    def test_strip_lower_normalization(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
    ):
        monkeypatch.setenv("APP_ENV", raw)
        assert get_app_env() == expected


class TestIsStubAllowedMatrix:
    @pytest.mark.parametrize("value", STUB_ALLOWED_VALUES)
    def test_allowlisted_envs_permit_stubs(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ):
        _set_app_env(monkeypatch, value)
        assert is_stub_allowed() is True

    @pytest.mark.parametrize("value", STUB_DENIED_VALUES)
    def test_production_and_unknown_envs_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        _set_app_env(monkeypatch, value)
        assert is_stub_allowed() is False

    def test_allowlist_is_the_documented_set(self):
        """The allowlist itself is part of the contract (#847 DoD)."""
        assert STUB_ALLOWED_ENVS == frozenset({"development", "test", "ci", "staging"})

    def test_unknown_env_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """Unknown values log a single diagnosable warning, then stay quiet."""
        # Unique value so other tests' warnings cannot interfere.
        monkeypatch.setenv("APP_ENV", "qa-847-warn-once")
        with caplog.at_level(logging.WARNING, logger="app.config"):
            assert is_stub_allowed() is False
            assert is_stub_allowed() is False
        warnings = [r for r in caplog.records if "qa-847-warn-once" in r.getMessage()]
        assert len(warnings) == 1
        assert "failing closed" in warnings[0].getMessage()

    def test_production_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """production is a recognised env — fail closed without noise."""
        monkeypatch.setenv("APP_ENV", "production")
        with caplog.at_level(logging.WARNING, logger="app.config"):
            assert is_stub_allowed() is False
        assert not [r for r in caplog.records if r.name == "app.config"]


class TestWorkerModeSharedNormalization:
    """get_worker_mode goes through the same _normalized_env helper."""

    def test_empty_value_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WORKER_MODE", "")
        assert get_worker_mode() == "inproc"

    def test_whitespace_and_case_normalized(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WORKER_MODE", "  InProc ")
        assert get_worker_mode() == "inproc"

    def test_unknown_value_still_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WORKER_MODE", " Bogus ")
        with pytest.raises(ValueError, match="WORKER_MODE"):
            get_worker_mode()


# ---------------------------------------------------------------------------
# C-b: ClaudeProvider fail-closed gate
# ---------------------------------------------------------------------------


class TestClaudeProviderFailClosed:
    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from app.llm import _reset_default_provider

        _reset_default_provider()
        yield
        _reset_default_provider()

    @pytest.mark.parametrize("value", ["production", "Production", "production ", "prod", "live"])
    def test_missing_key_raises_when_stubs_disallowed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        from app.llm import ClaudeProvider

        monkeypatch.setenv("APP_ENV", value)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            ClaudeProvider()

    @pytest.mark.parametrize("value", STUB_ALLOWED_VALUES)
    def test_missing_key_stubs_in_allowed_envs(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ):
        from app.llm import ClaudeProvider

        _set_app_env(monkeypatch, value)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()
        assert provider._stubbed is True
        assert provider.complete("test").startswith("[STUB Claude]")

    def test_key_present_in_production_instantiates_real_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from app.llm import ClaudeProvider

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

        provider = ClaudeProvider()
        assert provider._stubbed is False

    def test_error_names_env_and_allowlist(self, monkeypatch: pytest.MonkeyPatch):
        """The fail-closed error must be self-diagnosing for operators."""
        from app.llm import ClaudeProvider

        monkeypatch.setenv("APP_ENV", "produciton")  # typo on purpose
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(RuntimeError) as exc_info:
            ClaudeProvider()
        message = str(exc_info.value)
        assert "produciton" in message
        assert "staging" in message  # allowlist is spelled out

    def test_default_provider_singleton_raises_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from app.llm import get_default_provider

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            get_default_provider()


# ---------------------------------------------------------------------------
# C-b: VoyageProvider fail-closed gate
# ---------------------------------------------------------------------------


class TestVoyageProviderFailClosed:
    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from app.rag.embedding import _reset_default_embedding_provider

        _reset_default_embedding_provider()
        yield
        _reset_default_embedding_provider()

    @pytest.mark.parametrize("value", ["production", "PRODUCTION", "prod", "qa"])
    def test_missing_key_raises_when_stubs_disallowed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        from app.rag.embedding import VoyageProvider

        monkeypatch.setenv("APP_ENV", value)
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
            VoyageProvider()

    @pytest.mark.parametrize("value", STUB_ALLOWED_VALUES)
    def test_missing_key_stubs_in_allowed_envs(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ):
        from app.rag.embedding import VoyageProvider

        _set_app_env(monkeypatch, value)
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        provider = VoyageProvider()
        assert provider._stubbed is True
        vectors = asyncio.run(provider.embed(["Tsiviilseadustik"]))
        assert len(vectors) == 1
        assert len(vectors[0]) == provider.dimensions

    def test_key_present_in_production_instantiates_real_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from app.rag.embedding import VoyageProvider

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("VOYAGE_API_KEY", "voyage-test-key")

        provider = VoyageProvider()
        assert provider._stubbed is False

    def test_default_provider_singleton_raises_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from app.rag.embedding import get_default_embedding_provider

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
            get_default_embedding_provider()

    def test_ingestion_cannot_embed_with_stub_in_production(self, monkeypatch: pytest.MonkeyPatch):
        """The RAG ingestion path (ON CONFLICT DO UPDATE writer) must
        blow up BEFORE producing random vectors in production-like envs.
        """
        from app.rag.chunker import RagChunk
        from scripts.ingest_rag import _embed_chunks

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        chunk = RagChunk(
            content="Tsiviilseadustik § 1",
            metadata={"source_type": "provision", "source_uri": "estleg:TsMS_Par_1"},
            chunk_index=0,
        )
        with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
            asyncio.run(_embed_chunks([chunk]))


# ---------------------------------------------------------------------------
# Consumer matrix (review comment item 1) — NO code changes in these
# modules; the tests pin their behavior under the fail-closed gate.
# ---------------------------------------------------------------------------


class TestStorageEncryptionConsumer:
    """app.storage.encrypted ephemeral-key fallback across the matrix."""

    @pytest.mark.parametrize("value", ["development", "test", "ci", "staging", None])
    def test_ephemeral_key_in_allowed_envs(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ):
        import app.storage.encrypted as encrypted

        _set_app_env(monkeypatch, value)
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)

        key = encrypted._load_encryption_key()
        assert isinstance(key, bytes) and key

    @pytest.mark.parametrize("value", ["production", "prod", "Production ", "live"])
    def test_missing_key_raises_when_stubs_disallowed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        import app.storage.encrypted as encrypted

        monkeypatch.setenv("APP_ENV", value)
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)

        with pytest.raises(RuntimeError, match="STORAGE_ENCRYPTION_KEY"):
            encrypted._load_encryption_key()

    def test_explicit_key_wins_in_any_env(self, monkeypatch: pytest.MonkeyPatch):
        from cryptography.fernet import Fernet

        import app.storage.encrypted as encrypted

        explicit = Fernet.generate_key().decode()
        monkeypatch.setenv("APP_ENV", "prod")  # even an unknown env
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", explicit)

        assert encrypted._load_encryption_key() == explicit.encode()


class TestTikaConsumer:
    """app.docs.tika_client stub mode across the matrix."""

    @pytest.mark.parametrize("value", ["development", "test", "ci", "staging", None])
    def test_stub_mode_in_allowed_envs(self, monkeypatch: pytest.MonkeyPatch, value: str | None):
        from app.docs.tika_client import TikaClient

        _set_app_env(monkeypatch, value)
        monkeypatch.delenv("TIKA_URL", raising=False)

        client = TikaClient()
        assert client._stub_mode is True
        text = client.extract_text(b"fake docx bytes", "application/pdf")
        assert isinstance(text, str) and text

    @pytest.mark.parametrize("value", ["production", "prod", "qa"])
    def test_no_stub_and_call_time_error_when_disallowed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        from app.docs.tika_client import TikaClient

        monkeypatch.setenv("APP_ENV", value)
        monkeypatch.delenv("TIKA_URL", raising=False)

        client = TikaClient()
        assert client._stub_mode is False
        with pytest.raises(RuntimeError, match="TIKA_URL"):
            client.extract_text(b"fake docx bytes", "application/pdf")


class TestEmailConsumer:
    """app.email.service provider selection across the matrix."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from app.email.service import _reset_provider_for_tests

        _reset_provider_for_tests()
        yield
        _reset_provider_for_tests()

    @pytest.mark.parametrize("value", ["development", "test", "ci", "staging", None])
    def test_stub_provider_in_allowed_envs(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ):
        from app.email.service import get_email_provider
        from app.email.stub_provider import StubProvider

        _set_app_env(monkeypatch, value)
        monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)

        assert isinstance(get_email_provider(), StubProvider)

    @pytest.mark.parametrize("value", ["production", "prod", "live"])
    def test_missing_token_raises_when_stubs_disallowed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        from app.email.service import get_email_provider

        monkeypatch.setenv("APP_ENV", value)
        monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)

        with pytest.raises(RuntimeError, match="POSTMARK_API_TOKEN"):
            get_email_provider()


class TestSignedUrlsConsumer:
    """app.docs.signed_urls dev signing-key fallback across the matrix."""

    @pytest.mark.parametrize("value", ["development", "test", "ci", "staging", None])
    def test_dev_sentinel_in_allowed_envs(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ):
        from app.docs import signed_urls

        _set_app_env(monkeypatch, value)
        monkeypatch.delenv("DOWNLOAD_TOKEN_SECRET", raising=False)
        monkeypatch.delenv("SECRET_KEY", raising=False)

        assert signed_urls._load_signing_key() == signed_urls._DEV_SIGNING_KEY

    @pytest.mark.parametrize("value", ["production", "prod", "qa"])
    def test_missing_secret_raises_when_stubs_disallowed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        from app.docs import signed_urls

        monkeypatch.setenv("APP_ENV", value)
        monkeypatch.delenv("DOWNLOAD_TOKEN_SECRET", raising=False)
        monkeypatch.delenv("SECRET_KEY", raising=False)

        with pytest.raises(RuntimeError, match="DOWNLOAD_TOKEN_SECRET"):
            signed_urls._load_signing_key()


class TestReferenceResolverConsumer:
    """app.docs.reference_resolver ref-hash secret across the matrix."""

    @pytest.mark.parametrize("value", ["development", "test", "ci", "staging", None])
    def test_dev_sentinel_in_allowed_envs(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ):
        from app.docs import reference_resolver

        _set_app_env(monkeypatch, value)
        monkeypatch.delenv("RESOLVER_REF_HASH_SECRET", raising=False)

        assert reference_resolver._get_ref_hash_secret() == b"dev-only-resolver-ref-id-secret"

    @pytest.mark.parametrize("value", ["production", "prod", "live"])
    def test_missing_secret_raises_when_stubs_disallowed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        from app.docs import reference_resolver

        monkeypatch.setenv("APP_ENV", value)
        monkeypatch.delenv("RESOLVER_REF_HASH_SECRET", raising=False)

        with pytest.raises(RuntimeError, match="RESOLVER_REF_HASH_SECRET"):
            reference_resolver._get_ref_hash_secret()


# ---------------------------------------------------------------------------
# Ride-along: scripts/migrate_chat_encryption.py key guard
# ---------------------------------------------------------------------------


class TestMigrateChatEncryptionKeyGuard:
    @pytest.mark.parametrize("value", [None, "development", "staging", "production", "prod"])
    def test_missing_key_exits_1_in_every_env(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ):
        """The backfill must refuse to run without an explicit key,
        regardless of APP_ENV — an ephemeral key would permanently
        destroy every backfilled row.
        """
        import scripts.migrate_chat_encryption as mce

        _set_app_env(monkeypatch, value)
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)

        def _no_connect(*args: object, **kwargs: object) -> object:
            raise AssertionError("migrate() must not touch the DB without a key")

        monkeypatch.setattr(mce.psycopg, "connect", _no_connect)

        assert mce.migrate() == 1

    def test_key_present_passes_guard(self, monkeypatch: pytest.MonkeyPatch):
        """With the key set the guard lets the run proceed to the DB."""
        from cryptography.fernet import Fernet

        import scripts.migrate_chat_encryption as mce

        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())

        def _sentinel_connect(*args: object, **kwargs: object) -> object:
            raise RuntimeError("DB-CONNECT-ATTEMPTED")

        monkeypatch.setattr(mce.psycopg, "connect", _sentinel_connect)

        with pytest.raises(RuntimeError, match="DB-CONNECT-ATTEMPTED"):
            mce.migrate()
