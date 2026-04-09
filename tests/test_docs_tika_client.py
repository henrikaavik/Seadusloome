"""Unit tests for :mod:`app.docs.tika_client`.

All HTTP calls are mocked via ``unittest.mock.patch("httpx.put")`` /
``httpx.get`` so these tests never touch a real Tika container. The
pattern mirrors ``tests/test_sparql_client.py``: a ``MagicMock()``
stands in for the response object with ``.raise_for_status``,
``.text``, ``.status_code``, and ``.json`` pre-populated.

Test matrix:

    - Stub mode: TIKA_URL unset + APP_ENV=development → canned text
    - Prod mode: TIKA_URL unset + APP_ENV=production → call raises
    - Happy path for extract_text / extract_metadata
    - Error paths for timeout, HTTP 500, connection error, non-JSON
    - is_healthy: happy, timeout (never raises), no URL
    - Module-level singleton identity
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.docs.tika_client import (
    TikaClient,
    TikaError,
    get_default_tika_client,
    reset_default_tika_client,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(body: str = "hello world", json_body: dict | None = None) -> MagicMock:
    """Build a fake 200 OK ``httpx.Response``."""
    response = MagicMock()
    response.status_code = 200
    response.text = body
    response.raise_for_status = MagicMock()
    if json_body is not None:
        response.json.return_value = json_body
    return response


def _http_error_response(status: int, body: str = "boom") -> MagicMock:
    """Build a fake non-2xx response whose ``raise_for_status`` raises."""
    response = MagicMock()
    response.status_code = status
    response.text = body
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"{status} error",
        request=MagicMock(),
        response=response,
    )
    return response


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Clear the module-level default client around every test."""
    reset_default_tika_client()
    yield
    reset_default_tika_client()


# ---------------------------------------------------------------------------
# Stub mode
# ---------------------------------------------------------------------------


class TestStubMode:
    def test_stub_mode_when_url_unset_and_dev(self, monkeypatch: pytest.MonkeyPatch):
        """TIKA_URL unset + APP_ENV=development → stub text."""
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "development")

        client = TikaClient()
        # No httpx call should be made in stub mode.
        with patch("httpx.put") as mock_put:
            result = client.extract_text(b"hello", "application/pdf")
            mock_put.assert_not_called()

        assert "[STUB Tika]" in result
        assert "5 bytes" in result  # len(b"hello")

    def test_stub_mode_metadata_returns_empty_dict(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "development")

        client = TikaClient()
        with patch("httpx.put") as mock_put:
            result = client.extract_metadata(b"hello", "application/pdf")
            mock_put.assert_not_called()

        assert result == {}

    def test_stub_mode_is_healthy_returns_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "development")

        client = TikaClient()
        assert client.is_healthy() is False

    def test_explicit_url_skips_stub_mode(self, monkeypatch: pytest.MonkeyPatch):
        """Passing ``url=...`` explicitly must override stub mode."""
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "development")

        client = TikaClient(url="http://fake:9998")
        assert client.url == "http://fake:9998"
        # Not stub anymore — real HTTP must be attempted.
        with patch("httpx.put", return_value=_ok_response("extracted")) as mock_put:
            result = client.extract_text(b"hello", "application/pdf")
            mock_put.assert_called_once()
        assert result == "extracted"


# ---------------------------------------------------------------------------
# Production mode
# ---------------------------------------------------------------------------


class TestProductionMode:
    def test_prod_mode_missing_url_raises_at_call_time(self, monkeypatch: pytest.MonkeyPatch):
        """TIKA_URL unset + APP_ENV=production → RuntimeError on first call.

        #449: only an explicit ``APP_ENV=production`` forces a real
        Tika service. Dev / test / staging fall through to the stub
        path so a missing TIKA_URL there doesn't crash uploads.
        """
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "production")

        # Init must NOT raise (otherwise the whole app would crash on boot).
        client = TikaClient()
        assert client.url is None

        # is_healthy must never raise and must return False.
        assert client.is_healthy() is False

        with pytest.raises(RuntimeError, match="APP_ENV=production"):
            client.extract_text(b"hello", "application/pdf")

    def test_prod_mode_missing_url_metadata_also_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "production")

        client = TikaClient()
        with pytest.raises(RuntimeError, match="APP_ENV=production"):
            client.extract_metadata(b"hello", "application/pdf")

    def test_staging_mode_falls_through_to_stub(self, monkeypatch: pytest.MonkeyPatch):
        """#449: APP_ENV=staging is now stub-mode-eligible."""
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "staging")

        client = TikaClient()
        # Stub mode active because the gate matches anything that
        # isn't ``production``.
        assert client._stub_mode is True
        result = client.extract_text(b"hello", "application/pdf")
        assert "[STUB Tika]" in result


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_extract_text_happy_path(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.put", return_value=_ok_response("hello world")) as mock_put:
            result = client.extract_text(b"pdf bytes", "application/pdf")

        assert result == "hello world"
        mock_put.assert_called_once()
        _args, kwargs = mock_put.call_args
        # URL comes first (positional) and headers / content are keyword.
        assert kwargs["content"] == b"pdf bytes"
        assert kwargs["headers"]["Content-Type"] == "application/pdf"
        assert kwargs["headers"]["Accept"] == "text/plain"

    def test_extract_text_trailing_slash_in_url_is_stripped(self):
        client = TikaClient(url="http://tika:9998/")
        assert client.url == "http://tika:9998"
        with patch("httpx.put", return_value=_ok_response("ok")) as mock_put:
            client.extract_text(b"data", "application/pdf")
        called_url = mock_put.call_args.args[0]
        assert called_url == "http://tika:9998/tika"

    def test_extract_text_default_content_type_when_blank(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.put", return_value=_ok_response("ok")) as mock_put:
            client.extract_text(b"data", "")
        kwargs = mock_put.call_args.kwargs
        assert kwargs["headers"]["Content-Type"] == "application/octet-stream"

    def test_extract_text_timeout_raises_tika_error(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.put", side_effect=httpx.ReadTimeout("slow")):
            with pytest.raises(TikaError, match="timed out"):
                client.extract_text(b"data", "application/pdf")

    def test_extract_text_500_raises_tika_error(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.put", return_value=_http_error_response(500, "server down")):
            with pytest.raises(TikaError, match="HTTP 500"):
                client.extract_text(b"data", "application/pdf")

    def test_extract_text_connect_error_raises_tika_error(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.put", side_effect=httpx.ConnectError("refused")):
            with pytest.raises(TikaError, match="failed"):
                client.extract_text(b"data", "application/pdf")


# ---------------------------------------------------------------------------
# extract_metadata
# ---------------------------------------------------------------------------


class TestExtractMetadata:
    def test_extract_metadata_parses_json(self):
        client = TikaClient(url="http://tika:9998")
        meta = {"Content-Type": "application/pdf", "pdf:version": "1.7"}
        response = _ok_response(body="{}")
        response.json.return_value = meta

        with patch("httpx.put", return_value=response) as mock_put:
            result = client.extract_metadata(b"data", "application/pdf")

        assert result == meta
        called_url = mock_put.call_args.args[0]
        assert called_url == "http://tika:9998/meta"
        kwargs = mock_put.call_args.kwargs
        assert kwargs["headers"]["Accept"] == "application/json"

    def test_extract_metadata_non_dict_raises(self):
        """Tika returning a JSON array must fail with TikaError."""
        client = TikaClient(url="http://tika:9998")
        response = _ok_response(body="[]")
        response.json.return_value = []

        with patch("httpx.put", return_value=response):
            with pytest.raises(TikaError, match="not a JSON object"):
                client.extract_metadata(b"data", "application/pdf")

    def test_extract_metadata_invalid_json_raises(self):
        client = TikaClient(url="http://tika:9998")
        response = _ok_response(body="not json")
        response.json.side_effect = ValueError("bad json")

        with patch("httpx.put", return_value=response):
            with pytest.raises(TikaError, match="non-JSON"):
                client.extract_metadata(b"data", "application/pdf")

    def test_extract_metadata_timeout_raises_tika_error(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.put", side_effect=httpx.ReadTimeout("slow")):
            with pytest.raises(TikaError, match="timed out"):
                client.extract_metadata(b"data", "application/pdf")


# ---------------------------------------------------------------------------
# is_healthy
# ---------------------------------------------------------------------------


class TestIsHealthy:
    def test_is_healthy_happy_path(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.get", return_value=_ok_response("Apache Tika 2.9.0")):
            assert client.is_healthy() is True

    def test_is_healthy_non_200_returns_false(self):
        client = TikaClient(url="http://tika:9998")
        resp = MagicMock()
        resp.status_code = 503
        resp.text = "unavailable"
        with patch("httpx.get", return_value=resp):
            assert client.is_healthy() is False

    def test_is_healthy_empty_body_returns_false(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.get", return_value=_ok_response("")):
            assert client.is_healthy() is False

    def test_is_healthy_timeout_returns_false(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.get", side_effect=httpx.ReadTimeout("slow")):
            assert client.is_healthy() is False

    def test_is_healthy_connect_error_returns_false(self):
        client = TikaClient(url="http://tika:9998")
        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            assert client.is_healthy() is False

    def test_is_healthy_no_url_returns_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        client = TikaClient()
        # Should never raise, just return False.
        assert client.is_healthy() is False


# ---------------------------------------------------------------------------
# Env var loading
# ---------------------------------------------------------------------------


class TestEnvVarLoading:
    def test_env_var_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TIKA_URL", "http://env-tika:9998")
        monkeypatch.setenv("APP_ENV", "development")
        client = TikaClient()
        assert client.url == "http://env-tika:9998"

    def test_env_var_timeout(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TIKA_URL", "http://tika:9998")
        monkeypatch.setenv("TIKA_TIMEOUT_SECONDS", "120")
        client = TikaClient()
        assert client.timeout == 120.0

    def test_env_var_invalid_timeout_falls_back(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TIKA_URL", "http://tika:9998")
        monkeypatch.setenv("TIKA_TIMEOUT_SECONDS", "not-a-number")
        client = TikaClient()
        assert client.timeout == 60.0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


class TestDefaultClientSingleton:
    def test_get_default_tika_client_is_singleton(self, monkeypatch: pytest.MonkeyPatch):
        """Two calls must return the same instance (lazy-init + cache)."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("TIKA_URL", raising=False)

        first = get_default_tika_client()
        second = get_default_tika_client()
        assert first is second

    def test_reset_default_tika_client_clears_cache(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("TIKA_URL", raising=False)

        first = get_default_tika_client()
        reset_default_tika_client()
        second = get_default_tika_client()
        assert first is not second
