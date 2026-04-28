"""Email provider tests."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from app.email.service import _reset_provider_for_tests, get_email_provider
from app.email.stub_provider import StubProvider
from app.email.templates import password_reset, password_reset_admin


def test_stub_provider_logs_subject_and_body(caplog):
    provider = StubProvider()
    with caplog.at_level(logging.INFO, logger="app.email.stub_provider"):
        provider.send(
            to="alice@example.com",
            subject="Hello",
            html="<p>Hi</p>",
            text="Hi",
        )
    assert any("alice@example.com" in r.message for r in caplog.records)
    assert any("Hello" in r.message for r in caplog.records)


def test_postmark_provider_calls_emails_send():
    from app.email.postmark_provider import PostmarkProvider

    fake_client = MagicMock()
    with patch("app.email.postmark_provider.PostmarkClient", return_value=fake_client):
        provider = PostmarkProvider(api_token="test-token", default_from="x@y.z")
        provider.send(
            to="alice@example.com",
            subject="Hello",
            html="<p>Hi</p>",
            text="Hi",
        )
    fake_client.emails.send.assert_called_once()
    kwargs = fake_client.emails.send.call_args.kwargs
    assert kwargs["To"] == "alice@example.com"
    assert kwargs["From"] == "x@y.z"
    assert kwargs["Subject"] == "Hello"
    assert kwargs["HtmlBody"] == "<p>Hi</p>"
    assert kwargs["TextBody"] == "Hi"
    assert kwargs["MessageStream"] == "outbound"


def test_postmark_provider_lazy_init():
    """Client construction is deferred until first send."""
    from app.email.postmark_provider import PostmarkProvider

    with patch("app.email.postmark_provider.PostmarkClient") as cls:
        provider = PostmarkProvider(api_token="t", default_from="x@y.z")
        cls.assert_not_called()
        provider.send(to="a@b.c", subject="s", html="h", text="t")
        cls.assert_called_once_with(server_token="t")


@pytest.fixture(autouse=True)
def _reset_email_singleton():
    _reset_provider_for_tests()
    yield
    _reset_provider_for_tests()


def test_provider_is_stub_when_dev_and_no_token(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)
    assert isinstance(get_email_provider(), StubProvider)


def test_provider_is_postmark_when_dev_and_token_present(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("POSTMARK_API_TOKEN", "tok")
    monkeypatch.setenv("EMAIL_FROM", "x@y.z")
    from app.email.postmark_provider import PostmarkProvider

    assert isinstance(get_email_provider(), PostmarkProvider)


def test_provider_raises_in_production_without_token(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="POSTMARK_API_TOKEN"):
        get_email_provider()


def test_password_reset_template_estonian():
    subject, html, text = password_reset(
        full_name="Mari Maasikas",
        reset_url="https://example.com/auth/reset/abc",
    )
    assert "Seadusloome" in subject
    assert "Mari Maasikas" in html
    assert "https://example.com/auth/reset/abc" in html
    assert "https://example.com/auth/reset/abc" in text
    assert "1 tunni" in text  # 1-hour TTL mentioned


def test_password_reset_admin_template_estonian():
    subject, html, text = password_reset_admin(
        full_name="Mari",
        reset_url="https://example.com/auth/reset/xyz",
        admin_name="Henrik Aavik",
    )
    assert "Administraator" in subject
    assert "Henrik Aavik" in html
    assert "https://example.com/auth/reset/xyz" in text
