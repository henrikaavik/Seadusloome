"""Sidebar — left navigation, role-filtered."""

from fasthtml.common import *  # noqa: F403

from app.auth.provider import UserDict

# Navigation items with required roles.
# Each item: (label, href, icon, allowed_roles)
NAV_ITEMS: list[tuple[str, str, str, set[str]]] = [
    ("Töölaud", "/dashboard", "home", {"drafter", "reviewer", "org_admin", "admin"}),
    ("Uurija", "/explorer", "graph", {"drafter", "reviewer", "org_admin", "admin"}),
    ("Eelnõud", "/drafts", "file-text", {"drafter", "reviewer", "org_admin", "admin"}),
    ("Vestlus", "/chat", "message-circle", {"drafter", "reviewer", "org_admin", "admin"}),
    ("Kasutajad", "/org/users", "users", {"org_admin", "admin"}),
    ("Administraator", "/admin", "shield", {"admin"}),
]


def _is_active(active: str | None, href: str) -> bool:
    """Return True if the current ``active`` path matches a nav ``href``.

    Sub-pages keep their parent nav item highlighted, so visiting
    ``/admin/audit`` still highlights ``Administraator`` (``/admin``).
    The root ``/`` link is special-cased to only match the literal root,
    otherwise every path would highlight the root.
    """
    if active is None:
        return False
    if href == "/":
        return active == "/"
    return active == href or active.startswith(href + "/")


def _nav_link(label: str, href: str, icon: str, active: bool):  # noqa: ANN202
    classes = "sidebar-link active" if active else "sidebar-link"
    return Li(  # noqa: F405
        A(  # noqa: F405
            Span(cls=f"sidebar-icon icon-{icon}", aria_hidden="true"),  # noqa: F405
            Span(label, cls="sidebar-label"),  # noqa: F405
            href=href,
            cls=classes,
            aria_current="page" if active else None,
        ),
        cls="sidebar-item",
    )


def Sidebar(user: UserDict | None, active: str | None = None):  # noqa: ANN201
    """Left sidebar filtered by user role. Hidden on mobile (hamburger menu)."""
    if user is None:
        return None

    role = user.get("role", "drafter")
    visible = [(label, href, icon) for (label, href, icon, roles) in NAV_ITEMS if role in roles]

    return Aside(  # noqa: F405
        Nav(  # noqa: F405
            Ul(  # noqa: F405
                *[
                    _nav_link(label, href, icon, _is_active(active, href))
                    for (label, href, icon) in visible
                ],
                cls="sidebar-list",
            ),
            aria_label="Peamenüü",
        ),
        cls="sidebar",
    )
