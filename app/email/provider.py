"""Abstract email provider — concrete impls in stub_provider.py / postmark_provider.py."""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmailProvider(ABC):
    """Send transactional email. One method, sync."""

    @abstractmethod
    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        message_stream: str | None = None,
    ) -> None:
        """Deliver one transactional message. Raises on failure."""
        ...
