"""Authentication & authorization package.

This package mixes two kinds of module:

    - Framework-coupled web-layer modules — :mod:`app.auth.middleware`,
      :mod:`app.auth.roles`, :mod:`app.auth.jwt_provider` — which legitimately
      import ``fasthtml`` / ``starlette``; they *are* the web layer.
    - Neutral leaves — :mod:`app.auth.audit` (imports only
      :func:`app.db.get_connection`) and :mod:`app.auth.provider` (ABC +
      ``TypedDict`` only) — which import no web framework at all.

The public re-exports are resolved *lazily* (PEP 562 module ``__getattr__``)
rather than eagerly imported at package import time, mirroring
:mod:`app.dashboard` and :mod:`app.analyysikeskus`. Eagerly re-exporting them
meant that ``import app.auth.audit`` — the audit-logging path the standalone
worker container reaches via the ``app.docs`` job handlers — ran this
``__init__`` and pulled the fasthtml-coupled ``middleware`` / ``roles`` /
``jwt_provider`` siblings into ``sys.modules``, dragging ~13 FastHTML modules
into a worker that must stay framework-free (the promise in
``app/docs/__init__``'s docstring). Lazy exports keep the neutral leaves
(``audit``, ``provider``) importable framework-free, while
``from app.auth import require_role`` / ``log_action`` / ``JWTAuthProvider``
etc. keep working for the web layer and only load their framework-coupled
home module when the name is actually requested (#895).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.auth.audit import log_action
    from app.auth.jwt_provider import JWTAuthProvider
    from app.auth.middleware import auth_before
    from app.auth.provider import AuthProvider, UserDict
    from app.auth.roles import require_org_member, require_role

__all__ = [
    "AuthProvider",
    "JWTAuthProvider",
    "UserDict",
    "auth_before",
    "log_action",
    "require_org_member",
    "require_role",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve the public re-exports from their home modules (PEP 562)."""
    if name == "log_action":
        from app.auth.audit import log_action

        return log_action
    if name == "JWTAuthProvider":
        from app.auth.jwt_provider import JWTAuthProvider

        return JWTAuthProvider
    if name == "auth_before":
        from app.auth.middleware import auth_before

        return auth_before
    if name == "AuthProvider":
        from app.auth.provider import AuthProvider

        return AuthProvider
    if name == "UserDict":
        from app.auth.provider import UserDict

        return UserDict
    if name == "require_org_member":
        from app.auth.roles import require_org_member

        return require_org_member
    if name == "require_role":
        from app.auth.roles import require_role

        return require_role
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
