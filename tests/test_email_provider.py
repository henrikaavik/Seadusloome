"""Email provider tests."""

import logging
from unittest.mock import MagicMock, patch

from app.email.stub_provider import StubProvider


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
