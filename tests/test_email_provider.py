"""Email provider tests."""

import logging

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
