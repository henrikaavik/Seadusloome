"""Tabs + TabPanel navigation components.

Design system spec §4.3 + NFR §10.2 (accessibility):
    - ``role="tablist"`` wrapper with ``aria-orientation``.
    - Each tab trigger is ``<button role="tab">`` with ``aria-selected``,
      ``aria-controls``, and the standard ``tabindex`` roving pattern
      (active = 0, inactive = -1).
    - Panels are ``role="tabpanel"`` linked back via ``aria-labelledby``.
    - Arrow-key navigation is handled by ``/static/js/tabs.js`` — pages that
      render ``Tabs`` must include that script tag.
"""

from typing import Literal

from fasthtml.common import *  # noqa: F403

TabsOrientation = Literal["horizontal", "vertical"]


def Tabs(
    tabs: list[tuple[str, str]],
    *,
    active: str | None = None,
    orientation: TabsOrientation = "horizontal",
    cls: str = "",
    **kwargs,
):
    """Render an ARIA tablist.

    ``tabs`` is a list of ``(tab_id, label)`` tuples. If ``active`` is None
    the first tab is selected. JavaScript enhancement is opt-in via the
    ``data-tabs`` attribute which ``tabs.js`` uses as its init hook.
    """
    if not tabs:
        raise ValueError("Tabs requires at least one (id, label) tuple.")

    active_id = active if active is not None else tabs[0][0]
    wrapper_classes = f"tabs tabs-{orientation} {cls}".strip()
    tablist_classes = f"tablist tablist-{orientation}"

    buttons = []
    for tab_id, label in tabs:
        is_selected = tab_id == active_id
        buttons.append(
            ft_hx(
                "button",
                label,
                type="button",
                role="tab",
                id=f"tab-{tab_id}",
                aria_selected="true" if is_selected else "false",
                aria_controls=f"panel-{tab_id}",
                tabindex="0" if is_selected else "-1",
                cls="tab",
                data_tab_id=tab_id,
            )
        )

    return Div(
        ft_hx(
            "div",
            *buttons,
            role="tablist",
            aria_orientation=orientation,
            cls=tablist_classes,
            data_tabs=orientation,
        ),
        cls=wrapper_classes,
        **kwargs,
    )


def TabPanel(tab_id: str, *children, active: bool = False, cls: str = "", **kwargs):
    """Panel associated with a tab via ``aria-labelledby``/``id`` pairing."""
    classes = f"tabpanel {cls}".strip()
    attrs: dict = {
        "role": "tabpanel",
        "id": f"panel-{tab_id}",
        "aria_labelledby": f"tab-{tab_id}",
        "tabindex": "0",
        "cls": classes,
    }
    if not active:
        attrs["hidden"] = True
    attrs.update(kwargs)
    return ft_hx("div", *children, **attrs)
