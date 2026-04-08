"""Abstract authentication provider interface."""

from abc import ABC, abstractmethod
from typing import TypedDict


class UserDict(TypedDict):
    """User data returned by authentication operations."""

    id: str
    email: str
    full_name: str
    role: str
    org_id: str | None


class AuthProvider(ABC):
    """Abstract base class for authentication providers.

    Subclasses must implement authenticate, get_current_user, and logout.
    """

    @abstractmethod
    def authenticate(self, email: str, password: str) -> UserDict | None:
        """Verify credentials and return user data, or None if invalid."""
        ...

    @abstractmethod
    def get_current_user(self, token: str) -> UserDict | None:
        """Decode an access token and return the associated user, or None."""
        ...

    @abstractmethod
    def logout(self, session_id: str) -> None:
        """Remove the session identified by *session_id*."""
        ...
