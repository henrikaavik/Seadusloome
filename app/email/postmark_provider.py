"""PostmarkProvider — Postmark HTTP API via postmarker.

Lazy-initialises the SDK client on first send so dev/test environments
that have not installed postmarker still work as long as the stub path
is taken.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from postmarker.core import PostmarkClient

from app.email.provider import EmailProvider

logger = logging.getLogger(__name__)


class PostmarkProvider(EmailProvider):
    def __init__(self, *, api_token: str, default_from: str) -> None:
        self._token = api_token
        self._default_from = default_from
        self._client: Any = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = PostmarkClient(server_token=self._token)
        return self._client

    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        message_stream: str | None = None,
    ) -> None:
        client = self._get_client()
        client.emails.send(
            From=self._default_from,
            To=to,
            Subject=subject,
            HtmlBody=html,
            TextBody=text,
            MessageStream=message_stream or "outbound",
        )
        logger.info("[PostmarkEmail] sent to=%s subject=%r", to, subject)
