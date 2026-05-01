"""StubProvider — logs the email instead of sending. Used in dev/test/CI."""

from __future__ import annotations

import logging

from app.email.provider import EmailProvider

logger = logging.getLogger(__name__)


class StubProvider(EmailProvider):
    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        message_stream: str | None = None,
    ) -> None:
        logger.info(
            "[StubEmail] to=%s subject=%r stream=%s text=%r",
            to,
            subject,
            message_stream or "outbound",
            text,
        )
