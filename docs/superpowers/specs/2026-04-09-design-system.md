# Design System Spec

**Status:** Approved
**Date:** 2026-04-09
**Dependencies:** Phase 1 (foundation for all subsequent phases)

---

## 1. Goals

A unified, lightweight component library and design token system that:

- Produces visually consistent UI across all phases (2-5)
- Is built on the **Estonia Brand** visual identity (brand.estonia.ee)
- Is Python-first — components are callable functions returning FastHTML `FT` trees
- Supports **light and dark modes** via CSS custom properties
- Provides a **live reference page** at `/design-system` (admin-only)
- Is small enough to be learned in 30 minutes by a new developer

**Non-goals:** Not a full Storybook replacement. Not a React/Vue component library. No runtime theming configuration per user beyond light/dark.

---

## 2. Estonia Brand Assets

### 2.1 Color palette

The Estonia Brand color system is adopted as-is. Source: brand.estonia.ee/guidelines/colors

**Primary:**
- `--estonian-blue: #0030DE` — brand primary, CTAs, links, focus rings

**Blue family:**
- `--parnu: #CEE2FD` — light blue, backgrounds, badges
- `--liivi: #000087` — deep blue, hover states, dark mode primary
- `--paldiski: #0062F5` — mid blue, secondary actions
- `--narva: #00C3FF` — cyan accent, highlights, graph edges

**Warm accent:**
- `--haapsalu: #FCEEC8` — warm yellow, warnings, info callouts

**Neutrals (Boulders):**
- `--ehakivi: #FFFFFF` — white, surface
- `--pahkla: #F1F5F9` — light gray, app background
- `--hellamaa: #CBD5E1` — borders, dividers
- `--kabelikivi: #64748B` — muted text, secondary labels
- `--majakivi: #3D4B5E` — strong muted text
- `--mustkivi: #0F172A` — primary text, dark mode background

**Semantic (derived for status states):**
- `--success: #15803D` — success actions, passed validations
- `--warning: #CA8A04` — warnings, attention needed
- `--danger: #B91C1C` — errors, destructive actions
- `--info: #0062F5` — informational (alias of paldiski)

### 2.2 Typography

**Primary typeface:** Aino (by Anton Koovit)
- Self-hosted as WOFF2 in `/app/static/fonts/aino/`
- Weights: Regular (400), Bold (700), Bold Italic
- Licensing: assumed to permit government use. **TODO:** Confirm with brand@estonia.ee and include response in `docs/legal/aino-license.md`.

**Fallback stack:** `'Aino', Verdana, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`

**Scale (rem-based, root = 16px):**
- `--text-xs: 0.75rem` (12px) — metadata, timestamps
- `--text-sm: 0.875rem` (14px) — body text, table cells
- `--text-base: 1rem` (16px) — default body
- `--text-lg: 1.125rem` (18px) — subheadings
- `--text-xl: 1.25rem` (20px) — card titles
- `--text-2xl: 1.5rem` (24px) — page headings (h2)
- `--text-3xl: 1.875rem` (30px) — page titles (h1, minimum 30pt per brand)
- `--text-4xl: 2.25rem` (36px) — hero titles

**Line heights:**
- `--leading-tight: 1.25` — headings
- `--leading-normal: 1.5` — body
- `--leading-relaxed: 1.75` — legal text, long-form reading

### 2.3 Spacing (8pt grid)

`--space-0: 0`, `--space-1: 0.25rem` (4px), `--space-2: 0.5rem` (8px), `--space-3: 0.75rem` (12px), `--space-4: 1rem` (16px), `--space-5: 1.25rem` (20px), `--space-6: 1.5rem` (24px), `--space-8: 2rem` (32px), `--space-12: 3rem` (48px), `--space-16: 4rem` (64px), `--space-24: 6rem` (96px).

### 2.4 Radius, shadows, borders

- `--radius-sm: 4px` — inputs, small buttons
- `--radius: 8px` — cards, standard buttons
- `--radius-lg: 12px` — modals, major containers
- `--radius-full: 9999px` — pills, badges

- `--shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.05)`
- `--shadow: 0 4px 6px -1px rgba(15, 23, 42, 0.08)`
- `--shadow-lg: 0 10px 25px -5px rgba(15, 23, 42, 0.12)`

- `--border: 1px solid var(--hellamaa)` — default border

---

## 3. Theme System

### 3.1 Tokens — CSS custom properties

All colors referenced through semantic tokens, not raw color names. This lets dark mode override a single block:

```css
:root {
  --color-background: var(--pahkla);
  --color-surface: var(--ehakivi);
  --color-text: var(--mustkivi);
  --color-text-muted: var(--kabelikivi);
  --color-border: var(--hellamaa);
  --color-primary: var(--estonian-blue);
  --color-primary-hover: var(--liivi);
  --color-primary-text: var(--ehakivi);
  --color-accent: var(--narva);
  --color-success: var(--success);
  --color-warning: var(--warning);
  --color-danger: var(--danger);
  --color-info: var(--info);
  --color-warning-bg: var(--haapsalu);
  --color-info-bg: var(--parnu);
}

[data-theme="dark"] {
  --color-background: var(--mustkivi);
  --color-surface: #1e293b;
  --color-text: var(--ehakivi);
  --color-text-muted: var(--hellamaa);
  --color-border: var(--majakivi);
  --color-primary: var(--narva);
  --color-primary-hover: var(--paldiski);
  --color-primary-text: var(--mustkivi);
}
```

### 3.2 Theme toggle

- Cookie `theme` stores user preference (`light` | `dark` | `system`)
- Default `system` — respects `prefers-color-scheme` via `@media (prefers-color-scheme: dark)`
- Toggle button in `TopBar` component calls `POST /api/theme` with new value
- Server sets cookie, client inline script applies `data-theme` attribute to `<html>` immediately (no FOUC)

### 3.3 Explorer exception

The D3 Explorer keeps its existing dark-only theme. It opts out of the toggle by hardcoding its background and ignoring `data-theme`. This is a deliberate design choice — it's a specialized visualization view that works best in dark.

---

## 4. Component Library

All components live in `app/ui/` as Python functions returning FastHTML `FT` objects.

### 4.1 Module structure

```
app/ui/
├── __init__.py          # Re-exports all public components
├── tokens.py            # Python constants for tokens (used in dynamic styling)
├── theme.py             # Theme toggle logic, theme_before middleware
├── primitives/
│   ├── button.py        # Button, IconButton
│   ├── input.py         # Input, Textarea, Select, Checkbox, Radio
│   ├── form_field.py    # FormField (Label + Input + Help + Error)
│   ├── badge.py         # Badge, StatusBadge
│   └── icon.py          # Icon wrapper (Lucide icons)
├── surfaces/
│   ├── card.py          # Card, CardHeader, CardBody, CardFooter
│   ├── modal.py         # Modal, ConfirmModal
│   └── alert.py         # Alert (info/success/warning/danger)
├── layout/
│   ├── page_shell.py    # PageShell (topbar + sidebar + main)
│   ├── top_bar.py       # TopBar (logo, nav, user menu, theme toggle)
│   ├── sidebar.py       # Sidebar (collapsible, role-filtered nav)
│   └── container.py     # Container (max-width wrapper)
├── data/
│   ├── data_table.py    # DataTable (sortable, paginated)
│   ├── pagination.py    # Pagination controls
│   ├── empty_state.py   # EmptyState (icon + message + action)
│   └── loading.py       # LoadingSpinner, Skeleton
├── feedback/
│   ├── toast.py         # Toast system (sessions-based)
│   └── breadcrumb.py    # Breadcrumb
├── navigation/
│   └── tabs.py          # Tabs, TabPanel
└── forms/
    ├── validators.py    # Pure Python field validators
    └── live_validation.py  # HTMX endpoints for blur validation
```

### 4.2 Component API conventions

**Every component function takes:**
- Content as positional `*children` (FT elements or strings)
- Style variants as `variant: Literal[...]` kwargs
- Custom CSS classes as `cls: str` (appended to defaults)
- Arbitrary HTMX attributes passed through via `**kwargs`

**Example — Button:**

```python
def Button(
    *children,
    variant: Literal["primary", "secondary", "ghost", "danger"] = "primary",
    size: Literal["sm", "md", "lg"] = "md",
    type: str = "button",
    disabled: bool = False,
    loading: bool = False,
    icon: str | None = None,
    cls: str = "",
    **kwargs,
) -> FT:
    """Styled button with variant + size options and optional loading state."""
    classes = f"btn btn-{variant} btn-{size} {cls}".strip()
    if disabled or loading:
        classes += " btn-disabled"
    inner = []
    if icon:
        inner.append(Icon(icon))
    if loading:
        inner.append(LoadingSpinner(size="sm"))
    inner.extend(children)
    return ft_hx("button", *inner, cls=classes, type=type,
                 disabled=(disabled or loading), **kwargs)
```

### 4.3 Full component catalog

| Component | Purpose | Variants |
|-----------|---------|----------|
| `Button` | Actions | primary, secondary, ghost, danger |
| `IconButton` | Icon-only button (e.g., close) | same + sizes |
| `Input` | Single-line text input | text, email, password, number, search, url |
| `Textarea` | Multi-line text | — |
| `Select` | Dropdown | single, searchable |
| `Checkbox` | Boolean | — |
| `Radio` | Single-select in a group | — |
| `FormField` | Label + Input + Help + Error wrapper | — |
| `Form` | Form wrapper with HTMX defaults | — |
| `Badge` | Small status marker | default, primary, success, warning, danger |
| `StatusBadge` | Semantic status (OK/Failed/Running) | success, warning, danger, info, pending |
| `Icon` | Lucide icon wrapper | sm, md, lg |
| `Card` | Container with surface color + shadow | default, bordered, flat |
| `Modal` | Overlay dialog | sm, md, lg, full |
| `ConfirmModal` | Yes/No confirmation | — |
| `Alert` | Inline message | info, success, warning, danger |
| `PageShell` | Top-level layout wrapper | — |
| `TopBar` | Site header | — |
| `Sidebar` | Left navigation | collapsible, pinned |
| `Container` | Max-width content wrapper | sm, md, lg, xl, full |
| `DataTable` | Sortable, paginated table | default, striped |
| `Pagination` | Page controls | — |
| `EmptyState` | No-data placeholder | — |
| `LoadingSpinner` | Spinner | sm, md, lg |
| `Skeleton` | Loading placeholder | text, card, avatar |
| `Toast` | Transient notification | info, success, warning, danger |
| `Breadcrumb` | Hierarchical navigation | — |
| `Tabs` | Tabbed panels | horizontal, vertical |

### 4.4 PageShell — standard layout

Every application page uses `PageShell`, which wraps the content in a consistent structure:

```python
def PageShell(
    *content,
    title: str,
    user: UserDict | None = None,
    breadcrumbs: list[tuple[str, str]] | None = None,
    active_nav: str | None = None,
) -> FT:
    return (
        Title(f"{title} — Seadusloome"),
        Div(
            TopBar(user=user, theme=get_current_theme()),
            Div(
                Sidebar(user=user, active=active_nav) if user else None,
                Main(
                    Container(
                        Breadcrumb(*breadcrumbs) if breadcrumbs else None,
                        H1(title),
                        *content,
                    ),
                    cls="main-content",
                ),
                cls="app-body",
            ),
            ToastContainer(),
            cls="app-shell",
        ),
    )
```

Every route handler returns `PageShell(..., title=..., user=..., active_nav=...)`.

---

## 5. Form System

### 5.1 FormField pattern

```python
FormField(
    name="email",
    label="E-post",
    type="email",
    required=True,
    help="Sisestage oma tööpost",
    validator="validate_email",  # name of registered validator
)
```

Renders:
```html
<div class="form-field">
  <label for="email">E-post <span class="required">*</span></label>
  <input type="email" id="email" name="email" required
         hx-post="/api/validate/email" hx-trigger="blur" hx-target="#email-error">
  <small class="help-text">Sisestage oma tööpost</small>
  <div id="email-error" class="error-text"></div>
</div>
```

### 5.2 Validators

Pure Python functions in `app/ui/forms/validators.py`:

```python
def validate_email(value: str) -> str | None:
    """Returns error message or None if valid."""
    if not value or "@" not in value:
        return "Sisestage kehtiv e-posti aadress"
    return None

def validate_password_strength(value: str) -> str | None:
    if len(value) < 8:
        return "Parool peab olema vähemalt 8 tähemärki"
    if not any(c.isupper() for c in value):
        return "Parool peab sisaldama vähemalt ühte suurtähte"
    if not any(c.isdigit() for c in value):
        return "Parool peab sisaldama vähemalt ühte numbrit"
    return None
```

### 5.3 Live validation route

A single generic endpoint handles all validators:

```python
@rt("/api/validate/{field_name}", methods=["POST"])
def validate_field(field_name: str, req: Request):
    form = await req.form()
    value = form.get(field_name, "")
    validator = get_validator(field_name)  # looks up from registry
    error = validator(value) if validator else None
    if error:
        return Div(error, cls="error-text", id=f"{field_name}-error")
    return Div("", id=f"{field_name}-error")
```

Validators are registered in a dict at startup so the route can look them up by name.

### 5.4 Server-side validation on submit

Form submit handlers call the same validators and return the form with errors populated:

```python
def submit_user_form(req: Request, email: str, password: str):
    errors = {}
    if err := validate_email(email): errors["email"] = err
    if err := validate_password_strength(password): errors["password"] = err

    if errors:
        return UserForm(email=email, errors=errors)  # re-render with errors

    # ... save user ...
    return RedirectResponse("/admin/users", status_code=303)
```

---

## 6. Icons

**Library:** Lucide (https://lucide.dev) — open-source, ~1300 icons, SVG

**Delivery:** Self-hosted SVG sprite. At build time, a script generates `app/static/icons/sprite.svg` containing only the icons used in the codebase. Unused icons are tree-shaken.

**Usage:**
```python
Icon("check-circle", size="md", color="success")
# Renders: <svg class="icon icon-md text-success"><use href="/static/icons/sprite.svg#check-circle"/></svg>
```

A script `scripts/build_icons.py` scans the codebase for `Icon("...")` calls, downloads matching SVGs from Lucide, and builds the sprite. Run in CI before Docker build.

---

## 7. Toast Notifications

Built on FastHTML's `setup_toasts()` for session-persisted messages + a JS layer for real-time toasts from WebSocket events.

```python
# In a route handler:
add_toast(session, "Kasutaja loodud", "success")
return RedirectResponse("/admin/users")

# In a WebSocket handler (real-time):
await send_to_client(ws, {"type": "toast", "message": "Sünkroonimine valmis", "variant": "success"})
```

The `ToastContainer()` component in PageShell reads both sources.

---

## 8. Live Reference Page

### 8.1 Route

`GET /design-system` — accessible to `admin` role only.

### 8.2 Structure

```
/design-system
├── /colors          # Full color palette with hex + CSS variable names
├── /typography      # Type scale, weights, line heights
├── /components      # Every component rendered with code snippets
│   ├── /buttons
│   ├── /forms
│   ├── /cards
│   ├── /tables
│   └── ...
└── /patterns        # Common layouts (auth, admin, form)
```

### 8.3 Implementation

Each section is a FastHTML page that imports components from `app/ui/` and renders them with their source code shown beside them (via `inspect.getsource`).

```python
def show_button_examples():
    examples = [
        ("Primary", Button("Salvesta", variant="primary")),
        ("Secondary", Button("Tühista", variant="secondary")),
        ("Ghost", Button("Näita veel", variant="ghost")),
        ("Danger", Button("Kustuta", variant="danger")),
        ("With icon", Button("Lisa", variant="primary", icon="plus")),
        ("Loading", Button("Salvestan...", variant="primary", loading=True)),
        ("Disabled", Button("Deaktiveeritud", disabled=True)),
    ]
    return [
        Card(
            H3(name),
            Div(button, cls="example-render"),
            Pre(Code(inspect.getsource(button.__class__) if callable(button) else "")),
        )
        for name, button in examples
    ]
```

---

## 9. Migration from Current UI

Phase 1 already uses Pico CSS defaults and some inline styles. The migration is incremental:

1. Introduce `app/ui/` with tokens and base components
2. Update `PageShell` and rewire `app/main.py` to use it
3. Convert admin dashboard pages to use new components
4. Convert personal dashboard
5. Convert auth pages (login)
6. Convert org/user management pages
7. Remove Pico CSS from FastHTML initialization (`pico=False`)
8. Verify all pages render correctly in both themes

Each step is a commit. No page is forcibly rewritten — if a page already uses custom styling that works, it stays until the owning phase touches it.

---

## 10. Accessibility

- All interactive components have focus rings (`:focus-visible { outline: 2px solid var(--color-primary); }`)
- Color contrast AA or better for all text (Mustkivi on Pahkla = 16:1)
- Form fields always have `<label>` elements (no placeholder-only labels)
- Icons are decorative by default; semantic icons have `aria-label`
- Toast notifications are announced via `role="status"` / `aria-live="polite"`
- Modals trap focus and restore on close
- Keyboard navigation: Tab order follows visual order, Enter activates primary action, Escape closes modals

---

## 11. Testing

- **Unit tests** for validators (`tests/test_validators.py`)
- **Component smoke tests** — every component renders without raising (`tests/test_ui_smoke.py`)
- **Theme test** — both themes render the design-system page without console errors (Playwright, run manually)
- No visual regression testing in Phase 1 of the design system; add later if needed.

---

## 12. Definition of Done (per component)

Each component is "done" when:

1. Python function defined in `app/ui/`
2. CSS styles in `app/static/css/ui.css` (or inline for dynamic styles)
3. Light + dark mode tested
4. Rendered example on `/design-system`
5. Used in at least one real page
6. Smoke test added

---

## 13. Rollout Order (within Phase 2-5 work)

This spec is implemented **incrementally** as each phase needs components:

- **Before Phase 2:** tokens, theme, PageShell, TopBar, Sidebar, Button, FormField, Input, Select, Textarea, Card, Alert, Toast, LoadingSpinner, Icon
- **Phase 2:** Upload, DataTable, Badge, Modal, EmptyState, Skeleton, Pagination
- **Phase 3:** Chat bubble, streaming indicator, Tabs
- **Phase 4:** Annotation popover, Comment thread component, Breadcrumb refinements
- **Phase 5:** API key card, Code snippet display

Each phase spec references specific design system components it depends on.

---
