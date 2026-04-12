"""Tests for app/version.py and the /api/health version payload."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from app import version as version_module


@pytest.fixture(autouse=True)
def _clear_version_cache():
    """Every test starts with a clean cache so ordering doesn't matter."""
    version_module._reset_cache_for_tests()
    yield
    version_module._reset_cache_for_tests()


class TestReadVersion:
    def test_reads_from_version_json(self, tmp_path: Path):
        path = tmp_path / "VERSION.json"
        path.write_text(
            json.dumps(
                {"app": "1.2.3", "sha": "abcdef1234567890", "built_at": "2026-04-12T10:00:00Z"}
            ),
            encoding="utf-8",
        )
        with patch.object(version_module, "_VERSION_PATH", path):
            version_module._reset_cache_for_tests()
            v = version_module.read_version()

        assert set(v.keys()) == {"app", "sha", "built_at"}
        assert v["app"] == "1.2.3"
        assert v["sha"] == "abcdef1234567890"
        assert v["built_at"] == "2026-04-12T10:00:00Z"

    def test_falls_back_when_file_missing(self, tmp_path: Path):
        missing = tmp_path / "does-not-exist.json"
        with patch.object(version_module, "_VERSION_PATH", missing):
            version_module._reset_cache_for_tests()
            v = version_module.read_version()

        assert set(v.keys()) == {"app", "sha", "built_at"}
        assert v["app"] == "0.1.0"
        assert v["built_at"] == "unknown"
        # Either a real dev-<sha> (running inside the git checkout) or the
        # "dev" sentinel when git isn't available.
        assert v["sha"] == "dev" or v["sha"].startswith("dev-")

    def test_fallback_uses_git_when_file_missing(self, tmp_path: Path):
        missing = tmp_path / "no.json"
        with (
            patch.object(version_module, "_VERSION_PATH", missing),
            patch.object(
                version_module.subprocess,
                "check_output",
                return_value="1234567890abcdef1234567890abcdef12345678\n",
            ),
        ):
            version_module._reset_cache_for_tests()
            v = version_module.read_version()

        assert v["sha"] == "dev-1234567"

    def test_fallback_returns_dev_when_git_fails(self, tmp_path: Path):
        missing = tmp_path / "no.json"
        with (
            patch.object(version_module, "_VERSION_PATH", missing),
            patch.object(
                version_module.subprocess,
                "check_output",
                side_effect=FileNotFoundError,
            ),
        ):
            version_module._reset_cache_for_tests()
            v = version_module.read_version()

        assert v["sha"] == "dev"

    def test_malformed_json_falls_back(self, tmp_path: Path):
        path = tmp_path / "VERSION.json"
        path.write_text("not json", encoding="utf-8")
        with patch.object(version_module, "_VERSION_PATH", path):
            version_module._reset_cache_for_tests()
            v = version_module.read_version()

        assert v["app"] == "0.1.0"
        assert v["built_at"] == "unknown"

    def test_result_is_cached(self, tmp_path: Path):
        path = tmp_path / "VERSION.json"
        path.write_text(
            json.dumps({"app": "9.9.9", "sha": "cafef00d", "built_at": "x"}),
            encoding="utf-8",
        )
        with patch.object(version_module, "_VERSION_PATH", path):
            version_module._reset_cache_for_tests()
            first = version_module.read_version()
            # Mutate the file; cached result should not change.
            path.write_text(
                json.dumps({"app": "0.0.0", "sha": "deadbeef", "built_at": "y"}),
                encoding="utf-8",
            )
            second = version_module.read_version()

        assert first == second
        assert second["app"] == "9.9.9"

    def test_returns_a_copy(self, tmp_path: Path):
        path = tmp_path / "VERSION.json"
        path.write_text(
            json.dumps({"app": "1.0.0", "sha": "abc1234", "built_at": "t"}),
            encoding="utf-8",
        )
        with patch.object(version_module, "_VERSION_PATH", path):
            version_module._reset_cache_for_tests()
            v1 = version_module.read_version()
            v1["app"] = "mutated"
            v2 = version_module.read_version()

        assert v2["app"] == "1.0.0"


class TestHealthEndpointIncludesVersion:
    def test_health_payload_has_version_block(self):
        # Using importlib avoids shadowing the ``app`` package name with
        # the ASGI ``app`` object. We import the main module first (which
        # registers admin routes and transitively imports the shim), then
        # pull out the ASGI callable by attribute access.
        import importlib

        main_mod = importlib.import_module("app.main")
        importlib.import_module("app.templates.admin_dashboard")
        asgi_app = main_mod.app

        with (
            patch("app.templates.admin_dashboard.jena_check_health", return_value=True),
            patch("app.templates.admin_dashboard._check_postgres", return_value=True),
        ):
            client = TestClient(asgi_app)
            response = client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        version = data["version"]
        assert set(version.keys()) == {"app", "sha", "built_at"}
        assert isinstance(version["app"], str)
        assert isinstance(version["sha"], str)
        assert isinstance(version["built_at"], str)
