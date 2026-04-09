"""Live design-system reference page.

Serves a navigable catalog of every design system component at
``/design-system`` so developers can preview tokens, layouts, and components
without logging in. The route lives in ``SKIP_PATHS`` so it is reachable while
the rest of the app requires auth; a banner at the top reminds visitors that
in production access is admin-only.

Each sub-page renders live examples next to the Python snippet that produced
them. Examples are intentionally kept short (3-6 variations per component) so
the page stays a quick reference rather than a Storybook replacement.
"""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.feedback.empty_state import EmptyState
from app.ui.feedback.loading import LoadingSpinner, Skeleton
from app.ui.feedback.toast import Toast
from app.ui.forms.form_field import FormField, FormSelectField, FormTextareaField
from app.ui.layout.page_shell import PageShell
from app.ui.navigation.breadcrumb import Breadcrumb
from app.ui.navigation.tabs import TabPanel, Tabs
from app.ui.primitives.badge import Badge, StatusBadge
from app.ui.primitives.button import Button, IconButton
from app.ui.primitives.icon import Icon
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.modal import ConfirmModal, Modal, ModalBody, ModalFooter, ModalScript
from app.ui.theme import get_theme_from_request

# ---------------------------------------------------------------------------
# Section metadata — drives the landing page cards and breadcrumbs
# ---------------------------------------------------------------------------

SECTIONS: list[tuple[str, str, str]] = [
    ("colors", "Värvid", "Estonia Brand värvipalett koos hex- ja token-väärtustega"),
    ("typography", "Tüpograafia", "Tekstiastmik, kaalud ja reavahed"),
    ("buttons", "Nupud", "Button ja IconButton variandid + suurused"),
    ("forms", "Vormid", "FormField, Input, Select, Textarea näited"),
    ("surfaces", "Pinnad", "Card, Alert, Badge, StatusBadge"),
    ("feedback", "Tagasiside", "Toast, LoadingSpinner, Skeleton, EmptyState"),
    ("data", "Andmed", "DataTable ja Pagination"),
    ("navigation", "Navigatsioon", "Breadcrumb ja Tabs"),
    ("modals", "Modaalid", "Modal ja ConfirmModal"),
    ("icons", "Ikoonid", "Lucide ikooni galerii"),
]

_COLOR_TOKENS: list[tuple[str, str, str]] = [
    ("--estonian-blue", "#0030DE", "Brändi primaarne sinine"),
    ("--parnu", "#CEE2FD", "Hele sinine taust"),
    ("--liivi", "#000087", "Sügav sinine, hover"),
    ("--paldiski", "#0062F5", "Keskmine sinine"),
    ("--narva", "#00C3FF", "Tsüaan rõhuasetus"),
    ("--haapsalu", "#FCEEC8", "Soe kollane, hoiatused"),
    ("--ehakivi", "#FFFFFF", "Valge pind"),
    ("--pahkla", "#F1F5F9", "Helehall taust"),
    ("--hellamaa", "#CBD5E1", "Piirid"),
    ("--kabelikivi", "#64748B", "Sekundaarne tekst"),
    ("--majakivi", "#3D4B5E", "Tugev sekundaarne tekst"),
    ("--mustkivi", "#0F172A", "Primaarne tekst"),
    ("--success", "#15803D", "Edu"),
    ("--warning", "#CA8A04", "Hoiatus"),
    ("--danger", "#B91C1C", "Viga"),
    ("--info", "#0062F5", "Info (paldiski alias)"),
]

_TYPE_SCALE: list[tuple[str, str, str]] = [
    ("text-4xl", "2.25rem", "Hero pealkiri"),
    ("text-3xl", "1.875rem", "Lehe pealkiri (h1)"),
    ("text-2xl", "1.5rem", "Lehe alampealkiri (h2)"),
    ("text-xl", "1.25rem", "Kaardi pealkiri"),
    ("text-lg", "1.125rem", "Alampealkiri"),
    ("text-base", "1rem", "Põhitekst"),
    ("text-sm", "0.875rem", "Tabelilahter"),
    ("text-xs", "0.75rem", "Metaandmed"),
]

_ICON_SAMPLES: list[str] = [
    "home",
    "search",
    "user",
    "settings",
    "check",
    "x",
    "plus",
    "minus",
    "trash",
    "edit",
    "save",
    "upload",
    "download",
    "file-text",
    "folder",
    "bell",
    "mail",
    "message-circle",
    "alert-circle",
    "info",
]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _snippet(code: str):
    """Render a Python snippet in a ``<pre><code>`` block."""
    return Pre(Code(code.strip(), cls="ds-code"), cls="ds-snippet")  # noqa: F405


def _example(title: str, rendered, code: str):
    """Card wrapping a live rendered component and its source snippet."""
    return Card(
        CardHeader(H3(title, cls="ds-example-title")),  # noqa: F405
        CardBody(
            Div(rendered, cls="ds-example-render"),  # noqa: F405
            _snippet(code),
        ),
        cls="ds-example",
    )


def _admin_notice():
    """Banner reminding visitors that prod locks this page to admins."""
    return Alert(
        "Arendusrežiimis on see leht avalik. Tootmises on ligipääs ainult admin-rollile.",
        variant="info",
        title="Disainisüsteemi teatmik",
    )


def _section_shell(req: Request, title: str, slug: str, *content):
    """Wrap a sub-page's content in PageShell with a consistent breadcrumb."""
    return PageShell(
        _admin_notice(),
        Breadcrumb(("Disainisüsteem", "/design-system"), title),
        H2(title),  # noqa: F405
        *content,
        Div(  # noqa: F405
            A("\u2190 Tagasi teatmikku", href="/design-system", cls="ds-back-link"),  # noqa: F405
            cls="ds-footer-nav",
        ),
        title=f"Disainisüsteem — {title}",
        user=req.scope.get("auth"),
        theme=get_theme_from_request(req),
    )


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------


def _section_card(slug: str, label: str, description: str):
    return Card(
        CardHeader(H3(label)),  # noqa: F405
        CardBody(
            P(description),  # noqa: F405
            A("Ava \u2192", href=f"/design-system/{slug}", cls="ds-card-link"),  # noqa: F405
        ),
        cls="ds-index-card",
    )


def design_system_index(req: Request):
    """GET /design-system — landing page with cards linking to each section."""
    cards = [_section_card(slug, label, desc) for slug, label, desc in SECTIONS]
    return PageShell(
        _admin_notice(),
        P(  # noqa: F405
            "Seadusloome disainisüsteem põhineb Estonia Brand visuaalsel "
            "identiteedil (brand.estonia.ee). Siit leiad elavad näited "
            "kõigist komponentidest ja disaini žetoonidest."
        ),
        Div(*cards, cls="ds-index-grid", id="ds-index-grid"),  # noqa: F405
        title="Disainisüsteem",
        user=req.scope.get("auth"),
        theme=get_theme_from_request(req),
    )


# ---------------------------------------------------------------------------
# Section pages
# ---------------------------------------------------------------------------


def _colors_page(req: Request):
    swatches = [
        Div(  # noqa: F405
            Div(cls="ds-swatch", style=f"background:{hex_value}"),  # noqa: F405
            Div(  # noqa: F405
                Strong(name, cls="ds-swatch-name"),  # noqa: F405
                Div(hex_value, cls="ds-swatch-hex"),  # noqa: F405
                Div(note, cls="ds-swatch-note"),  # noqa: F405
            ),
            cls="ds-swatch-item",
        )
        for name, hex_value, note in _COLOR_TOKENS
    ]
    return _section_shell(
        req,
        "Värvid",
        "colors",
        P(
            "Iga värv on CSS muutuja tokens.css failis — kasuta semantilist "  # noqa: F405
            "tokenit (nt --color-primary) tavalises koodis."
        ),
        Div(*swatches, cls="ds-swatch-grid"),  # noqa: F405
    )


def _typography_page(req: Request):
    rows = [
        Div(  # noqa: F405
            Span(name, cls="ds-type-name"),  # noqa: F405
            Span(size, cls="ds-type-size"),  # noqa: F405
            Div(
                f"Seaduseelnõu {name}",
                cls=f"ds-type-sample {name}",  # noqa: F405
                style=f"font-size:{size}",
            ),
            Span(note, cls="ds-type-note"),  # noqa: F405
            cls="ds-type-row",
        )
        for name, size, note in _TYPE_SCALE
    ]
    return _section_shell(
        req,
        "Tüpograafia",
        "typography",
        P("Font: Aino (Estonia Brand). Baasmõõt 16px, skaala rem-põhine."),  # noqa: F405
        Div(*rows, cls="ds-type-list"),  # noqa: F405
    )


def _buttons_page(req: Request):
    examples = [
        _example(
            "Primary",
            Button("Salvesta", variant="primary"),
            'Button("Salvesta", variant="primary")',
        ),
        _example(
            "Secondary",
            Button("Tühista", variant="secondary"),
            'Button("Tühista", variant="secondary")',
        ),
        _example(
            "Ghost", Button("Näita veel", variant="ghost"), 'Button("Näita veel", variant="ghost")'
        ),
        _example(
            "Danger", Button("Kustuta", variant="danger"), 'Button("Kustuta", variant="danger")'
        ),
        _example(
            "Suurused",
            Div(  # noqa: F405
                Button("Väike", size="sm"),
                Button("Keskmine", size="md"),
                Button("Suur", size="lg"),
                cls="ds-row",
            ),
            'Button("Väike", size="sm")\nButton("Keskmine", size="md")\nButton("Suur", size="lg")',
        ),
        _example(
            "Olek",
            Div(  # noqa: F405
                Button("Salvestan", loading=True),
                Button("Deaktiveeritud", disabled=True),
                IconButton("x", aria_label="Sulge"),
                cls="ds-row",
            ),
            'Button("Salvestan", loading=True)\nButton("Deaktiveeritud", disabled=True)\n'
            'IconButton("x", aria_label="Sulge")',
        ),
    ]
    return _section_shell(req, "Nupud", "buttons", *examples)


def _forms_page(req: Request):
    examples = [
        _example(
            "Tekstisisend",
            FormField(
                "email", "E-post", type="email", required=True, help="Sisestage oma tööpost"
            ),
            'FormField("email", "E-post", type="email", required=True,\n'
            '          help="Sisestage oma tööpost")',
        ),
        _example(
            "Veaga väli",
            FormField(
                "password",
                "Parool",
                type="password",
                error="Parool peab olema vähemalt 8 tähemärki",
            ),
            'FormField("password", "Parool", type="password",\n'
            '          error="Parool peab olema vähemalt 8 tähemärki")',
        ),
        _example(
            "Textarea",
            FormTextareaField("note", "Märkus", rows=3, placeholder="Lisage märkus..."),
            'FormTextareaField("note", "Märkus", rows=3,\n'
            '                  placeholder="Lisage märkus...")',
        ),
        _example(
            "Select",
            FormSelectField(
                "role", "Roll", options=[("drafter", "Koostaja"), ("reviewer", "Ülevaataja")]
            ),
            'FormSelectField("role", "Roll",\n'
            '                options=[("drafter", "Koostaja"), ("reviewer", "Ülevaataja")])',
        ),
    ]
    return _section_shell(req, "Vormid", "forms", *examples)


def _surfaces_page(req: Request):
    examples = [
        _example(
            "Card",
            Card(
                CardHeader(H3("Pealkiri")),  # noqa: F405
                CardBody(P("Kaardi sisu paikneb siin.")),
            ),  # noqa: F405
            'Card(CardHeader(H3("Pealkiri")),\n     CardBody(P("Kaardi sisu paikneb siin.")))',
        ),
        _example(
            "Alert variandid",
            Div(  # noqa: F405
                Alert("Info sõnum", variant="info", title="Info"),
                Alert("Edukalt salvestatud", variant="success", title="Õnnestus"),
                Alert("Kontrolli sisendeid", variant="warning", title="Hoiatus"),
                Alert("Midagi läks valesti", variant="danger", title="Viga"),
                cls="ds-stack",
            ),
            'Alert("Info sõnum", variant="info", title="Info")\n'
            'Alert("Edukalt salvestatud", variant="success", title="Õnnestus")',
        ),
        _example(
            "Badge",
            Div(  # noqa: F405
                Badge("Uus", variant="primary"),
                Badge("Valmis", variant="success"),
                Badge("Hoiatus", variant="warning"),
                Badge("Viga", variant="danger"),
                cls="ds-row",
            ),
            'Badge("Uus", variant="primary")',
        ),
        _example(
            "StatusBadge",
            Div(  # noqa: F405
                StatusBadge("ok"),
                StatusBadge("running"),
                StatusBadge("pending"),
                StatusBadge("failed"),
                StatusBadge("warning"),
                cls="ds-row",
            ),
            'StatusBadge("ok")\nStatusBadge("running")\nStatusBadge("failed")',
        ),
    ]
    return _section_shell(req, "Pinnad", "surfaces", *examples)


def _feedback_page(req: Request):
    examples = [
        _example(
            "Toast",
            Div(  # noqa: F405
                Toast("Muudatused salvestatud", variant="success", title="Õnnestus"),
                Toast("Võrguühendus katkes", variant="danger", title="Viga"),
                cls="ds-stack",
            ),
            'Toast("Muudatused salvestatud", variant="success", title="Õnnestus")',
        ),
        _example(
            "LoadingSpinner",
            Div(  # noqa: F405
                LoadingSpinner(size="sm"),
                LoadingSpinner(size="md"),
                LoadingSpinner(size="lg"),
                cls="ds-row",
            ),
            'LoadingSpinner(size="sm")\nLoadingSpinner(size="md")\nLoadingSpinner(size="lg")',
        ),
        _example(
            "Skeleton",
            Div(  # noqa: F405
                Skeleton(variant="text"),
                Skeleton(variant="card"),
                Skeleton(variant="avatar"),
                cls="ds-stack",
            ),
            'Skeleton(variant="text")\nSkeleton(variant="card")\nSkeleton(variant="avatar")',
        ),
        _example(
            "EmptyState",
            EmptyState(
                "Andmed puuduvad",
                message="Alustage, lisades esimese kirje.",
                icon="inbox",
                action=Button("Lisa kirje", variant="primary", icon="plus"),
            ),
            'EmptyState("Andmed puuduvad",\n'
            '           message="Alustage, lisades esimese kirje.",\n'
            '           action=Button("Lisa kirje", variant="primary"))',
        ),
    ]
    return _section_shell(req, "Tagasiside", "feedback", *examples)


def _data_page(req: Request):
    columns = [
        Column("title", "Pealkiri"),
        Column("status", "Olek"),
        Column("updated", "Muudetud", sortable=False),
    ]
    rows = [
        {"title": "Töölepingu seadus", "status": "Kehtiv", "updated": "2024-05-12"},
        {"title": "Asjaõigusseadus", "status": "Eelnõu", "updated": "2024-05-11"},
        {"title": "Isikuandmete kaitse seadus", "status": "Kehtiv", "updated": "2024-04-30"},
    ]
    examples = [
        _example(
            "DataTable",
            DataTable(columns, rows, sort_by="title", sort_dir="asc"),
            'DataTable(columns, rows, sort_by="title", sort_dir="asc")',
        ),
        _example(
            "Pagination",
            Pagination(
                current_page=3, total_pages=10, base_url="/example", page_size=20, total=200
            ),
            'Pagination(current_page=3, total_pages=10, base_url="/example",\n'
            "           page_size=20, total=200)",
        ),
    ]
    return _section_shell(req, "Andmed", "data", *examples)


def _navigation_page(req: Request):
    examples = [
        _example(
            "Breadcrumb",
            Breadcrumb(("Avaleht", "/"), ("Eelnõud", "/drafts"), "Töölepingu seadus"),
            'Breadcrumb(("Avaleht", "/"), ("Eelnõud", "/drafts"), "Töölepingu seadus")',
        ),
        _example(
            "Tabs",
            Div(  # noqa: F405
                Tabs(
                    [("general", "Üldinfo"), ("impact", "Mõjuanalüüs"), ("history", "Ajalugu")],
                    active="general",
                ),
                TabPanel("general", P("Üldinfo sisu"), active=True),  # noqa: F405
            ),
            'Tabs([("general", "Üldinfo"), ("impact", "Mõjuanalüüs"),\n'
            '      ("history", "Ajalugu")], active="general")',
        ),
    ]
    return _section_shell(req, "Navigatsioon", "navigation", *examples)


def _modals_page(req: Request):
    examples = [
        _example(
            "Modal",
            Modal(
                ModalBody(P("Modaali keha paikneb siin.")),  # noqa: F405
                ModalFooter(
                    Button("Tühista", variant="secondary", data_modal_close=""),
                    Button("Kinnita", variant="primary"),
                ),
                title="Näide",
                id="ds-demo-modal",
                size="md",
            ),
            'Modal(ModalBody(P("...")),\n'
            '      ModalFooter(Button("Tühista", variant="secondary"),\n'
            '                  Button("Kinnita", variant="primary")),\n'
            '      title="Näide", id="my-modal", size="md")',
        ),
        _example(
            "ConfirmModal",
            ConfirmModal(
                "Kinnita kustutamine",
                "Kas soovid kirje lõplikult kustutada?",
                id="ds-demo-confirm",
                confirm_variant="danger",
                confirm_label="Kustuta",
            ),
            'ConfirmModal("Kinnita kustutamine",\n'
            '             "Kas soovid kirje lõplikult kustutada?",\n'
            '             id="my-confirm", confirm_variant="danger",\n'
            '             confirm_label="Kustuta")',
        ),
    ]
    return _section_shell(req, "Modaalid", "modals", ModalScript(), *examples)


def _icons_page(req: Request):
    tiles = [
        Div(  # noqa: F405
            Icon(name, size="md"),
            Span(name, cls="ds-icon-label"),  # noqa: F405
            cls="ds-icon-tile",
        )
        for name in _ICON_SAMPLES
    ]
    return _section_shell(
        req,
        "Ikoonid",
        "icons",
        P(
            "Ikoonid pärinevad Lucide'ist ja lingitakse self-hosted SVG "  # noqa: F405
            "sprite'i kaudu (/static/icons/sprite.svg)."
        ),
        Div(*tiles, cls="ds-icon-grid"),  # noqa: F405
        _snippet('Icon("check", size="md")\nIcon("alert-circle", aria_label="Viga")'),
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_design_system_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount GET handlers for ``/design-system`` and all sub-sections."""
    rt("/design-system", methods=["GET"])(design_system_index)
    rt("/design-system/colors", methods=["GET"])(_colors_page)
    rt("/design-system/typography", methods=["GET"])(_typography_page)
    rt("/design-system/buttons", methods=["GET"])(_buttons_page)
    rt("/design-system/forms", methods=["GET"])(_forms_page)
    rt("/design-system/surfaces", methods=["GET"])(_surfaces_page)
    rt("/design-system/feedback", methods=["GET"])(_feedback_page)
    rt("/design-system/data", methods=["GET"])(_data_page)
    rt("/design-system/navigation", methods=["GET"])(_navigation_page)
    rt("/design-system/modals", methods=["GET"])(_modals_page)
    rt("/design-system/icons", methods=["GET"])(_icons_page)
