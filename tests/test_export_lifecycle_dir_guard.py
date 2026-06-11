"""#845 (C2) — EXPORT_DIR resolution guard tests.

Rendered exports are plaintext derivatives of politically sensitive
drafts, so the export directory must never resolve inside the deployed
source tree in production (where a stray ``git add`` or image rebuild
could pick the files up — exactly how
``storage/exports/drafter-….docx`` ended up tracked in git).

Covers:

* ``_resolve_export_dir`` fails closed for an in-repo directory when
  ``APP_ENV=production`` and allows it in development.
* APP_ENV is normalized (strip + lowercase, missing/empty →
  development) and the guard applies to EVERY value outside the
  development/test/ci/staging allowlist — ``Production``,
  ``" production "``, ``prod`` and unknown values cannot bypass it on
  a casing/whitespace mismatch (#845 review finding 3).
* The production *defaults* already point outside the tree.
* ``get_export_dir`` re-reads the environment and can mkdir.
* ``app.drafter.docx_builder`` delegates to the SAME shared resolver,
  so the guard covers both EXPORT_DIR writers (review note on #845).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.docs.docx_export import (
    _SOURCE_TREE_ROOT,
    _resolve_export_dir,
    get_export_dir,
)


class TestResolveExportDirGuard:
    def test_production_rejects_dir_inside_source_tree(self):
        in_repo = str(_SOURCE_TREE_ROOT / "storage" / "exports")
        with pytest.raises(RuntimeError, match="source tree"):
            _resolve_export_dir(in_repo, "production")

    def test_production_rejects_source_tree_root_itself(self):
        with pytest.raises(RuntimeError, match="refusing to start"):
            _resolve_export_dir(str(_SOURCE_TREE_ROOT), "production")

    def test_production_allows_dir_outside_source_tree(self, tmp_path: Path):
        resolved = _resolve_export_dir(str(tmp_path), "production")
        assert resolved == tmp_path

    def test_production_default_is_outside_source_tree(self):
        """No EXPORT_DIR set + production must fall back to /var, not ./storage."""
        resolved = _resolve_export_dir(None, "production")
        assert resolved == Path("/var/seadusloome/exports")
        assert not resolved.resolve().is_relative_to(_SOURCE_TREE_ROOT)

    def test_development_allows_in_repo_default(self):
        """Dev keeps the ./storage/exports convenience default (gitignored)."""
        resolved = _resolve_export_dir(None, "development")
        assert resolved == Path("./storage/exports").resolve()

    def test_development_allows_explicit_in_repo_dir(self):
        in_repo = str(_SOURCE_TREE_ROOT / "storage" / "exports")
        assert _resolve_export_dir(in_repo, "development") == Path(in_repo)


class TestAppEnvNormalization:
    """#845 review finding 3: the guard must key on a NORMALIZED env.

    ``APP_ENV=Production`` / ``" production "`` / ``prod`` previously
    bypassed the exact-match ``== "production"`` check; unknown values
    must fail closed too (same normalization PR #865 introduces in
    ``app.config`` — duplicated here until that PR lands).
    """

    _IN_REPO = str(_SOURCE_TREE_ROOT / "storage" / "exports")

    @pytest.mark.parametrize(
        "env",
        ["Production", "PRODUCTION", " production ", "prod", "live", "totally-unknown"],
    )
    def test_production_like_and_unknown_values_are_guarded(self, env: str):
        with pytest.raises(RuntimeError, match="production-like"):
            _resolve_export_dir(self._IN_REPO, env)

    @pytest.mark.parametrize(
        "env",
        ["test", "TEST", " ci ", "staging", "Staging", "Development"],
    )
    def test_allowlisted_envs_tolerate_in_repo_dir(self, env: str):
        """test/ci/staging (any casing/whitespace) keep developer + CI
        workspaces working with an in-repo EXPORT_DIR."""
        assert _resolve_export_dir(self._IN_REPO, env) == Path(self._IN_REPO)

    @pytest.mark.parametrize("env", [None, "", "   "])
    def test_missing_or_empty_env_normalizes_to_development(self, env: str | None):
        resolved = _resolve_export_dir(None, env)
        assert resolved == Path("./storage/exports").resolve()
        # ...and the in-repo dev default is permitted (no raise above).

    def test_unknown_env_without_export_dir_uses_prod_default_and_passes(self):
        """An unknown env with NO explicit EXPORT_DIR falls back to the
        /var default, which sits outside the tree → guard passes."""
        resolved = _resolve_export_dir(None, "prod")
        assert resolved == Path("/var/seadusloome/exports")


class TestGetExportDir:
    def test_rereads_environment_per_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        first = tmp_path / "a"
        second = tmp_path / "b"
        monkeypatch.setenv("EXPORT_DIR", str(first))
        assert get_export_dir() == first
        monkeypatch.setenv("EXPORT_DIR", str(second))
        assert get_export_dir() == second

    def test_create_flag_mkdirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        target = tmp_path / "fresh" / "exports"
        monkeypatch.setenv("EXPORT_DIR", str(target))
        assert not target.exists()
        resolved = get_export_dir(create=True)
        assert resolved == target
        assert target.is_dir()

    def test_production_env_var_guard_applies(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("EXPORT_DIR", str(_SOURCE_TREE_ROOT / "storage" / "exports"))
        with pytest.raises(RuntimeError, match="EXPORT_DIR"):
            get_export_dir()


class TestDrafterWriterSharesResolution:
    """Both EXPORT_DIR writers must run through ONE resolution path."""

    def test_docx_builder_delegates_to_shared_helper(self, tmp_path: Path):
        from app.drafter import docx_builder

        with patch("app.drafter.docx_builder.get_export_dir", return_value=tmp_path) as shared:
            assert docx_builder._get_export_dir() == tmp_path
        shared.assert_called_once_with(create=True)

    def test_guard_trips_through_drafter_writer_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The drafter module must not be able to drift back to an
        in-repo default: with a production env + in-repo EXPORT_DIR its
        directory lookup raises via the shared resolver."""
        from app.drafter import docx_builder

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("EXPORT_DIR", str(_SOURCE_TREE_ROOT / "storage" / "exports"))
        with pytest.raises(RuntimeError, match="source tree"):
            docx_builder._get_export_dir()
